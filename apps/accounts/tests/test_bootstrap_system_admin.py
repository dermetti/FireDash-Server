from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.accounts.models import AccountSetupToken, User
from apps.accounts.services import consume_setup_token
from apps.authorization.models import SystemRole


def run_bootstrap(*, email: str = "system@example.test") -> str:
    output = StringIO()
    call_command(
        "bootstrap_system_admin",
        "--email",
        email,
        "--display-name",
        "Initial System Administrator",
        "--base-url",
        "https://fire-backend.internal/",
        stdout=output,
    )
    return output.getvalue().strip()


@pytest.mark.django_db
def test_bootstrap_creates_inactive_system_admin_and_prints_setup_url():
    setup_url = run_bootstrap()

    user = User.objects.get(email="system@example.test")
    token = AccountSetupToken.objects.get(user=user)
    assert setup_url.startswith("https://fire-backend.internal/accounts/setup/")
    assert setup_url.endswith("/")
    assert user.is_active is False
    assert user.has_usable_password() is False
    assert SystemRole.objects.filter(user=user, active=True).exists()
    assert setup_url.rsplit("/", 2)[1] not in token.token_hash


@pytest.mark.django_db
def test_bootstrap_refuses_second_active_system_admin():
    run_bootstrap()

    with pytest.raises(CommandError, match="already exists"):
        run_bootstrap(email="second@example.test")

    assert User.objects.filter(email="second@example.test").exists() is False


@pytest.mark.django_db
def test_bootstrap_setup_token_expires_and_is_single_use():
    setup_url = run_bootstrap()
    raw_token = setup_url.rstrip("/").rsplit("/", 1)[1]
    token = AccountSetupToken.objects.get()
    token.expires_at = timezone.now() - timedelta(seconds=1)
    token.save(update_fields=("expires_at",))
    assert consume_setup_token(raw_token=raw_token, password="safe-password") is None

    token.expires_at = timezone.now() + timedelta(hours=1)
    token.save(update_fields=("expires_at",))
    assert consume_setup_token(raw_token=raw_token, password="safe-password") is not None
    assert consume_setup_token(raw_token=raw_token, password="safe-password") is None


@pytest.mark.django_db
def test_bootstrap_rolls_back_when_role_creation_fails():
    with patch(
        "apps.accounts.management.commands.bootstrap_system_admin.SystemRole.objects.create",
        side_effect=RuntimeError("database failure"),
    ):
        with pytest.raises(RuntimeError, match="database failure"):
            run_bootstrap()

    assert User.objects.count() == 0
    assert AccountSetupToken.objects.count() == 0


def test_bootstrap_command_has_no_password_argument_or_prompt():
    from apps.accounts.management.commands import bootstrap_system_admin

    source = Path(bootstrap_system_admin.__file__).read_text(encoding="utf-8")
    assert "getpass" not in source
    assert '"--password"' not in source
