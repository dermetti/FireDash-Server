"""Upgrade both erroneous 0029 backfills and fresh-install legacy queues."""

import importlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.audit.services import record_event
from apps.organizations.models import Department
from apps.publications.models import PublicationJob

CORRECTION = "0030_correct_legacy_bulk_rebuild_intent"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("upgrade", [False, True])
def test_legacy_queue_migration_fresh_and_upgrade_paths(upgrade):
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    before = "0029_publicationjob_allow_clean_rebuild" if upgrade else "0028_file_library_registry"
    targets = [(app, before) if app == "publications" else (app, name) for app, name in leaves]
    executor.migrate(targets)
    try:
        rolled_back = MigrationExecutor(connection)
        historical = rolled_back.loader.project_state(
            list(rolled_back.loader.applied_migrations)
        ).apps
        OldScope = historical.get_model("publications", "DatasetScopeState")
        OldJob = historical.get_model("publications", "PublicationJob")
        actor = User.objects.create_user("bulk-migration@example.test", "Admin", "password")
        jobs = {}
        for label, trigger, debounce, status, force in (
            ("ordinary_bulk", "BULK_REQUEST", False, "PENDING", False),
            ("running_bulk", "BULK_REQUEST", False, "RUNNING", False),
            ("explicit_user", "USER_REQUEST", False, "PENDING", True),
            ("promoted_build_now", "USER_REQUEST", True, "PENDING", False),
            ("confirmed_bulk_force", "BULK_REQUEST", False, "PENDING", True),
            ("terminal_bulk", "BULK_REQUEST", False, "SUCCEEDED", False),
        ):
            department = Department.objects.create(
                name=label, short_code=label[:10], created_by=actor
            )
            scope = OldScope.objects.create(
                department_id=department.pk,
                dataset_type_code="department_hydrants",
                source_revision=7,
            )
            values = dict(
                scope_state=scope,
                department_id=department.pk,
                dataset_type_code="department_hydrants",
                source_revision=7,
                requested_by_id=actor.pk,
                trigger_type=trigger,
                status=status,
                debounce_started_at=timezone.now() if debounce else None,
            )
            if upgrade:
                values["allow_clean_rebuild"] = not debounce and status in {"PENDING", "RUNNING"}
            job = OldJob.objects.create(**values)
            jobs[label] = (job.pk, force)
            if upgrade:
                # Represent a queue that existed when 0029 ran, rather than a
                # new job whose durable flag is already authoritative.
                cutoff = (
                    MigrationRecorder(connection)
                    .migration_qs.get(app="publications", name=before)
                    .applied
                )
                OldJob.objects.filter(pk=job.pk).update(created_at=cutoff - timedelta(seconds=1))
            if label == "confirmed_bulk_force":
                record_event(
                    action="publication.rebuild_requested",
                    actor_user=actor,
                    department=department,
                    target_type="dataset_scope_state",
                    target_uuid=scope.pk,
                    metadata={"dataset_type_code": "department_hydrants", "source_revision": 7},
                )
            if label == "ordinary_bulk":
                # Even a real force event for another tenant/scope is irrelevant.
                record_event(
                    action="publication.rebuild_requested",
                    actor_user=actor,
                    department=department,
                    target_type="dataset_scope_state",
                    target_uuid=actor.pk,
                    metadata={"dataset_type_code": "department_hydrants", "source_revision": 7},
                )
        executor = MigrationExecutor(connection)
        executor.migrate(leaves)
        for label, (pk, expected) in jobs.items():
            job = PublicationJob.objects.get(pk=pk)
            assert job.allow_clean_rebuild == expected, label
            assert job.status == (
                "RUNNING"
                if label == "running_bulk"
                else "SUCCEEDED"
                if label == "terminal_bulk"
                else "PENDING"
            )
        corrections = AuditEvent.objects.filter(action="publication.legacy_bulk_intent_corrected")
        assert set(corrections.values_list("target_uuid", flat=True)) == {
            jobs["ordinary_bulk"][0],
            jobs["running_bulk"][0],
        }
        migration = importlib.import_module(f"apps.publications.migrations.{CORRECTION}")
        with transaction.atomic():
            migration.correct_legacy_bulk_requests(apps, SimpleNamespace(connection=connection))
        assert corrections.count() == 2
        # A new explicit bulk-force enqueue after 0029 must never be inferred
        # away, even without a legacy request_rebuild audit event.
        job = PublicationJob.objects.get(pk=jobs["ordinary_bulk"][0])
        PublicationJob.objects.filter(pk=job.pk).update(
            created_at=timezone.now(), allow_clean_rebuild=True
        )
        with transaction.atomic():
            migration.correct_legacy_bulk_requests(apps, SimpleNamespace(connection=connection))
        job.refresh_from_db()
        assert job.allow_clean_rebuild
    finally:
        MigrationExecutor(connection).migrate(leaves)
