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
]
