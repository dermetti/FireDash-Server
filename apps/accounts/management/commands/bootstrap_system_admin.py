from getpass import getpass

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.authorization.models import SystemRole


class Command(BaseCommand):
    help = "Create the initial system administrator through an interactive password prompt."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--email", required=True)
        parser.add_argument("--display-name", required=True)

    def handle(self, *args, **options) -> str:
        email = options["email"].casefold()
        if User.objects.filter(email=email).exists():
            raise CommandError("A user with this email already exists.")
        password = getpass("Password: ")
        confirmation = getpass("Password (again): ")
        if password != confirmation:
            raise CommandError("Passwords do not match.")
        user = User.objects.create_user(email, options["display_name"], password)
        SystemRole.objects.create(user=user)
        return "Initial system administrator created. Enroll TOTP MFA at first sign-in."
