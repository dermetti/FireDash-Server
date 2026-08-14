from django.urls import path

from apps.tablets import views

urlpatterns = [
    path("departments/<uuid:department_id>/tablets/", views.tablet_list, name="tablet-list"),
    path(
        "departments/<uuid:department_id>/tablets/status-summary/",
        views.tablet_status_summary,
        name="tablet-status-summary",
    ),
    path(
        "departments/<uuid:department_id>/tablets/new/",
        views.tablet_create,
        name="tablet-create",
    ),
    path(
        "departments/<uuid:department_id>/tablets/<uuid:tablet_id>/",
        views.tablet_detail,
        name="tablet-detail",
    ),
    path(
        "departments/<uuid:department_id>/tablets/<uuid:tablet_id>/assign/",
        views.tablet_assign,
        name="tablet-assign",
    ),
    path(
        "departments/<uuid:department_id>/tablets/<uuid:tablet_id>/adopt/",
        views.tablet_adopt,
        name="tablet-adopt",
    ),
    path(
        "departments/<uuid:department_id>/tablets/<uuid:tablet_id>/adopt-status/<uuid:invitation_id>/",
        views.tablet_adoption_status,
        name="tablet-adoption-status",
    ),
    path(
        "departments/<uuid:department_id>/tablets/<uuid:tablet_id>/reactivate/",
        views.tablet_reactivate,
        name="tablet-reactivate",
    ),
    path(
        "departments/<uuid:department_id>/tablets/<uuid:tablet_id>/reactivate-status/<uuid:invitation_id>/",
        views.tablet_reactivation_status,
        name="tablet-reactivation-status",
    ),
    path(
        "departments/<uuid:department_id>/tablets/<uuid:tablet_id>/remove/",
        views.tablet_remove,
        name="tablet-remove",
    ),
]
