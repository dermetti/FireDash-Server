"""Versioned tablet provisioning API views and installation authentication."""

import base64
import hashlib
import hmac
from dataclasses import dataclass
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import (
    authentication,
    exceptions,
    parsers,
    permissions,
    renderers,
    serializers,
    status,
)
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.authorization.services import minimum_supported_app_version
from apps.publications.manifests import (
    ManifestError,
    authorized_publications,
    manifest_response_etag,
    publication_signing_public_key_for_requested_version,
    request_manifest,
)
from apps.tablets.models import AdoptionRequest, AppInstallation
from apps.tablets.services import (
    TabletError,
    canonical_protocol_datetime,
    check_in,
    complete_adoption,
    create_adoption_request,
    refresh_installation_lease,
)
from apps.tablets.versions import AppVersionError, parse_app_build, parse_app_version

API_MAJOR = 1


class ClientUpdateRequired(exceptions.APIException):
    status_code = status.HTTP_426_UPGRADE_REQUIRED
    default_detail = "This FireDash application version must be upgraded."
    default_code = "client_update_required"
    minimum_app_version: str


class InstallationAuthenticationFailed(exceptions.AuthenticationFailed):
    default_code = "invalid_credential"


@dataclass(frozen=True)
class InstallationPrincipal:
    installation: AppInstallation

    @property
    def is_authenticated(self) -> bool:
        return True


class InstallationBearerAuthentication(authentication.BaseAuthentication):
    """Authenticate opaque installation credentials without exposing a lookup identifier."""

    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header:
            return None
        if len(header) != 2 or header[0].lower() != b"bearer":
            raise InstallationAuthenticationFailed("Invalid installation authorization header.")
        try:
            credential = header[1].decode("ascii")
        except UnicodeDecodeError as error:
            raise InstallationAuthenticationFailed("Invalid installation credential.") from error

        digest = hmac.new(
            settings.SECRET_KEY.encode(), credential.encode(), hashlib.sha256
        ).hexdigest()
        # Do not use a client-provided installation identifier to select the credential hash.
        installation = next(
            (
                candidate
                for candidate in (AppInstallation.objects.select_related("tablet__department"))
                if hmac.compare_digest(candidate.credential_hash, digest)
            ),
            None,
        )
        if installation is None:
            raise InstallationAuthenticationFailed("Invalid installation credential.")
        return InstallationPrincipal(installation), credential


class InstallationBearerScheme(OpenApiAuthenticationExtension):
    target_class = "apps.tablets.api.InstallationBearerAuthentication"
    name = "InstallationBearer"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "installation credential"}


class AdoptionPreviewSerializer(serializers.Serializer[dict[str, object]]):
    token = serializers.CharField(max_length=256, trim_whitespace=False)
    installation_uuid = serializers.UUIDField()
    app_version = serializers.CharField(max_length=64)
    app_build = serializers.IntegerField(required=False, min_value=1)
    hpke_public_key = serializers.CharField()
    hpke_ciphersuite = serializers.CharField(max_length=128)

    def validate_hpke_public_key(self, value):
        try:
            return base64.b64decode(value, validate=True)
        except ValueError as error:
            raise serializers.ValidationError("Must be valid base64.") from error

    def validate_app_version(self, value):
        try:
            return str(parse_app_version(value))
        except AppVersionError as error:
            raise serializers.ValidationError(str(error)) from error


class AdoptionCompleteSerializer(serializers.Serializer[dict[str, object]]):
    adoption_request_id = serializers.UUIDField()
    challenge_response = serializers.CharField()
    confirmed = serializers.BooleanField()

    def validate_challenge_response(self, value):
        try:
            return base64.b64decode(value, validate=True)
        except ValueError as error:
            raise serializers.ValidationError("Must be valid base64.") from error


class AdoptionPreviewResponseSerializer(serializers.Serializer[dict[str, object]]):
    adoption_request_id = serializers.UUIDField()
    encrypted_challenge = serializers.CharField()
    expires_at = serializers.DateTimeField()
    tablet_id = serializers.UUIDField()
    hpke_ciphersuite = serializers.CharField()
    hpke_public_key_fingerprint = serializers.CharField()
    mode = serializers.CharField()
    protocol = serializers.CharField()


