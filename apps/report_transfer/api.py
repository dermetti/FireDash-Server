from django.conf import settings
from django.views.decorators.debug import sensitive_variables
from drf_spectacular.utils import extend_schema
from rest_framework import parsers
from rest_framework.response import Response

from apps.report_transfer import services
from apps.report_transfer.contract import TransferError, parse_json
from apps.tablets.api import InstallationAPIView


class BoundedPackageParser(parsers.BaseParser):
    media_type = "application/json"

    @sensitive_variables()
    def parse(self, stream, media_type=None, parser_context=None):
        raw = stream.read(settings.REPORT_TRANSFER_MAX_REQUEST_BYTES + 1)
        if len(raw) > settings.REPORT_TRANSFER_MAX_REQUEST_BYTES:
            raise TransferError("package_too_large", 413)
        return parse_json(raw)


class ReportAPIView(InstallationAPIView):
    def handle_exception(self, exc):
        if isinstance(exc, TransferError):
            return Response({"code": exc.code}, status=exc.status)
        if isinstance(exc, OSError):
            return Response({"code": "report_storage_unavailable"}, status=503)
        return super().handle_exception(exc)


class SubmissionView(ReportAPIView):
    parser_classes = [BoundedPackageParser]

    @extend_schema(
        request={"application/json": {"type": "object"}},
        responses={201: {"type": "object"}, 200: {"type": "object"}},
    )
    def post(self, request):
        result, created = services.submit(installation=self.installation, envelope=request.data)
        return Response(result, status=201 if created else 200)


class SubmissionStatusView(ReportAPIView):
    @extend_schema(responses={200: {"type": "object"}})
    def get(self, request, submission_id):
        return Response(
            services.status(installation=self.installation, submission_id=submission_id)
        )


class BoundedTransferParser(parsers.BaseParser):
    media_type = "application/json"

    def parse(self, stream, media_type=None, parser_context=None):
        raw = stream.read(4097)
        if len(raw) > 4096:
            raise TransferError("request_too_large", 413)
        return parse_json(raw)


class TransferView(ReportAPIView):
    parser_classes = [BoundedTransferParser]

    @extend_schema(
        request={"application/json": {"type": "object"}},
        responses={201: {"type": "object"}, 200: {"type": "object"}},
    )
    def post(self, request):
        from apps.report_transfer.transfers import request_transfer

        result, created = request_transfer(installation=self.installation, data=request.data)
        return Response(result, status=201 if created else 200)


class TransferRevokeView(ReportAPIView):
    @extend_schema(responses={200: {"type": "object"}})
    def post(self, request, transfer_id):
        from apps.report_transfer.transfers import revoke_tablet

        return Response(revoke_tablet(installation=self.installation, transfer_id=transfer_id))
