"""0029 inferred force intent from BULK_REQUEST without durable evidence."""

import uuid

from django.db import migrations


def correct_legacy_bulk_requests(apps, schema_editor):
    alias = schema_editor.connection.alias
    # Do not reinterpret jobs created by code that already persisted explicit
    # force intent. On fresh installs this timestamp is recorded before 0030.
    from django.db.migrations.recorder import MigrationRecorder

    applied = (
        MigrationRecorder(schema_editor.connection)
        .migration_qs.filter(app="publications", name="0029_publicationjob_allow_clean_rebuild")
        .values_list("applied", flat=True)
        .first()
    )
    if applied is None:
        raise RuntimeError("0029 must be recorded before correcting legacy bulk requests.")
    jobs = apps.get_model("publications", "PublicationJob").objects.using(alias)
    events = apps.get_model("audit", "AuditEvent").objects.using(alias)
    for job in jobs.select_for_update().filter(
        status__in=("PENDING", "RUNNING"),
        trigger_type="BULK_REQUEST",
        debounce_started_at__isnull=True,
        allow_clean_rebuild=True,
        created_at__lte=applied,
    ):
        # request_rebuild records intent before enqueueing. Coalescing may
        # replace the trigger, requester and revision, so none of those can
        # disprove explicit force intent during this job's queue lifetime.
        previous = (
            jobs.filter(scope_state_id=job.scope_state_id, created_at__lt=job.created_at)
            .exclude(status__in=("PENDING", "RUNNING"))
            .order_by("-created_at", "id")
            .first()
        )
        evidence = events.filter(
            action="publication.rebuild_requested",
            target_type="dataset_scope_state",
            target_uuid=job.scope_state_id,
            department_id=job.department_id,
            station_id=job.station_id,
            metadata__dataset_type_code=job.dataset_type_code,
        )
        if previous:
            evidence = evidence.filter(timestamp__gte=previous.completed_at or previous.created_at)
        if evidence.exists():
            continue
        jobs.filter(pk=job.pk).update(allow_clean_rebuild=False)
        events.create(
            action="publication.legacy_bulk_intent_corrected",
            department_id=job.department_id,
            station_id=job.station_id,
            target_type="publication_job",
            target_uuid=job.pk,
            request_id=uuid.uuid4(),
            metadata={
                "migration": "publications.0030",
                "allow_clean_rebuild_before": True,
                "allow_clean_rebuild_after": False,
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("publications", "0029_publicationjob_allow_clean_rebuild"),
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(correct_legacy_bulk_requests, migrations.RunPython.noop),
    ]
