from django.urls import path

from apps.ingestion import views

urlpatterns = [
    path("departments/<uuid:department_id>/imports/", views.imports, name="ingestion-imports"),
    path(
        "departments/<uuid:department_id>/imports/<uuid:batch_id>/",
        views.preview,
        name="ingestion-preview",
    ),
    path(
        "departments/<uuid:department_id>/imports/<uuid:batch_id>/cancel/",
        views.cancel,
        name="ingestion-cancel",
    ),
]
