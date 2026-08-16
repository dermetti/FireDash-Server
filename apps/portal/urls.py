from django.urls import path

from apps.portal import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
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
    path("departments/<uuid:department_id>/stations/", views.stations, name="portal-stations"),
    path(
        "departments/<uuid:department_id>/selectors/<str:kind>/",
        views.scoped_selector,
        name="portal-scoped-selector",
    ),
    path("stations/<uuid:station_id>/", views.station_manage, name="portal-station-manage"),
    path("stations/<uuid:station_id>/vehicles/", views.vehicles, name="portal-vehicles"),
    path("vehicles/<uuid:vehicle_id>/", views.vehicle_manage, name="portal-vehicle-manage"),
    path(
        "departments/<uuid:department_id>/audit/",
        views.department_audit,
        name="portal-department-audit",
    ),
]
