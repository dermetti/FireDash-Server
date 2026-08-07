from urllib.parse import urlsplit, urlunsplit

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import User
from apps.accounts.services import create_setup_token_for_user
from apps.authorization.models import SystemRole


class Command(BaseCommand):
    help = "Create the initial system administrator and print one account setup URL."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--email", required=True)
        parser.add_argument("--display-name", required=True)
        parser.add_argument("--base-url", required=True)

    @staticmethod
    def _base_url(value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise CommandError(
                "--base-url must be an absolute HTTP(S) URL without query or fragment."
            )
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))

    def handle(self, *args, **options) -> str:
        email = options["email"].casefold()
        base_url = self._base_url(options["base_url"])
        with transaction.atomic():
            if SystemRole.objects.filter(active=True).exists():
                raise CommandError("An active system administrator already exists.")
            if User.objects.filter(email=email).exists():
                raise CommandError("A user with this email already exists.")
            user = User.objects.create_user(email, options["display_name"])
            user.set_unusable_password()
            user.is_active = False
            user.save(update_fields=("password", "is_active"))
            _, token = create_setup_token_for_user(user=user, actor=user)
            SystemRole.objects.create(user=user)
        return f"{base_url}/accounts/setup/{token}/"
