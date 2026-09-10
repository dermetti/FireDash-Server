from django.apps import AppConfig


class OutboundMailConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.outbound_mail"
    label = "outbound_mail"

    def ready(self) -> None:
        # Register built-in factories only; construction/decryption remains lazy.
        from apps.outbound_mail import brevo  # noqa: F401
