from django.apps import AppConfig


class OutboundMailConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.outbound_mail"
    label = "outbound_mail"
