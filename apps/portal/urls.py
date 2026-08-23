from django.urls import path

from apps.portal import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("departments/<uuid:department_id>/data/", views.data_hub, name="portal-data-hub"),
    path("system/departments/", views.system_departments, name="portal-system-departments"),
    path(
        "system/api-compatibility/",
        views.system_api_compatibility,
        name="portal-system-api-compatibility",
    ),
    path(
        "system/departments/<uuid:department_id>/",
        views.system_department_detail,
        name="portal-system-department",
    ),
    path(
        "departments/<uuid:department_id>/manage/",
        views.department_manage,
        name="portal-department-manage",
    ),
    path(
        "departments/<uuid:department_id>/settings/",
        views.department_settings,
        name="portal-department-settings",
    ),
    path(
        "departments/<uuid:department_id>/administrators/<uuid:membership_id>/revoke/",
        views.department_admin_revoke_modal,
        name="portal-department-admin-revoke",
    ),
    path("departments/<uuid:department_id>/stations/", views.stations, name="portal-stations"),
    path(
        "departments/<uuid:department_id>/selectors/<str:kind>/",
        views.scoped_selector,
        name="portal-scoped-selector",
    ),
    path("stations/<uuid:station_id>/", views.station_manage, name="portal-station-manage"),
    path("stations/<uuid:station_id>/edit/", views.station_edit_modal, name="portal-station-edit"),
    path(
        "stations/<uuid:station_id>/delete/",
        views.station_delete_modal,
        name="portal-station-delete",
    ),
    path(
        "stations/<uuid:station_id>/vehicles/create/",
        views.vehicle_create_modal,
        name="portal-vehicle-create",
    ),
    path("stations/<uuid:station_id>/vehicles/", views.vehicles, name="portal-vehicles"),
    path("vehicles/<uuid:vehicle_id>/", views.vehicle_manage, name="portal-vehicle-manage"),
    path("vehicles/<uuid:vehicle_id>/edit/", views.vehicle_edit_modal, name="portal-vehicle-edit"),
    path(
        "vehicles/<uuid:vehicle_id>/delete/",
        views.vehicle_delete_modal,
        name="portal-vehicle-delete",
    ),
    path(
        "departments/<uuid:department_id>/audit/",
        views.department_audit,
        name="portal-department-audit",
    ),
]
