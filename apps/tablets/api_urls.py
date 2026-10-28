from django.urls import path

from apps.report_transfer.api import (
    SubmissionStatusView,
    SubmissionView,
    TransferRevokeView,
    TransferView,
)
from apps.tablets import api

urlpatterns = [
    path("tablet/report-transfers", TransferView.as_view(), name="api-report-transfers"),
    path(
        "tablet/report-transfers/<uuid:transfer_id>/revoke",
        TransferRevokeView.as_view(),
        name="api-report-transfer-revoke",
    ),
    path("tablet/report-submissions", SubmissionView.as_view(), name="api-report-submissions"),
    path(
        "tablet/report-submissions/<uuid:submission_id>/status",
        SubmissionStatusView.as_view(),
        name="api-report-submission-status",
    ),
    path("adoption/preview", api.AdoptionPreviewView.as_view(), name="api-adoption-preview"),
    path("adoption/complete", api.AdoptionCompleteView.as_view(), name="api-adoption-complete"),
    path("tablet/check-in", api.CheckInView.as_view(), name="api-tablet-check-in"),
    path("tablet/refresh", api.RefreshView.as_view(), name="api-tablet-refresh"),
    path("tablet/status", api.StatusView.as_view(), name="api-tablet-status"),
    path(
        "tablet/report-delivery",
        api.ReportDeliveryView.as_view(),
        name="api-tablet-report-delivery",
    ),
    path("tablet/configuration", api.ConfigurationView.as_view(), name="api-tablet-configuration"),
    path("tablet/manifest", api.ManifestView.as_view(), name="api-tablet-manifest"),
    path(
        "tablet/signing-keys/<str:version>",
        api.SigningKeyView.as_view(),
        name="api-tablet-signing-key",
    ),
    path(
        "tablet/datasets/<uuid:publication_id>/download",
        api.DownloadView.as_view(),
        name="api-dataset-download",
    ),
    path(
        "tablet/document-generations/<uuid:publication_id>/manifest",
        api.DocumentGenerationManifestView.as_view(),
        name="api-document-generation-manifest",
    ),
    path(
        "tablet/document-generations/<uuid:publication_id>/artifacts/<uuid:artifact_id>/download",
        api.DocumentArtifactDownloadView.as_view(),
        name="api-document-artifact-download",
    ),
]
