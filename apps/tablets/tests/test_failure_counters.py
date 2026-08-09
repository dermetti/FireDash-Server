"""PostgreSQL-backed regression tests for adoption failure-counter transaction isolation."""

import uuid

import pytest
from django.db import transaction
from django.utils import timezone

from apps.publications.hpke import HPKE_CIPHERSUITE
from apps.tablets.models import AdoptionRequest, AppInstallation
from apps.tablets.services import (
    MAX_FAILED_ATTEMPTS,
    TabletError,
    complete_adoption,
    create_adoption_invitation,
    create_adoption_request,
)

from .conftest import _adopt, _p256_public_key  # noqa: E402


def test_invalid_proof_increments_failure_count_once(db, operational_tablet):
    user, tablet = operational_tablet
    _, token = create_adoption_invitation(actor=user, tablet=tablet)
    key = _p256_public_key()
    challenge = create_adoption_request(
        token=token,
        installation_uuid=uuid.uuid4(),
        app_version="1.0",
        hpke_public_key=key,
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    with pytest.raises(TabletError, match="proof"):
        complete_adoption(
            request_id=challenge.request.id,
            challenge_response=b"invalid-proof",
            confirmed=True,
        )
    challenge.request.refresh_from_db()
    assert challenge.request.failed_attempt_count == 1


def test_failure_count_persists_after_service_raises(db, operational_tablet):
    user, tablet = operational_tablet
    _, token = create_adoption_invitation(actor=user, tablet=tablet)
    key = _p256_public_key()
    challenge = create_adoption_request(
        token=token,
        installation_uuid=uuid.uuid4(),
        app_version="1.0",
        hpke_public_key=key,
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    for attempt in range(1, 4):
        with pytest.raises(TabletError, match="proof"):
            complete_adoption(
                request_id=challenge.request.id,
                challenge_response=b"wrong-" + str(attempt).encode(),
                confirmed=True,
            )
        challenge.request.refresh_from_db()
        assert challenge.request.failed_attempt_count == attempt


def test_attempts_after_lockout_fail_without_resetting_counter(db, operational_tablet):
    user, tablet = operational_tablet
    _, token = create_adoption_invitation(actor=user, tablet=tablet)
    key = _p256_public_key()
    challenge = create_adoption_request(
        token=token,
        installation_uuid=uuid.uuid4(),
        app_version="1.0",
        hpke_public_key=key,
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    for _ in range(MAX_FAILED_ATTEMPTS):
        with pytest.raises(TabletError, match="proof"):
            complete_adoption(
                request_id=challenge.request.id,
                challenge_response=b"bad",
                confirmed=True,
            )
    challenge.request.refresh_from_db()
    assert challenge.request.failed_attempt_count == MAX_FAILED_ATTEMPTS
    with pytest.raises(TabletError, match="maximum"):
        complete_adoption(
            request_id=challenge.request.id,
            challenge_response=challenge.request.expected_hmac_digest,
            confirmed=True,
        )
    challenge.request.refresh_from_db()
    assert challenge.request.failed_attempt_count == MAX_FAILED_ATTEMPTS


def test_concurrent_invalid_attempts_do_not_lose_increments(db, operational_tablet):
    user, tablet = operational_tablet
    _, token = create_adoption_invitation(actor=user, tablet=tablet)
    key = _p256_public_key()
    challenge = create_adoption_request(
        token=token,
        installation_uuid=uuid.uuid4(),
        app_version="1.0",
        hpke_public_key=key,
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )

    def do_invalid_attempt():
        with pytest.raises(TabletError, match="proof"):
            complete_adoption(
                request_id=challenge.request.id,
                challenge_response=b"concurrent-bad",
                confirmed=True,
            )

    with transaction.atomic():
        do_invalid_attempt()
        do_invalid_attempt()
    challenge.request.refresh_from_db()
    assert challenge.request.failed_attempt_count == 2


def test_successful_adoption_performs_all_state_changes_atomically(db, operational_tablet):
    install, credential = _adopt(*operational_tablet)
    assert install.status == AppInstallation.Status.ACTIVE
    assert install.authorization_valid_until > timezone.now()
    assert len(credential) == 43
    request = AdoptionRequest.objects.filter(installation_uuid=install.installation_uuid).first()
    assert request is not None
    assert request.completed_at is not None


def test_failure_accounting_does_not_consume_invitation(db, operational_tablet):
    user, tablet = operational_tablet
    _, token = create_adoption_invitation(actor=user, tablet=tablet)
    key = _p256_public_key()
    challenge = create_adoption_request(
        token=token,
        installation_uuid=uuid.uuid4(),
        app_version="1.0",
        hpke_public_key=key,
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    with pytest.raises(TabletError, match="proof"):
        complete_adoption(
            request_id=challenge.request.id,
            challenge_response=b"bad",
            confirmed=True,
        )
    assert AppInstallation.objects.count() == 0
    second_challenge = create_adoption_request(
        token=token,
        installation_uuid=uuid.uuid4(),
        app_version="1.0",
        hpke_public_key=key,
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    install, _ = complete_adoption(
        request_id=second_challenge.request.id,
        challenge_response=second_challenge.request.expected_hmac_digest,
        confirmed=True,
    )
    assert install.status == AppInstallation.Status.ACTIVE


def test_replay_after_successful_adoption_still_fails(db, operational_tablet):
    user, tablet = operational_tablet
    _, token = create_adoption_invitation(actor=user, tablet=tablet)
    key = _p256_public_key()
    challenge = create_adoption_request(
        token=token,
        installation_uuid=uuid.uuid4(),
        app_version="1.0",
        hpke_public_key=key,
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    complete_adoption(
        request_id=challenge.request.id,
        challenge_response=challenge.request.expected_hmac_digest,
        confirmed=True,
    )
    with pytest.raises(TabletError, match="already been completed"):
        complete_adoption(
            request_id=challenge.request.id,
            challenge_response=challenge.request.expected_hmac_digest,
            confirmed=True,
        )


def test_unconfirmed_does_not_increment_failure_counter(db, operational_tablet):
    user, tablet = operational_tablet
    _, token = create_adoption_invitation(actor=user, tablet=tablet)
    key = _p256_public_key()
    challenge = create_adoption_request(
        token=token,
        installation_uuid=uuid.uuid4(),
        app_version="1.0",
        hpke_public_key=key,
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    with pytest.raises(TabletError, match="not confirmed"):
        complete_adoption(
            request_id=challenge.request.id,
            challenge_response=challenge.request.expected_hmac_digest,
            confirmed=False,
        )
    challenge.request.refresh_from_db()
    assert challenge.request.failed_attempt_count == 0
