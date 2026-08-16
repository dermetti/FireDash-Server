from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("api/v1/", include("apps.tablets.api_urls")),
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="openapi-schema"),
    path(
        "api/v1/docs/",
        SpectacularSwaggerView.as_view(url_name="openapi-schema"),
        name="openapi-docs",
    ),
    path("accounts/", include("apps.accounts.urls")),
    path("", include("apps.reference_data.urls")),
    path("", include("apps.ingestion.urls")),
    path("", include("apps.portal.urls")),
    path("", include("apps.personnel.urls")),
    path("", include("apps.publications.urls")),
    path("", include("apps.tablets.urls")),
    path("health/", include("apps.health.urls")),
]
