from django.urls import include, path

urlpatterns = [
    path("accounts/", include("apps.accounts.urls")),
    path("", include("apps.reference_data.urls")),
    path("", include("apps.portal.urls")),
    path("", include("apps.personnel.urls")),
    path("", include("apps.publications.urls")),
    path("health/", include("apps.health.urls")),
]
