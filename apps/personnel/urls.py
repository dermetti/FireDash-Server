from django.urls import path

from apps.personnel import views

urlpatterns = [
    path("departments/<uuid:department_id>/people/", views.people, name="personnel-list"),
    path(
        "departments/<uuid:department_id>/people/create/",
        views.person_create_modal,
        name="personnel-create",
    ),
    path(
        "departments/<uuid:department_id>/people/<uuid:person_id>/",
        views.person_detail,
        name="personnel-detail",
    ),
    path(
        "departments/<uuid:department_id>/people/<uuid:person_id>/edit/",
        views.person_edit_modal,
        name="personnel-edit",
    ),
    path(
        "departments/<uuid:department_id>/people/<uuid:person_id>/delete/",
        views.person_delete_modal,
        name="personnel-delete",
    ),
    path(
        "departments/<uuid:department_id>/people/<uuid:person_id>/eligibility/",
        views.commander_eligibility,
        name="personnel-eligibility",
    ),
    path(
        "departments/<uuid:department_id>/people/<uuid:person_id>/email/",
        views.commander_email,
        name="personnel-email",
    ),
    path(
        "departments/<uuid:department_id>/people/<uuid:person_id>/verify-email/",
        views.verify_email,
        name="personnel-verify-email",
    ),
    path(
        "departments/<uuid:department_id>/people/<uuid:person_id>/offboard/",
        views.offboard,
        name="personnel-offboard",
    ),
    path(
        "departments/<uuid:department_id>/people/<uuid:person_id>/anonymize/",
        views.anonymize,
        name="personnel-anonymize",
    ),
    path(
        "departments/<uuid:department_id>/retention-policy/",
        views.retention_policy,
        name="personnel-retention-policy",
    ),
]
