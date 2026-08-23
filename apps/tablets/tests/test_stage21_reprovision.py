"""Stage 2.1 Re-provision FireDash regression tests (wording + invariants)."""

import uuid

from django.urls import reverse
from django.utils import timezone

from apps.publications.hpke import HPKE_CIPHERSUITE
from apps.tablets.models import AdoptionInvitation, AppInstallation, Tablet
from apps.tablets.services import (
    complete_adoption,
    create_adoption_request,
    initiate_installation_replacement,
)

from .conftest import _adopt, _p256_public_key  # noqa: E402


def _reprovision(user, tablet, token):
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


def test_reprovision_keeps_tablet_identity_and_assignment(operational_tablet):
    user, tablet = operational_tablet
    previous, _ = _adopt(user, tablet)
    assignment_before = tablet.vehicle_assignments.filter(
        valid_until__isnull=True, ended_at__isnull=True
    ).first()

    _, token = initiate_installation_replacement(actor=user, tablet=tablet)
    new_installation, _ = _reprovision(user, tablet, token)

    tablet.refresh_from_db()
    previous.refresh_from_db()
    # Logical tablet identity and assignment are preserved (no new Tablet asset).
    assert Tablet.objects.filter(department=tablet.department).count() == 1
    assignment_after = tablet.vehicle_assignments.filter(
        valid_until__isnull=True, ended_at__isnull=True
    ).first()
    assert assignment_before.vehicle_id == assignment_after.vehicle_id
    assert previous.status == AppInstallation.Status.REPLACED
    assert new_installation.status == AppInstallation.Status.ACTIVE
    assert (
        AppInstallation.objects.filter(tablet=tablet, status=AppInstallation.Status.ACTIVE).count()
        == 1
    )


def test_reprovision_user_facing_wording(client, operational_tablet):
    user, tablet = operational_tablet
    _adopt(user, tablet)
    client.force_login(user)
    html = client.get(
        reverse("tablet-detail", args=(tablet.department_id, tablet.id))
    ).content.decode()
    assert "Re-provision FireDash" in html
    assert "Replace installation" not in html


def test_reprovision_requires_reauth_and_creates_invitation(client, operational_tablet):
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
