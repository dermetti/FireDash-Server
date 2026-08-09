from django.urls import path

from apps.tablets import views

urlpatterns = [
    path("departments/<uuid:department_id>/tablets/", views.tablet_list, name="tablet-list"),
    path(
        "departments/<uuid:department_id>/tablets/<uuid:tablet_id>/",
        views.tablet_detail,
        name="tablet-detail",
    ),
]
