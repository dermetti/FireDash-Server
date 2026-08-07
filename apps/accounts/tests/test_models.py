import uuid

import pytest

from apps.accounts.models import User


@pytest.mark.django_db
def test_user_uses_uuid_primary_key_and_normalized_email() -> None:
    user = User.objects.create_user(
        "ADMIN@EXAMPLE.TEST", "Admin User", "correct-horse-battery-staple"
    )

    assert isinstance(user.id, uuid.UUID)
    assert user.email == "admin@example.test"
    assert user.check_password("correct-horse-battery-staple")
    assert user.is_active is True
    assert user.mfa_enabled is False


@pytest.mark.django_db
def test_user_requires_email_and_display_name() -> None:
    with pytest.raises(ValueError, match="email"):
        User.objects.create_user("", "Admin User")

    with pytest.raises(ValueError, match="display name"):
        User.objects.create_user("admin@example.test", "")
