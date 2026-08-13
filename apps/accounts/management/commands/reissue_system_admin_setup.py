from urllib.parse import urlsplit, urlunsplit

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import AccountSetupToken
from apps.accounts.services import create_setup_token_for_user
from apps.authorization.models import SystemRole


class Command(BaseCommand):
    help = (
        "Reissue the initial system administrator account setup URL. "
        "Requires exactly one inactive bootstrap system administrator."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--base-url", required=True)

    @staticmethod
    def _base_url(value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise CommandError(
                "--base-url must be an https:// URL without userinfo, port, "
                "path, query, or fragment."
            )
        return urlunsplit(("https", parsed.hostname, "", "", ""))

    def handle(self, *args, **options) -> str:
        base_url = self._base_url(options["base_url"])
        active_roles = list(SystemRole.objects.filter(active=True).select_related("user"))
        if len(active_roles) == 0:
            raise CommandError("No system administrator exists.")
        if len(active_roles) > 1:
            raise CommandError("Multiple system administrators exist; refusing to reissue.")
        with transaction.atomic():
            role = SystemRole.objects.select_for_update().get(pk=active_roles[0].pk)
            user = role.user
            if user.is_active:
                raise CommandError(
                    "The system administrator is already active; refusing to reissue."
                )
            AccountSetupToken.objects.filter(user=user, used_at__isnull=True).update(
                expires_at=timezone.now()
            )
            _, raw_token = create_setup_token_for_user(user=user, actor=user)
        return f"{base_url}/accounts/setup/{raw_token}/"
