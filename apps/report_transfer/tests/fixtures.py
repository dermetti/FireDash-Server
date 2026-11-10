import hashlib
import hmac
from datetime import timedelta
from uuid import uuid4

import pytest
from django.conf import settings
from django.utils import timezone

from apps.accounts.models import User
from apps.organizations.models import Department
from apps.personnel.models import Person
from apps.tablets.models import AppInstallation, Tablet


@pytest.fixture
def request_context(db):
    now = timezone.now()
    user = User.objects.create_user(email="admin@example.test", display_name="Admin")
    department = Department.objects.create(name="Department", short_code="dep", created_by=user)
    tablet = Tablet.objects.create(department=department, display_name="Tablet")
    credential = "tablet-report-credential"
    installation = AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=uuid4(),
        credential_hash=hmac.new(
            settings.SECRET_KEY.encode(), credential.encode(), hashlib.sha256
        ).hexdigest(),
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=now,
        hpke_public_key=b"key",
        hpke_ciphersuite="suite",
        hpke_key_fingerprint="b" * 64,
        hpke_key_verified_at=now,
        adopted_at=now,
        authorization_valid_until=now + timedelta(days=1),
    )
    person = Person.objects.create(
        department=department,
        display_name="Commander",
        incident_commander_eligible=True,
        incident_commander_email="commander@example.test",
        email_verified_at=now,
    )
    return installation, person, credential
