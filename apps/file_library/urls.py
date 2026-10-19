from django.urls import path

from apps.file_library import explorer_views, views

urlpatterns = [
    path("stations/<uuid:station_id>/data/", views.station_hub, name="station-data-hub"),
    path("files/", views.index, name="file-library-index"),
    path(
        "files/station-access/activate/",
        views.select_station_access,
        name="file-library-station-access-select",
    ),
    path(
        "files/capacity-reviews/<uuid:review_id>/",
        views.capacity_review,
        name="file-library-capacity-review",
    ),
    path(
        "files/station-access/<uuid:station_id>/activate/",
        views.activate_station_access,
        name="file-library-station-access-activate",
    ),
    path(
        "files/station-access/release/",
        views.release_station_access,
        name="file-library-station-access-release",
    ),
    path(
        "files/<uuid:library_id>/explorer/", explorer_views.explorer, name="file-library-explorer"
    ),
    path(
        "files/<uuid:library_id>/explorer/actions/<slug:action>/",
        explorer_views.action,
        name="file-library-explorer-action",
    ),
    path(
        "files/<uuid:library_id>/station-access/",
        explorer_views.choose_station_access,
        name="file-library-station-access-choose",
    ),
    path(
        "files/station-search/<uuid:department_id>/",
        explorer_views.station_search,
        name="file-library-station-search",
    ),
    path("files/<uuid:library_id>/", views.library_view, name="file-library"),
    path(
        "files/<uuid:library_id>/<uuid:entry_id>/pdf/", views.document, name="file-library-document"
    ),
]
