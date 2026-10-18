from django.urls import path

from apps.file_library import views

urlpatterns = [
    path("stations/<uuid:station_id>/data/", views.station_hub, name="station-data-hub"),
    path("files/", views.index, name="file-library-index"),
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
    path("files/<uuid:library_id>/", views.library_view, name="file-library"),
    path(
        "files/<uuid:library_id>/<uuid:entry_id>/pdf/", views.document, name="file-library-document"
    ),
]
