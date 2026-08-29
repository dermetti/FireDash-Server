from django.urls import path

from apps.tablets import api

urlpatterns = [
    path("adoption/preview", api.AdoptionPreviewView.as_view(), name="api-adoption-preview"),
    path("adoption/complete", api.AdoptionCompleteView.as_view(), name="api-adoption-complete"),
    path("tablet/check-in", api.CheckInView.as_view(), name="api-tablet-check-in"),
    path("tablet/refresh", api.RefreshView.as_view(), name="api-tablet-refresh"),
    path("tablet/status", api.StatusView.as_view(), name="api-tablet-status"),
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
        "tablet/fire-plan-generations/<uuid:publication_id>/manifest",
        api.FirePlanGenerationManifestView.as_view(),
        name="api-fire-plan-generation-manifest",
    ),
    path(
        "tablet/fire-plan-generations/<uuid:publication_id>/artifacts/<uuid:artifact_id>/download",
        api.FirePlanDocumentArtifactDownloadView.as_view(),
        name="api-fire-plan-document-download",
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
