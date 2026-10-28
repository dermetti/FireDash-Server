from django.urls import path

from apps.report_transfer import portal

urlpatterns = [
    path("access/<str:token>/", portal.access, name="report-portal-access"),
    path("access/<str:token>/redeem/", portal.redeem, name="report-portal-redeem"),
    path("access/<str:token>/revoke/", portal.revoke_token, name="report-portal-token-revoke"),
    path("", portal.report_list, name="report-portal-list"),
    path("ended/", portal.ended, name="report-portal-ended"),
    path("logout/", portal.logout, name="report-portal-logout"),
    path("revoke/", portal.revoke, name="report-portal-revoke"),
    path("<uuid:report_id>/", portal.detail, name="report-portal-detail"),
    path("<uuid:report_id>/pdf/", portal.pdf, name="report-portal-pdf"),
    path("<uuid:report_id>/photos/<uuid:attachment_id>/", portal.jpeg, name="report-portal-jpeg"),
    path(
        "<uuid:report_id>/photos/<uuid:attachment_id>/preview/",
        portal.jpeg,
        {"preview": True},
        name="report-portal-preview",
    ),
]
