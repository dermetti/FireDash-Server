from django.urls import path

from apps.publications import views

urlpatterns = [
    path(
        "departments/<uuid:department_id>/publications/",
        views.publications,
        name="publications-list",
    ),
    path(
        "departments/<uuid:department_id>/publications/status/",
        views.publication_status,
        name="publications-status",
    ),
    path(
        "publications/scopes/<uuid:scope_id>/",
        views.publication_scope_detail,
        name="publications-scope-detail",
    ),
    path(
        "publications/scopes/<uuid:scope_id>/row/",
        views.publication_scope_row,
        name="publications-scope-row",
    ),
    path(
        "publications/scopes/<uuid:scope_id>/status/",
        views.publication_scope_status,
        name="publications-scope-status",
    ),
    path(
        "departments/<uuid:department_id>/publications/rebuild-affected/",
        views.bulk_rebuild,
        name="publications-bulk-rebuild",
    ),
    path(
        "publications/<uuid:scope_id>/rebuild/",
        views.scope_rebuild,
        name="publications-scope-rebuild",
    ),
    path(
        "publications/<uuid:publication_id>/review/",
        views.publication_review,
        name="publications-review",
    ),
    path(
        "publications/<uuid:publication_id>/<str:action>/",
        views.publication_lifecycle_modal,
        name="publications-lifecycle-modal",
    ),
]
