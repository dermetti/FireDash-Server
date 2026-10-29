import pytest
from django.test import Client
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import User


@pytest.mark.django_db
def test_mfa_enrollment_creates_pending_device_without_authenticating() -> None:
    user = User.objects.create_user("admin@example.test", "Admin User", "safe-password")
    client = Client()
    session = client.session
    session["pending_mfa_user_id"] = str(user.id)
    session["pending_mfa_started_at"] = timezone.now().timestamp()
    session.save()

    response = client.get("/accounts/mfa/enroll/", HTTP_HOST="localhost")

    assert response.status_code == 200
    device = TOTPDevice.objects.get(user=user)
    assert device.confirmed is False
    user.refresh_from_db()
    assert user.mfa_enabled is False
    assert response.wsgi_request.user.is_anonymous
    assert "_auth_user_id" not in client.session
    assert client.session["pending_mfa_user_id"] == str(user.pk)
