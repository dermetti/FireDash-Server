from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.accounts.models import AccountSetupToken, User
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


def run_reissue(*, base_url: str = "https://fire-backend.internal/") -> str:
    output = StringIO()
    call_command("reissue_system_admin_setup", "--base-url", base_url, stdout=output)
    return output.getvalue().strip()


def _system_admin_user(email: str = "system@example.test") -> User:
    return User.objects.get(email=email)


@pytest.mark.django_db
def test_reissue_returns_new_setup_url_for_inactive_admin():
    run_bootstrap()
    user = _system_admin_user()
    old_ids = set(AccountSetupToken.objects.filter(user=user).values_list("id", flat=True))

    setup_url = run_reissue()

    assert setup_url.startswith("https://fire-backend.internal/accounts/setup/")
    assert setup_url.endswith("/")
    tokens = AccountSetupToken.objects.filter(user=user)
    assert tokens.count() == 2
    new_ids = set(tokens.values_list("id", flat=True)) - old_ids
    assert len(new_ids) == 1


@pytest.mark.django_db
def test_reissue_invalidates_prior_unused_tokens():
    run_bootstrap()
    user = _system_admin_user()

    run_reissue()

    tokens = AccountSetupToken.objects.filter(user=user)
    assert tokens.count() >= 1
    # Every token other than the newest usable one must be unusable.
    usable = [t for t in tokens if t.is_usable(timezone.now())]
    assert len(usable) == 1


@pytest.mark.django_db
def test_reissue_refuses_when_no_admin_exists():
    with pytest.raises(CommandError, match="No system administrator"):
        run_reissue()


@pytest.mark.django_db
def test_reissue_refuses_when_admin_is_active():
    run_bootstrap()
    user = _system_admin_user()
    user.is_active = True
    user.save(update_fields=("is_active",))

    with pytest.raises(CommandError, match="already active"):
        run_reissue()


@pytest.mark.django_db
def test_reissue_refuses_multiple_admins():
    run_bootstrap()
    second = User.objects.create_user("second@example.test", "Second Admin")
    second.set_unusable_password()
    second.is_active = False
    second.save(update_fields=("password", "is_active"))
    SystemRole.objects.create(user=second)

    with pytest.raises(CommandError, match="Multiple system administrators"):
        run_reissue()


@pytest.mark.django_db
def test_repeated_reissue_leaves_single_usable_token():
    run_bootstrap()
    user = _system_admin_user()

    run_reissue()
    run_reissue()

    tokens = AccountSetupToken.objects.filter(user=user)
    usable = [t for t in tokens if t.is_usable(timezone.now())]
    assert len(usable) == 1


@pytest.mark.parametrize(
    "base_url",
    [
        "not-a-url",
        "http://fire-backend.internal",
        "https://user@fire-backend.internal",
        "https://fire-backend.internal:8443",
        "https://fire-backend.internal/some/path",
        "https://fire-backend.internal?query=1",
        "https://fire-backend.internal#fragment",
    ],
)
def test_reissue_rejects_invalid_base_url(base_url):
    with pytest.raises(CommandError):
        call_command("reissue_system_admin_setup", "--base-url", base_url)
