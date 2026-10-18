from django.urls import path

from apps.file_library import views

urlpatterns = [
    path("stations/<uuid:station_id>/data/", views.station_hub, name="station-data-hub"),
    path("files/", views.index, name="file-library-index"),
    path("files/<uuid:library_id>/", views.library_view, name="file-library"),
    path(
        "files/<uuid:library_id>/<uuid:entry_id>/pdf/", views.document, name="file-library-document"
    ),
]
