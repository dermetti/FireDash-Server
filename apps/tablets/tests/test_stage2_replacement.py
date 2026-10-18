"""Stage 2 installation replacement regression tests (PostgreSQL-backed)."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.tablets.models import AdoptionInvitation
from apps.tablets.services import initiate_installation_replacement

from .conftest import _adopt  # noqa: E402


def test_replacement_records_audit_event(operational_tablet):
    user, tablet = operational_tablet
    _adopt(user, tablet)
    initiate_installation_replacement(actor=user, tablet=tablet)
    assert AuditEvent.objects.filter(action="tablet.installation_replacement_initiated").exists()


def test_replace_view_requires_recent_reauth(client, operational_tablet):
    user, tablet = operational_tablet
    _adopt(user, tablet)
    client.force_login(user)
    response = client.post(reverse("tablet-replace", args=(tablet.department_id, tablet.id)))
    assert response.status_code == 302
    assert "reauthenticate" in response.url
    assert AdoptionInvitation.objects.count() == 1  # only the original adoption invitation


@pytest.mark.django_db
def test_replace_view_creates_invitation_after_reauth(client, operational_tablet):
    user, tablet = operational_tablet
    _adopt(user, tablet)
    client.force_login(user)
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()
    response = client.post(reverse("tablet-replace", args=(tablet.department_id, tablet.id)))
    assert response.status_code == 200
    assert (
        AdoptionInvitation.objects.filter(
            tablet=tablet, used_at__isnull=True, revoked_at__isnull=True
        ).count()
        == 1
    )
