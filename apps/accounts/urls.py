from django.urls import path

from apps.accounts import views

urlpatterns = [
    path("login/", views.account_login, name="accounts-login"),
    path("mfa/enroll/", views.mfa_enroll, name="accounts-mfa-enroll"),
    path("mfa/verify/", views.mfa_verify, name="accounts-mfa-verify"),
    path("setup/<str:token>/", views.account_setup, name="accounts-setup"),
    path("logout/", views.account_logout, name="accounts-logout"),
    path("reauthenticate/", views.reauthenticate, name="accounts-reauthenticate"),
]
