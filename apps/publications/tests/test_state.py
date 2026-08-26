"""Publication operational-state view-model tests."""

from datetime import UTC, datetime, timedelta

import pytest

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.publications.services import mark_dirty
from apps.publications.state import (
    BUILDING,
    CURRENT,
    FAILED,
    NEEDS_REBUILD,
    NOT_PUBLISHED,
    QUEUED,
    READY_TO_PUBLISH,
    UPDATE_QUEUED,
    compute_scope_state,
    scope_operational_states,
)

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def test_compute_state_precedence():
    assert (
        compute_scope_state(
            dirty=True,
            latest_status="READY_FOR_REVIEW",
            latest_built_status="READY_FOR_REVIEW",
            current_published=True,
            active_job_status="RUNNING",
            active_job_not_before=None,
            now=NOW,
        )
        == BUILDING
    )

    assert (
        compute_scope_state(
            dirty=True,
            latest_status=None,
            latest_built_status=None,
            current_published=True,
            active_job_status="PENDING",
            active_job_not_before=NOW + timedelta(minutes=1),
            now=NOW,
        )
        == UPDATE_QUEUED
    )

    assert (
        compute_scope_state(
            dirty=True,
            latest_status=None,
            latest_built_status=None,
            current_published=True,
            active_job_status="PENDING",
            active_job_not_before=NOW - timedelta(seconds=1),
            now=NOW,
        )
        == QUEUED
    )

    assert (
        compute_scope_state(
            dirty=True,
            latest_status="FAILED",
            latest_built_status=None,
            current_published=False,
            active_job_status=None,
            active_job_not_before=None,
            now=NOW,
            latest_source_fingerprint="a" * 64,
            current_source_fingerprint="a" * 64,
        )
        == FAILED
    )

    assert (
        compute_scope_state(
            dirty=True,
            latest_status="FAILED",
            latest_built_status=None,
            current_published=True,
            active_job_status=None,
            active_job_not_before=None,
            now=NOW,
            latest_source_fingerprint="a" * 64,
            current_source_fingerprint="b" * 64,
        )
        == NEEDS_REBUILD
    )

    assert (
        compute_scope_state(
            dirty=False,
            latest_status="READY_FOR_REVIEW",
            latest_built_status="READY_FOR_REVIEW",
            current_published=False,
            active_job_status=None,
            active_job_not_before=None,
            now=NOW,
        )
        == READY_TO_PUBLISH
    )

    assert (
        compute_scope_state(
            dirty=True,
            latest_status="PUBLISHED",
            latest_built_status="PUBLISHED",
            current_published=True,
            active_job_status=None,
            active_job_not_before=None,
            now=NOW,
        )
        == NEEDS_REBUILD
    )

    assert (
        compute_scope_state(
            dirty=False,
            latest_status="PUBLISHED",
            latest_built_status="PUBLISHED",
            current_published=True,
            active_job_status=None,
            active_job_not_before=None,
            now=NOW,
        )
        == CURRENT
    )

    assert (
        compute_scope_state(
            dirty=False,
            latest_status=None,
            latest_built_status=None,
            current_published=False,
            active_job_status=None,
            active_job_not_before=None,
            now=NOW,
        )
        == NOT_PUBLISHED
    )


@pytest.mark.django_db(transaction=True)
def test_scope_operational_states_derives_per_scope_state():
    admin = User.objects.create_user("state@example.test", "State Admin", "safe-password")
    department = Department.objects.create(name="State Dept", short_code="STD", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(department=department, name="Station A", short_code="STA")

    mark_dirty(
        department=department, station=station, dataset_type_code="station_personnel", actor=admin
    )

    rows = scope_operational_states(department)
    assert len(rows) == 1
    row = rows[0]
    assert row["dataset_name"] == "Station personnel"
    assert row["scope_label"] == "Station A"
    assert row["state"] == UPDATE_QUEUED
    assert row["state_label"] == "Update queued"
    assert row["distributed_version"] is None
    assert row["source_revision"] == 1
