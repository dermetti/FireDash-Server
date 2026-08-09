from django.urls import path

from apps.publications import views

urlpatterns = [
    path(
        "departments/<uuid:department_id>/publications/",
        views.publications,
        name="publications-list",
    ),
    path(
        "publications/<uuid:publication_id>/review/",
        views.publication_review,
        name="publications-review",
    ),
]
