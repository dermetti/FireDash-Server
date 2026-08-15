"""Versioned tablet provisioning API views and installation authentication."""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import authentication, exceptions, permissions, renderers, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.publications.manifests import (
    ManifestError,
    authorized_publications,
    publication_signing_public_key,
    request_manifest,
)
from apps.tablets.models import AdoptionRequest, AppInstallation
from apps.tablets.services import (
    TabletError,
    canonical_protocol_datetime,
    check_in,
    complete_adoption,
    create_adoption_request,
)


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
            raise exceptions.AuthenticationFailed("Invalid installation authorization header.")
        try:
            credential = header[1].decode("ascii")
        except UnicodeDecodeError as error:
            raise exceptions.AuthenticationFailed("Invalid installation credential.") from error

        digest = hmac.new(
            settings.SECRET_KEY.encode(), credential.encode(), hashlib.sha256
        ).hexdigest()
        # Do not use a client-provided installation identifier to select the credential hash.
        installation = next(
            (
                candidate
                for candidate in (
                    AppInstallation.objects.select_related("tablet__department").exclude(
                        status=AppInstallation.Status.REPLACED
                    )
                )
                if hmac.compare_digest(candidate.credential_hash, digest)
            ),
            None,
        )
        if installation is None:
            raise exceptions.AuthenticationFailed("Invalid installation credential.")
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
    hpke_public_key = serializers.CharField()
    hpke_ciphersuite = serializers.CharField(max_length=128)

    def validate_hpke_public_key(self, value):
        try:
            return base64.b64decode(value, validate=True)
        except ValueError as error:
            raise serializers.ValidationError("Must be valid base64.") from error


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


class CheckInResponseSerializer(serializers.Serializer[dict[str, object]]):
    status = serializers.CharField()
    server_time = serializers.DateTimeField()
    authorization_valid_until = serializers.DateTimeField()


class StatusResponseSerializer(serializers.Serializer[dict[str, object]]):
    status = serializers.CharField()
    authorization_valid_until = serializers.DateTimeField()
    purge_provisioned_data = serializers.BooleanField()


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
    detail = serializers.CharField()
    request_id = serializers.CharField()
    manifest_request_id = serializers.UUIDField()


def _problem_from_service(error: Exception) -> exceptions.APIException:
    exception = exceptions.PermissionDenied(str(error))
    exception.default_code = "tablet-authorization"
    return exception


@extend_schema(
    request=AdoptionPreviewSerializer, responses={201: AdoptionPreviewResponseSerializer}
)
class AdoptionPreviewView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = AdoptionPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
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
    request=AdoptionPreviewSerializer, responses={201: AdoptionPreviewResponseSerializer}
)
class ReactivationPreviewView(AdoptionPreviewView):
    def post(self, request):
        serializer = AdoptionPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
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
class AdoptionCompleteView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
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
            },
            status=status.HTTP_201_CREATED,
        )


@extend_schema(
    request=AdoptionCompleteSerializer, responses={201: AdoptionCompleteResponseSerializer}
)
class ReactivationCompleteView(AdoptionCompleteView):
    authentication_classes = [InstallationBearerAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    reactivation = True

    def post(self, request):
        serializer = AdoptionCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not AdoptionRequest.objects.filter(
            pk=serializer.validated_data["adoption_request_id"],
            reactivation_invitation__app_installation=request.user.installation,
        ).exists():
            raise exceptions.PermissionDenied("Reactivation request is not for this installation.")
        return super().post(request)


class InstallationAPIView(APIView):
    authentication_classes = [InstallationBearerAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    @property
    def installation(self) -> AppInstallation:
        user = self.request.user
        if not isinstance(user, InstallationPrincipal):
            raise exceptions.NotAuthenticated("Installation authentication is required.")
        return user.installation


@extend_schema(request=None, responses={200: CheckInResponseSerializer})
class CheckInView(InstallationAPIView):
    def post(self, request):
        try:
            installation = check_in(installation=self.installation, credential=request.auth)
        except (TabletError, PermissionDenied) as error:
            raise _problem_from_service(error) from error
        return Response(
            {
                "status": "active",
                "server_time": timezone.now(),
                "authorization_valid_until": installation.authorization_valid_until,
            }
        )


@extend_schema(responses={200: StatusResponseSerializer})
class StatusView(InstallationAPIView):
    def get(self, request):
        installation = self.installation
        state = installation.status.lower()
        return Response(
            {
                "status": state,
                "authorization_valid_until": installation.authorization_valid_until,
                "purge_provisioned_data": installation.status == AppInstallation.Status.REVOKED,
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


@extend_schema(responses={200: ConfigurationResponseSerializer})
class ConfigurationView(InstallationAPIView):
    def get(self, request):
        try:
            return Response(_configuration(self.installation))
        except ManifestError as error:
            raise _problem_from_service(error) from error


@extend_schema(responses={200: SigningKeyResponseSerializer})
class SigningKeyView(InstallationAPIView):
    def get(self, request, version: str):
        if version != settings.PUBLICATION_SIGNING_KEY_VERSION:
            raise exceptions.NotFound("Signing key version is not available.")
        try:
            public_key = publication_signing_public_key()
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
    responses={200: OpenApiTypes.OBJECT, 202: ManifestPendingResponseSerializer, 304: None}
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
        etag_payload = {key: value for key, value in payload.items() if key != "generated_at"}
        etag = (
            '"'
            + hashlib.sha256(
                json.dumps(etag_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            + '"'
        )
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


@extend_schema(responses={200: OpenApiTypes.BINARY, 304: None})
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