class SigningKeyResponseSerializer(serializers.Serializer[dict[str, object]]):
    algorithm = serializers.CharField()
    version = serializers.CharField()
    public_key = serializers.CharField()


class AdoptionCompleteResponseSerializer(serializers.Serializer[dict[str, object]]):
    installation_id = serializers.UUIDField()
    credential = serializers.CharField()
    authorization_valid_until = serializers.DateTimeField()
    server_time = serializers.DateTimeField()


class CheckInResponseSerializer(serializers.Serializer[dict[str, object]]):
    status = serializers.CharField()
    server_time = serializers.DateTimeField()
    authorization_valid_until = serializers.DateTimeField()


class StatusResponseSerializer(serializers.Serializer[dict[str, object]]):
    status = serializers.CharField()
    authorization_valid_until = serializers.DateTimeField()
    purge_provisioned_data = serializers.BooleanField()
    server_time = serializers.DateTimeField()


class ConfigurationResponseSerializer(serializers.Serializer[dict[str, object]]):
    installation_id = serializers.UUIDField()
    tablet_id = serializers.UUIDField()
    department_id = serializers.UUIDField()
    station_id = serializers.UUIDField()
    vehicle_id = serializers.UUIDField()


class ManifestPendingResponseSerializer(serializers.Serializer[dict[str, object]]):
    type = serializers.URLField()
    title = serializers.CharField()
    status = serializers.IntegerField()
    code = serializers.CharField()
    detail = serializers.CharField()
    request_id = serializers.CharField()
    manifest_request_id = serializers.UUIDField()


class ClientUpdateRequiredResponseSerializer(serializers.Serializer[dict[str, object]]):
    type = serializers.URLField()
    title = serializers.CharField()
    status = serializers.IntegerField()
    code = serializers.CharField()
    detail = serializers.CharField()
    request_id = serializers.CharField()
    minimum_app_version = serializers.CharField()


def _problem_from_service(error: Exception) -> exceptions.APIException:
    exception = exceptions.PermissionDenied(str(error))
    exception.default_code = getattr(error, "code", "invalid_request")
    return exception


def _minimum_or_none():
    return minimum_supported_app_version(api_major=API_MAJOR)


def _raise_if_incompatible(app_version: str) -> None:
    minimum = _minimum_or_none()
    if minimum is not None and parse_app_version(app_version) < minimum:
        error = ClientUpdateRequired()
        error.minimum_app_version = str(minimum)
        raise error


def _telemetry_from_headers(request) -> tuple[str | None, int | None, bool]:
    version = request.headers.get("X-FireDash-App-Version")
    build_value = request.headers.get("X-FireDash-App-Build")
    if version is None:
        return None, None, False
    try:
        parsed_version = str(parse_app_version(version))
        parsed_build = parse_app_build(build_value) if build_value is not None else None
    except AppVersionError:
        # Telemetry must not make a valid installation lose its authorization lease.
        return None, None, False
    return parsed_version, parsed_build, build_value is not None


class TabletProtocolAPIView(APIView):
    """Apply private/no-store semantics to every tablet protocol response."""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response.setdefault("Cache-Control", "no-store, private")
        return response


