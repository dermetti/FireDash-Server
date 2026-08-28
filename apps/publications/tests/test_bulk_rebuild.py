"""Bulk "Rebuild affected datasets" eligibility and deduplication tests."""

import uuid
from time import time
from urllib.parse import parse_qs, urlparse

import pytest
from django.urls import reverse
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.publications.builders import build_source_payload, source_fingerprint_for_payload
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.paths import publication_artifact_relative_path
from apps.publications.registry import get_dataset_definition
from apps.publications.services import bulk_request_rebuilds, mark_dirty


@pytest.fixture
def bulk_context(db):
    admin = User.objects.create_user("bulk@example.test", "Bulk Admin", "safe-password")
    department = Department.objects.create(name="Bulk Dept", short_code="BLK", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(department=department, name="Station A", short_code="STA")
    station_b = Station.objects.create(department=department, name="Station B", short_code="STB")
    return admin, department, station, station_b


def _published(department, scope, station=None, version_number=1):
    publication_id = uuid.uuid4()
    source_fingerprint = source_fingerprint_for_payload(
        build_source_payload(
            definition=get_dataset_definition(scope.dataset_type_code),
            department=department,
            station=station,
        )
    )
    return DatasetPublication.objects.create(
        id=publication_id,
        department=department,
        station=station,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        version_number=version_number,
        schema_version=1,
        source_revision=1,
        source_fingerprint=source_fingerprint,
        status=DatasetPublication.Status.PUBLISHED,
        artifact_ready=True,
        artifact_status=DatasetPublication.ArtifactStatus.READY,
        artifact_path=publication_artifact_relative_path(
            department_id=department.id, publication_id=publication_id
        ),
        artifact_size=1,
        artifact_sha256="a" * 64,
        artifact_nonce=b"n" * 12,
        artifact_wrapped_cek=b"w" * 40,
        artifact_encryption_algorithm="AES-256-GCM",
        artifact_wrapping_algorithm="AES-KW-RFC3394",
        artifact_kek_version="1",
        artifact_signature=b"s" * 64,
        artifact_signature_algorithm="Ed25519",
        artifact_signing_key_version="1",
    )


def _set_current(scope, publication):
    scope.latest_built_publication = publication
    scope.current_published_publication = publication
    scope.current_source_fingerprint = publication.source_fingerprint
    scope.save(
        update_fields=(
            "latest_built_publication",
            "current_published_publication",
            "current_source_fingerprint",
        )
    )


def _current_token(device: TOTPDevice) -> str:
    token = TOTP(
        device.bin_key,
        step=device.step,
        t0=device.t0,
        digits=device.digits,
        drift=device.drift,
    ).token()
    return f"{token:0{device.digits}d}"


@pytest.mark.django_db(transaction=True)
def test_bulk_rebuild_requests_only_affected_scopes(bulk_context):
    admin, department, station, station_b = bulk_context

    DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_fire_plans"
    )

    failed_scope = DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_hydrants"
    )
    DatasetPublication.objects.create(
        department=department,
        dataset_type_code="department_hydrants",
        scope_state=failed_scope,
        version_number=1,
        schema_version=1,
        source_revision=1,
        status=DatasetPublication.Status.FAILED,
    )

    current_scope = DatasetScopeState.objects.create(
        department=department, station=station, dataset_type_code="station_personnel"
    )
    current = _published(department, current_scope, station=station)
    _set_current(current_scope, current)

    # An active pending job must be left untouched and counted as already queued.
    mark_dirty(
        department=department,
        station=station_b,
        dataset_type_code="station_personnel",
        actor=admin,
    )

    result = bulk_request_rebuilds(actor=admin, department=department)

    assert result == {
        "requested": 3,
        "already_queued": 1,
        "already_current": 1,
        "created": 2,
        "promoted": 1,
        "already_running": 0,
        "skipped_current": 1,
    }


@pytest.mark.django_db(transaction=True)
def test_bulk_rebuild_does_not_request_current_scopes(bulk_context):
    admin, department, station, _ = bulk_context
    current_scope = DatasetScopeState.objects.create(
        department=department, station=station, dataset_type_code="station_personnel"
    )
    current = _published(department, current_scope, station=station)
    _set_current(current_scope, current)

    result = bulk_request_rebuilds(actor=admin, department=department)

    assert result == {
        "requested": 0,
        "already_queued": 0,
        "already_current": 1,
        "created": 0,
        "promoted": 0,
        "already_running": 0,
        "skipped_current": 1,
    }


@pytest.mark.django_db(transaction=True)
def test_bulk_promotes_scheduled_intent_and_records_one_summary_audit_event(bulk_context):
    admin, department, station, _ = bulk_context
    mark_dirty(
        actor=admin,
        department=department,
        station=station,
        dataset_type_code="station_personnel",
    )

    result = bulk_request_rebuilds(actor=admin, department=department)

    job = PublicationJob.objects.get(department=department)
    assert job.trigger_type == PublicationJob.TriggerType.BULK_REQUEST
    assert job.not_before is not None
    assert result["promoted"] == 1
    events = AuditEvent.objects.filter(
        department=department, action="publication.bulk_rebuild_requested"
    )
    assert events.count() == 1
    assert events.get().metadata == {
        "created": 0,
        "promoted": 1,
        "already_running": 0,
        "skipped_current": 0,
    }


@pytest.mark.django_db(transaction=True)
def test_bulk_rebuild_reauthentication_returns_to_list_before_second_post(client, bulk_context):
    admin, department, _, _ = bulk_context
    admin.mfa_enabled = True
    admin.save(update_fields=("mfa_enabled",))
    device = TOTPDevice.objects.create(
        user=admin,
        name="default",
        key="3132333435363738393031323334353637383930",
        confirmed=True,
    )
    DatasetScopeState.objects.create(
        department=department, dataset_type_code="department_fire_plans"
    )
    client.force_login(admin)
    action_url = reverse("publications-bulk-rebuild", args=(department.id,))
    return_url = reverse("publications-list", args=(department.id,))

    pending_response = client.post(action_url)

    assert pending_response.status_code == 302
    token = parse_qs(urlparse(pending_response.url).query)["pending"][0]
    assert client.session["pending_reauth"]["url"] == action_url
    assert client.session["pending_reauth"]["method"] == "POST"
    assert client.session["pending_reauth"]["return_url"] == return_url
    assert not PublicationJob.objects.filter(scope_state__department=department).exists()

    reauth_response = client.post(
        reverse("accounts-reauthenticate"),
        {"pending": token, "token": _current_token(device)},
    )

    assert reauth_response.status_code == 302
    assert reauth_response.url == return_url
    assert client.get(reauth_response.url).status_code == 200
    assert not PublicationJob.objects.filter(scope_state__department=department).exists()
    assert "pending_reauth" not in client.session
    assert client.session["recent_reauthentication_at"] >= time() - 5

    second_action_response = client.post(action_url)

    assert second_action_response.status_code == 302
    assert second_action_response.url == return_url
    assert PublicationJob.objects.filter(scope_state__department=department).exists()
