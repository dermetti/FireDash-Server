from django.urls import path

from apps.ingestion import views

urlpatterns = [
    path(
        "departments/<uuid:department_id>/imports/hydrants/",
        views.import_hydrants,
        name="ingestion-import-hydrants",
    ),
    path(
        "departments/<uuid:department_id>/imports/personnel/",
        views.import_personnel,
        name="ingestion-import-personnel",
    ),
    path(
        "departments/<uuid:department_id>/imports/fire-plans/",
        views.import_fire_plans,
        name="ingestion-import-fire-plans",
    ),
    path(
        "departments/<uuid:department_id>/imports/klgv-plans/",
        views.import_klgv_plans,
        name="ingestion-import-klgv-plans",
    ),
    path(
        "departments/<uuid:department_id>/imports/stations-vehicles/",
        views.import_station_vehicles,
        name="ingestion-import-station-vehicles",
    ),
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
        "departments/<uuid:department_id>/imports/<uuid:batch_id>/review/<str:key>/station-resolution/",
        views.review_station_resolution,
        name="ingestion-review-station-resolution",
    ),
    path(
        "departments/<uuid:department_id>/imports/<uuid:batch_id>/review/coordinates/<int:row_index>/",
        views.review_coordinates,
        name="ingestion-review-coordinates",
    ),
    path(
        "departments/<uuid:department_id>/imports/<uuid:batch_id>/review/approve-all/",
        views.review_approve_all,
        name="ingestion-review-approve-all",
    ),
]