@extend_schema(
    request=AdoptionPreviewSerializer,
    responses={
        201: AdoptionPreviewResponseSerializer,
        (426, "application/problem+json"): ClientUpdateRequiredResponseSerializer,
    },
)
class AdoptionPreviewView(TabletProtocolAPIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    parser_classes = [parsers.JSONParser]

    def post(self, request):
        serializer = AdoptionPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        _raise_if_incompatible(serializer.validated_data["app_version"])
        try:
            challenge = create_adoption_request(**serializer.validated_data)
        except TabletError as error:
            raise _problem_from_service(error) from error
        invitation = challenge.request.invitation
        if invitation is None:
            raise exceptions.APIException("Adoption request is missing its invitation.")
        return Response(
            {
                "adoption_request_id": str(challenge.request.id),
                "encrypted_challenge": base64.b64encode(challenge.encrypted_challenge).decode(
                    "ascii"
                ),
                "expires_at": canonical_protocol_datetime(challenge.request.expires_at),
                "tablet_id": str(invitation.tablet_id),
                "hpke_ciphersuite": challenge.request.hpke_ciphersuite,
                "hpke_public_key_fingerprint": challenge.request.hpke_public_key_fingerprint,
                "mode": "adoption",
                "protocol": "tablet-adoption-v1",
            },
            status=status.HTTP_201_CREATED,
        )


@extend_schema(
    request=AdoptionPreviewSerializer,
    responses={
        201: AdoptionPreviewResponseSerializer,
        (426, "application/problem+json"): ClientUpdateRequiredResponseSerializer,
    },
)
class ReactivationPreviewView(AdoptionPreviewView):
    def post(self, request):
        serializer = AdoptionPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        _raise_if_incompatible(serializer.validated_data["app_version"])
        try:
            challenge = create_adoption_request(**serializer.validated_data, reactivation=True)
        except TabletError as error:
            raise _problem_from_service(error) from error
        invitation = challenge.request.reactivation_invitation
        if invitation is None:
            raise exceptions.APIException("Reactivation request is missing its invitation.")
        return Response(
            {
                "adoption_request_id": str(challenge.request.id),
                "encrypted_challenge": base64.b64encode(challenge.encrypted_challenge).decode(
                    "ascii"
                ),
                "expires_at": canonical_protocol_datetime(challenge.request.expires_at),
                "tablet_id": str(invitation.app_installation.tablet_id),
                "hpke_ciphersuite": challenge.request.hpke_ciphersuite,
                "hpke_public_key_fingerprint": challenge.request.hpke_public_key_fingerprint,
                "mode": "reactivation",
                "protocol": "tablet-adoption-v1",
            },
            status=status.HTTP_201_CREATED,
        )


@extend_schema(
    request=AdoptionCompleteSerializer, responses={201: AdoptionCompleteResponseSerializer}
)
class AdoptionCompleteView(TabletProtocolAPIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    parser_classes = [parsers.JSONParser]
    reactivation = False

    def post(self, request):
        serializer = AdoptionCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            installation, credential = complete_adoption(
                request_id=serializer.validated_data["adoption_request_id"],
                challenge_response=serializer.validated_data["challenge_response"],
                confirmed=serializer.validated_data["confirmed"],
                reactivation=self.reactivation,
            )
        except (TabletError, AppInstallation.DoesNotExist) as error:
            raise _problem_from_service(error) from error
        return Response(
            {
                "installation_id": str(installation.id),
                "credential": credential,
                "authorization_valid_until": installation.authorization_valid_until,
                "server_time": timezone.now(),
            },
            status=status.HTTP_201_CREATED,
        )


@extend_schema(
    request=AdoptionCompleteSerializer, responses={201: AdoptionCompleteResponseSerializer}
)
class ReactivationCompleteView(AdoptionCompleteView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    reactivation = True

    def post(self, request):
        serializer = AdoptionCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        provisioning_request = AdoptionRequest.objects.filter(
            pk=serializer.validated_data["adoption_request_id"],
            reactivation_invitation__isnull=False,
        ).first()
        if provisioning_request is None:
            raise exceptions.PermissionDenied("Reactivation request is not for this installation.")
        if provisioning_request.completed_at is None:
            invitation = provisioning_request.reactivation_invitation
            if invitation is None:
                raise exceptions.PermissionDenied(
                    "Reactivation request is not for this installation."
                )
            principal = InstallationBearerAuthentication().authenticate(request)
            if principal is None or principal[0].installation.id != invitation.app_installation_id:
                raise exceptions.PermissionDenied(
                    "Reactivation request is not for this installation."
                )
        try:
            installation, credential = complete_adoption(
                request_id=serializer.validated_data["adoption_request_id"],
                challenge_response=serializer.validated_data["challenge_response"],
                confirmed=serializer.validated_data["confirmed"],
                reactivation=True,
            )
        except (TabletError, AppInstallation.DoesNotExist) as error:
            raise _problem_from_service(error) from error
        return Response(
            {
                "installation_id": str(installation.id),
                "credential": credential,
                "authorization_valid_until": installation.authorization_valid_until,
                "server_time": timezone.now(),
            },
            status=status.HTTP_201_CREATED,
        )


class InstallationAPIView(TabletProtocolAPIView):
    authentication_classes = [InstallationBearerAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    compatibility_exempt = False
    defer_compatibility = False

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if (
            self.installation.status == AppInstallation.Status.REPLACED
            and not self.compatibility_exempt
        ):
            exception = exceptions.PermissionDenied("Installation has been replaced.")
            exception.default_code = "installation_replaced"
            raise exception
        if not self.compatibility_exempt and not self.defer_compatibility:
            _raise_if_incompatible(self.installation.app_version)

    @property
    def installation(self) -> AppInstallation:
        user = self.request.user
        if not isinstance(user, InstallationPrincipal):
            raise exceptions.NotAuthenticated("Installation authentication is required.")
        return user.installation


_APP_TELEMETRY_HEADERS = [
    OpenApiParameter("X-FireDash-App-Version", str, OpenApiParameter.HEADER, required=False),
    OpenApiParameter("X-FireDash-App-Build", int, OpenApiParameter.HEADER, required=False),
]


@extend_schema(
    request=None,
    responses={
        200: CheckInResponseSerializer,
        (426, "application/problem+json"): ClientUpdateRequiredResponseSerializer,
    },
    parameters=_APP_TELEMETRY_HEADERS,
)
class CheckInView(InstallationAPIView):
    defer_compatibility = True

    def post(self, request):
        try:
            app_version, app_build, build_supplied = _telemetry_from_headers(request)
            minimum = _minimum_or_none()
            installation = check_in(
                installation=self.installation,
                credential=request.auth,
                app_version=app_version,
                app_build=app_build,
                build_supplied=build_supplied,
                minimum_app_version=minimum,
            )
        except (TabletError, PermissionDenied) as error:
            raise _problem_from_service(error) from error
        if getattr(installation, "compatibility_blocked", False):
            update_required = ClientUpdateRequired()
            update_required.minimum_app_version = str(minimum)
            raise update_required
        return Response(
            {
                "status": "active",
                "server_time": timezone.now(),
                "authorization_valid_until": installation.authorization_valid_until,
            }
        )


@extend_schema(
    request=None,
    responses={
        200: CheckInResponseSerializer,
        (426, "application/problem+json"): ClientUpdateRequiredResponseSerializer,
    },
    parameters=_APP_TELEMETRY_HEADERS,
)
class RefreshView(InstallationAPIView):
    """Top up an active tablet lease before the normal synchronization sequence."""

    defer_compatibility = True

    def post(self, request):
        try:
            app_version, app_build, build_supplied = _telemetry_from_headers(request)
            minimum = _minimum_or_none()
            installation = refresh_installation_lease(
                installation=self.installation,
                credential=request.auth,
                app_version=app_version,
                app_build=app_build,
                build_supplied=build_supplied,
                minimum_app_version=minimum,
            )
        except (TabletError, PermissionDenied) as error:
            raise _problem_from_service(error) from error
        if getattr(installation, "compatibility_blocked", False):
            update_required = ClientUpdateRequired()
            update_required.minimum_app_version = str(minimum)
            raise update_required
        return Response(
            {
                "status": "active",
                "server_time": timezone.now(),
                "authorization_valid_until": installation.authorization_valid_until,
            }
        )


@extend_schema(responses={200: StatusResponseSerializer})
class StatusView(InstallationAPIView):
    compatibility_exempt = True

    def get(self, request):
        installation = self.installation
        state = installation.status.lower()
        return Response(
            {
                "status": state,
                "authorization_valid_until": installation.authorization_valid_until,
                "purge_provisioned_data": installation.status
                in (AppInstallation.Status.REVOKED, AppInstallation.Status.REPLACED),
                "server_time": timezone.now(),
            }
        )


def _configuration(installation: AppInstallation) -> dict[str, str]:
    _, vehicle, _ = authorized_publications(installation=installation)
    return {
        "installation_id": str(installation.id),
        "tablet_id": str(installation.tablet_id),
        "department_id": str(installation.tablet.department_id),
        "station_id": str(vehicle.station_id),
        "vehicle_id": str(vehicle.id),
    }


@extend_schema(
    responses={
        200: ConfigurationResponseSerializer,
        (426, "application/problem+json"): ClientUpdateRequiredResponseSerializer,
    }
)
class ConfigurationView(InstallationAPIView):
    def get(self, request):
        try:
            return Response(_configuration(self.installation))
        except ManifestError as error:
            raise _problem_from_service(error) from error


@extend_schema(
    responses={
        200: SigningKeyResponseSerializer,
        (426, "application/problem+json"): ClientUpdateRequiredResponseSerializer,
    }
)
class SigningKeyView(InstallationAPIView):
    def get(self, request, version: str):
        try:
            public_key = publication_signing_public_key_for_requested_version(version)
        except KeyError as error:
            raise exceptions.NotFound("Signing key version is not available.") from error
        except ManifestError as error:
            raise _problem_from_service(error) from error
        return Response(
            {
                "algorithm": "Ed25519",
                "version": version,
                "public_key": base64.b64encode(public_key).decode("ascii"),
            }
        )


@extend_schema(
    responses={
        200: OpenApiTypes.OBJECT,
        (202, "application/problem+json"): ManifestPendingResponseSerializer,
        304: None,
        (426, "application/problem+json"): ClientUpdateRequiredResponseSerializer,
    }
)
class ManifestView(InstallationAPIView):
    def get(self, request):
        try:
            result = request_manifest(installation=self.installation)
        except ManifestError as error:
            raise _problem_from_service(error) from error
        if result.unavailable:
            return Response(
                {
                    "type": "https://fire-backend.internal/problems/manifest-pending",
                    "title": "Manifest pending",
                    "status": status.HTTP_202_ACCEPTED,
                    "code": "manifest_pending",
                    "detail": "The authorized manifest is being prepared.",
                    "request_id": str(getattr(request, "request_id", "")),
                    "manifest_request_id": str(result.request_id),
                },
                status=status.HTTP_202_ACCEPTED,
                headers={"Retry-After": "5"},
                content_type="application/problem+json",
            )
        if result.payload is None:
            raise exceptions.PermissionDenied("Manifest is not available for this installation.")
        payload = result.payload
        etag = manifest_response_etag(payload)
        if request.headers.get("If-None-Match") == etag:
            return Response(status=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
        return Response(payload, headers={"ETag": etag})


class OctetStreamRenderer(renderers.BaseRenderer):
    media_type = "application/octet-stream"
    format = "binary"
    charset = None
    render_style = "binary"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if data is None:
            return b""
        if isinstance(data, bytes):
            return data
        return renderers.JSONRenderer().render(data, accepted_media_type, renderer_context)


@extend_schema(
    # DRF Spectacular otherwise derives its global ``?format=`` parameter from
    # the JSON/octet-stream renderers. Dataset downloads deliberately use
    # Accept plus whole-object ETag semantics, not URL format negotiation.
    parameters=[OpenApiParameter("format", exclude=True)],
    responses={
        (200, "application/octet-stream"): OpenApiTypes.BINARY,
        304: None,
        (426, "application/problem+json"): ClientUpdateRequiredResponseSerializer,
    },
)
class DownloadView(InstallationAPIView):
    # The view returns a plain ``HttpResponse`` on success, so the renderer only
    # participates in content negotiation. Accepting ``application/octet-stream``
    # prevents DRF from raising 406 before ``get()``; structured error payloads are
    # delegated to JSON so RFC 9457 problem responses remain intact.
    renderer_classes = [renderers.JSONRenderer, OctetStreamRenderer]

    def get(self, request, publication_id: UUID):
        try:
            manifest = request_manifest(installation=self.installation)
        except ManifestError as error:
            raise _problem_from_service(error) from error
        if manifest.unavailable or manifest.payload is None:
            raise exceptions.PermissionDenied("Publication is not available for this installation.")
        _, _, publications = authorized_publications(installation=self.installation)
        datasets = manifest.payload.get("datasets")
        if not isinstance(datasets, list):
            raise exceptions.PermissionDenied("Manifest datasets are invalid.")
        authorized_ids = {
            manifest_publication_id
            for dataset in datasets
            if isinstance(dataset, dict)
            and isinstance((manifest_publication_id := dataset.get("publication_id")), str)
        }
        publication = next(
            (
                item
                for item in publications
                if item.id == publication_id and str(item.id) in authorized_ids
            ),
            None,
        )
        if publication is None:
            raise exceptions.NotFound("Publication is not authorized for this installation.")
        etag = f'"{publication.artifact_sha256}"'
        if request.headers.get("If-None-Match") == etag:
            return Response(status=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
        response = HttpResponse(content_type="application/octet-stream")
        response["ETag"] = etag
        response["Accept-Ranges"] = "bytes"
        response["X-Accel-Redirect"] = f"/internal-protected-datasets/{publication.artifact_path}"
        return response
