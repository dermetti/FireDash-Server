from django.urls import path

from apps.reference_data import views

urlpatterns = [
    path(
        "departments/<uuid:department_id>/hydrants/", views.hydrants, name="reference-data-hydrants"
    ),
    path(
        "departments/<uuid:department_id>/hydrants/import/<uuid:preview_id>/",
        views.hydrant_preview,
        name="reference-data-hydrant-preview",
    ),
    path(
        "departments/<uuid:department_id>/hydrants/create/",
        views.hydrant_create,
        name="reference-data-hydrant-create",
    ),
    path("hydrants/<uuid:hydrant_id>/", views.hydrant_manage, name="reference-data-hydrant-manage"),
    path(
        "departments/<uuid:department_id>/fire-plans/",
        views.fire_plans,
        name="reference-data-fire-plans",
    ),
    path(
        "fire-plans/<uuid:fire_plan_id>/",
        views.fire_plan_detail,
        name="reference-data-fire-plan-detail",
    ),
]
