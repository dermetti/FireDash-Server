from django.urls import path

from apps.health import views

urlpatterns = [
    path("live", views.live, name="health-live"),
    path("ready", views.ready, name="health-ready"),
]
