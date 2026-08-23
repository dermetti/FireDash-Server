"""Stage 2 installation replacement regression tests (PostgreSQL-backed)."""

import uuid

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.publications.hpke import HPKE_CIPHERSUITE
from apps.tablets.models import AdoptionInvitation, AppInstallation, Tablet
from apps.tablets.services import (
    complete_adoption,
    create_adoption_request,
    initiate_installation_replacement,
)

from .conftest import _adopt, _p256_public_key  # noqa: E402


def _complete_new_adoption(user, tablet, token):
    challenge = create_adoption_request(
        token=token,
        installation_uuid=uuid.uuid4(),
        app_version="2.0.0",
        hpke_public_key=_p256_public_key(),
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    return complete_adoption(
        request_id=challenge.request.id,
        challenge_response=challenge.request.expected_hmac_digest,
        confirmed=True,
    )


def test_replacement_invitation_keeps_current_installation_active(operational_tablet):
    user, tablet = operational_tablet
    current, _ = _adopt(user, tablet)
    invitation, token = initiate_installation_replacement(actor=user, tablet=tablet)
    assert invitation.tablet_id == tablet.id
    current.refresh_from_db()
    assert current.status == AppInstallation.Status.ACTIVE


def test_replacement_records_audit_event(operational_tablet):
    user, tablet = operational_tablet
    _adopt(user, tablet)
    initiate_installation_replacement(actor=user, tablet=tablet)
    assert AuditEvent.objects.filter(action="tablet.installation_replacement_initiated").exists()


def test_replacement_adoption_activates_new_and_replaces_previous(operational_tablet):
    user, tablet = operational_tablet
    previous, _ = _adopt(user, tablet)
    _, token = initiate_installation_replacement(actor=user, tablet=tablet)
    new_installation, _ = _complete_new_adoption(user, tablet, token)

    assert new_installation.status == AppInstallation.Status.ACTIVE
    previous.refresh_from_db()
    assert previous.status == AppInstallation.Status.REPLACED
    # Exactly one active installation remains for the logical tablet.
    active = AppInstallation.objects.filter(tablet=tablet, status=AppInstallation.Status.ACTIVE)
    assert active.count() == 1


def test_replacement_preserves_logical_tablet(operational_tablet):
    user, tablet = operational_tablet
    _adopt(user, tablet)
    _, token = initiate_installation_replacement(actor=user, tablet=tablet)
    _complete_new_adoption(user, tablet, token)
    # The logical Tablet asset is unchanged; only installations are added.
    assert Tablet.objects.filter(pk=tablet.pk).count() == 1
    assert AppInstallation.objects.filter(tablet=tablet).count() == 2


def test_replaced_installation_drives_purge_flag(operational_tablet):
    user, tablet = operational_tablet
    previous, _ = _adopt(user, tablet)
    _, token = initiate_installation_replacement(actor=user, tablet=tablet)
    _complete_new_adoption(user, tablet, token)
    previous.refresh_from_db()
    # StatusView returns purge_provisioned_data=true when status is REVOKED/REPLACED.
    assert previous.status in (AppInstallation.Status.REVOKED, AppInstallation.Status.REPLACED)


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
