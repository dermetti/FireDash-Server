"""PostgreSQL-backed tests for tablet lifecycle, adoption, lease, stale, and concurrency."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.db import close_old_connections, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.assignments.models import TabletVehicleAssignment
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station, Vehicle
from apps.publications.hpke import HPKE_CIPHERSUITE
from apps.publications.manifests import request_manifest
from apps.publications.models import SignedManifest
from apps.tablets.models import AdoptionRequest, AppInstallation, Tablet
from apps.tablets.services import (
    TabletError,
    check_in,
    complete_adoption,
    create_adoption_invitation,
    create_adoption_request,
    create_tablet,
    mark_stale_installations,
    retire_tablet,
    verify_credential,
)

from .conftest import _adopt, _p256_public_key  # noqa: E402


def test_create_tablet_is_audited_and_requires_active_department(department_user):
    user = department_user
    department = Department.objects.create(name="Audit Dept", short_code="AUD", created_by=user)
    DepartmentMembership.objects.create(user=user, department=department, created_by=user)
    station = Station.objects.create(department=department, name="Station", short_code="STA")
    vehicle = Vehicle.objects.create(department=department, station=station, display_name="Engine")
    tablet = create_tablet(actor=user, department=department, display_name="Tablet 1")
    TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=timezone.now(), created_by=user
    )
    assert tablet.department == department
    assert tablet.display_name == "Tablet 1"


def test_create_adoption_invitation_generates_unique_token(operational_tablet):
    user, tablet = operational_tablet
    inv1, token1 = create_adoption_invitation(actor=user, tablet=tablet)
    inv2, token2 = create_adoption_invitation(actor=user, tablet=tablet)
    assert token1 != token2
    assert inv1.token_hash != inv2.token_hash
    assert inv1.expires_at > timezone.now()


def test_adoption_invitation_expires_and_cannot_be_used(operational_tablet):
    user, tablet = operational_tablet
    invitation, token = create_adoption_invitation(
        actor=user, tablet=tablet, expires_at=timezone.now() - timedelta(seconds=1)
    )
    with pytest.raises(TabletError, match="expired"):
        create_adoption_request(
            token=token,
            installation_uuid=uuid.uuid4(),
            app_version="1.0.0",
            hpke_public_key=_p256_public_key(),
            hpke_ciphersuite=HPKE_CIPHERSUITE,
        )


def test_adoption_request_replay_is_rejected(operational_tablet):
    user, tablet = operational_tablet
    _, token = create_adoption_invitation(actor=user, tablet=tablet)
    key = _p256_public_key()
    challenge = create_adoption_request(
        token=token,
        installation_uuid=uuid.uuid4(),
        app_version="1.0.0",
        hpke_public_key=key,
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    complete_adoption(
        request_id=challenge.request.id,
        challenge_response=challenge.request.expected_hmac_digest,
        confirmed=True,
    )
    with pytest.raises(TabletError, match="invalid"):
        create_adoption_request(
            token=token,
            installation_uuid=uuid.uuid4(),
            app_version="1.0.0",
            hpke_public_key=key,
            hpke_ciphersuite=HPKE_CIPHERSUITE,
        )


def test_adoption_proof_is_verified_before_creation(operational_tablet):
    user, tablet = operational_tablet
    _, token = create_adoption_invitation(actor=user, tablet=tablet)
    key = _p256_public_key()
    challenge = create_adoption_request(
        token=token,
        installation_uuid=uuid.uuid4(),
        app_version="1.0.0",
        hpke_public_key=key,
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    with pytest.raises(TabletError, match="proof"):
        complete_adoption(
            request_id=challenge.request.id,
            challenge_response=b"wrong-response",
            confirmed=True,
        )
    assert AppInstallation.objects.count() == 0


def test_adoption_creates_installation_with_seven_day_lease(operational_tablet):
    installation, credential = _adopt(*operational_tablet)
    assert installation.status == AppInstallation.Status.ACTIVE
    lease_remaining = installation.authorization_valid_until - timezone.now()
    assert timedelta(days=6) < lease_remaining <= timedelta(days=7)
    assert len(credential) == 43
    assert verify_credential(installation=installation, credential=credential)


def test_successful_adoption_completion_can_recover_once_response_is_lost(operational_tablet):
    user, tablet = operational_tablet
    _, token = create_adoption_invitation(actor=user, tablet=tablet)
    challenge = create_adoption_request(
        token=token,
        installation_uuid=uuid.uuid4(),
        app_version="1.0.0",
        hpke_public_key=_p256_public_key(),
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    installation, first_credential = complete_adoption(
        request_id=challenge.request.id,
        challenge_response=challenge.request.expected_hmac_digest,
        confirmed=True,
    )
    request = AdoptionRequest.objects.get(pk=challenge.request.id)
    replay_valid_until = request.completion_replay_valid_until
    assert replay_valid_until is not None
    replay_installation, recovery_credential = complete_adoption(
        request_id=challenge.request.id,
        challenge_response=challenge.request.expected_hmac_digest,
        confirmed=True,
    )
    assert replay_installation.id == installation.id
    assert recovery_credential != first_credential
    assert (
        AppInstallation.objects.filter(installation_uuid=installation.installation_uuid).count()
        == 1
    )
    installation.refresh_from_db()
    request.refresh_from_db()
    assert request.completion_replay_valid_until == replay_valid_until
    assert not verify_credential(installation=installation, credential=first_credential)
    assert verify_credential(installation=installation, credential=recovery_credential)


def test_adoption_completion_replay_rejects_expiry_invalidation_and_changed_proof(
    operational_tablet,
):
    user, tablet = operational_tablet

    def completed_request():
        _, token = create_adoption_invitation(actor=user, tablet=tablet)
        challenge = create_adoption_request(
            token=token,
            installation_uuid=uuid.uuid4(),
            app_version="1.0.0",
            hpke_public_key=_p256_public_key(),
            hpke_ciphersuite=HPKE_CIPHERSUITE,
        )
        installation, credential = complete_adoption(
            request_id=challenge.request.id,
            challenge_response=challenge.request.expected_hmac_digest,
            confirmed=True,
        )
        return challenge, installation, credential

    expired, expired_installation, expired_credential = completed_request()
    AdoptionRequest.objects.filter(pk=expired.request.id).update(
        completion_replay_valid_until=timezone.now() - timedelta(seconds=1)
    )
    original_count = AppInstallation.objects.count()
    with pytest.raises(TabletError, match="not available"):
        complete_adoption(
            request_id=expired.request.id,
            challenge_response=expired.request.expected_hmac_digest,
            confirmed=True,
        )
    expired_installation.refresh_from_db()
    assert AppInstallation.objects.count() == original_count
    assert verify_credential(installation=expired_installation, credential=expired_credential)

    invalidated, invalidated_installation, invalidated_credential = completed_request()
    AdoptionRequest.objects.filter(pk=invalidated.request.id).update(
        completion_replay_invalidated_at=timezone.now()
    )
    original_count = AppInstallation.objects.count()
    with pytest.raises(TabletError, match="not available"):
        complete_adoption(
            request_id=invalidated.request.id,
            challenge_response=invalidated.request.expected_hmac_digest,
            confirmed=True,
        )
    invalidated_installation.refresh_from_db()
    assert AppInstallation.objects.count() == original_count
    assert verify_credential(
        installation=invalidated_installation, credential=invalidated_credential
    )

    changed_proof, proof_installation, proof_credential = completed_request()
    original_count = AppInstallation.objects.count()
    with pytest.raises(TabletError, match="not available"):
        complete_adoption(
            request_id=changed_proof.request.id,
            challenge_response=b"not-the-original-proof",
            confirmed=True,
        )
    proof_installation.refresh_from_db()
    assert AppInstallation.objects.count() == original_count
    assert verify_credential(installation=proof_installation, credential=proof_credential)


def test_version_only_telemetry_preserves_build_when_version_is_unchanged(operational_tablet):
    installation, credential = _adopt(*operational_tablet)
    installation.app_build = 7
    installation.save(update_fields=("app_build",))
    check_in(installation=installation, credential=credential, app_version="1.0.0")
    installation.refresh_from_db()
    assert installation.app_build == 7
    check_in(installation=installation, credential=credential, app_version="1.0.1")
    installation.refresh_from_db()
    assert installation.app_version == "1.0.1"
    assert installation.app_build is None


def test_check_in_renews_only_near_expiry_and_recovers_when_stale(operational_tablet):
    installation, credential = _adopt(*operational_tablet)
    original_expiry = installation.authorization_valid_until
    first_manifest = request_manifest(installation=installation)
    checked_in = check_in(installation=installation, credential=credential)
    assert checked_in.authorization_valid_until == original_expiry
    assert checked_in.last_successful_check_in_at is not None
    second_manifest = request_manifest(installation=installation)
    assert second_manifest.request_id == first_manifest.request_id
    assert SignedManifest.objects.filter(app_installation=installation).count() == 1

    installation.authorization_valid_until = timezone.now() + timedelta(hours=48)
    installation.save(update_fields=("authorization_valid_until",))
    renewed = check_in(installation=installation, credential=credential)
    assert renewed.authorization_valid_until > original_expiry

    immediately_checked_in = check_in(installation=installation, credential=credential)
    assert immediately_checked_in.authorization_valid_until == renewed.authorization_valid_until

    installation.authorization_valid_until = timezone.now() - timedelta(seconds=1)
    installation.save(update_fields=("authorization_valid_until",))
    recovered = check_in(installation=installation, credential=credential)
    assert recovered.status == AppInstallation.Status.ACTIVE
    assert recovered.authorization_valid_until > timezone.now()


def test_mark_stale_installations_transitions_expired_leases(operational_tablet):
    installation, credential = _adopt(*operational_tablet)
    installation.authorization_valid_until = timezone.now() - timedelta(seconds=1)
    installation.save(update_fields=("authorization_valid_until",))
    count = mark_stale_installations(now=timezone.now())
    assert count == 1
    installation.refresh_from_db()
    assert installation.status == AppInstallation.Status.STALE
    assert installation.stale_at is not None


@pytest.mark.django_db(transaction=True)
def test_concurrent_adoption_completion_replays_serialize_credential_rotation(operational_tablet):
    user, tablet = operational_tablet
    _, token = create_adoption_invitation(actor=user, tablet=tablet)
    challenge = create_adoption_request(
        token=token,
        installation_uuid=uuid.uuid4(),
        app_version="1.0.0",
        hpke_public_key=_p256_public_key(),
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    installation, _ = complete_adoption(
        request_id=challenge.request.id,
        challenge_response=challenge.request.expected_hmac_digest,
        confirmed=True,
    )
    barrier = threading.Barrier(2)

    def replay() -> tuple[AppInstallation, str]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return complete_adoption(
                request_id=challenge.request.id,
                challenge_response=challenge.request.expected_hmac_digest,
                confirmed=True,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: replay(), range(2)))

    assert {result[0].id for result in results} == {installation.id}
    assert (
        AppInstallation.objects.filter(installation_uuid=installation.installation_uuid).count()
        == 1
    )
    installation.refresh_from_db()
    assert (
        sum(
            verify_credential(installation=installation, credential=result[1]) for result in results
        )
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_postgresql_replay_lock_avoids_nullable_outer_join(operational_tablet):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL locking semantics are required for this regression test.")
    user, tablet = operational_tablet
    _, token = create_adoption_invitation(actor=user, tablet=tablet)
    challenge = create_adoption_request(
        token=token,
        installation_uuid=uuid.uuid4(),
        app_version="1.0.0",
        hpke_public_key=_p256_public_key(),
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    complete_adoption(
        request_id=challenge.request.id,
        challenge_response=challenge.request.expected_hmac_digest,
        confirmed=True,
    )

    with CaptureQueriesContext(connection) as captured:
        complete_adoption(
            request_id=challenge.request.id,
            challenge_response=challenge.request.expected_hmac_digest,
            confirmed=True,
        )

    locking_sql = [
        entry["sql"].upper()
        for entry in captured.captured_queries
        if "FOR UPDATE" in entry["sql"].upper()
    ]
    assert locking_sql
    assert all("LEFT OUTER JOIN" not in sql for sql in locking_sql)
    assert any("FOR UPDATE OF" in sql for sql in locking_sql)


def test_retire_tablet_revokes_installations_and_grants(operational_tablet):
    user, tablet = operational_tablet
    installation, credential = _adopt(user, tablet)
    retired = retire_tablet(actor=user, tablet=tablet, reason="End of life")
    assert retired.status == Tablet.Status.RETIRED
    assert not retired.active
    installation.refresh_from_db()
    assert installation.status == AppInstallation.Status.REVOKED
    assert installation.revoked_at is not None
    assert installation.revocation_reason == "End of life"


def test_concurrent_check_in_is_serialized(operational_tablet):
    installation, credential = _adopt(*operational_tablet)
    lease_tokens = []

    def do_check_in():
        install = AppInstallation.objects.get(pk=installation.pk)
        with transaction.atomic():
            renewed = check_in(installation=install, credential=credential)
            lease_tokens.append(renewed.authorization_valid_until)

    do_check_in()
    do_check_in()
    assert len(lease_tokens) == 2
    assert lease_tokens[1] == lease_tokens[0]


def test_adoption_request_ids_are_unique(db, operational_tablet):
    user, tablet = operational_tablet
    key = _p256_public_key()
    _, token1 = create_adoption_invitation(actor=user, tablet=tablet)
    _, token2 = create_adoption_invitation(actor=user, tablet=tablet)
    c1 = create_adoption_request(
        token=token1,
        installation_uuid=uuid.uuid4(),
        app_version="1.0.0",
        hpke_public_key=key,
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    c2 = create_adoption_request(
        token=token2,
        installation_uuid=uuid.uuid4(),
        app_version="1.0.0",
        hpke_public_key=key,
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    assert c1.request.id != c2.request.id
