import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User


@pytest.mark.django_db
def test_mfa_enrollment_renders_qr_code() -> None:
    user = User.objects.create_user("admin@example.test", "Admin User", "safe-password")
    client = Client()
    session = client.session
    session["pending_mfa_user_id"] = str(user.id)
    session["pending_mfa_started_at"] = timezone.now().timestamp()
    session.save()

    response = client.get("/accounts/mfa/enroll/", HTTP_HOST="localhost")

    assert response.status_code == 200
    assert b"data:image/png;base64," in response.content
