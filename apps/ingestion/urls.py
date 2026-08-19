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
    path(
        "departments/<uuid:department_id>/imports/<uuid:batch_id>/review/<str:key>/approve/",
        views.review_approve,
        name="ingestion-review-approve",
    ),
    path(
        "departments/<uuid:department_id>/imports/<uuid:batch_id>/review/<str:key>/skip/",
        views.review_skip,
        name="ingestion-review-skip",
    ),
    path(
        "departments/<uuid:department_id>/imports/<uuid:batch_id>/review/approve-all/",
        views.review_approve_all,
        name="ingestion-review-approve-all",
    ),
]
