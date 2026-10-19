"""Conservative, independently evaluated legacy-default conversion.

No quota-change events means untouched only when the recorded migration history
shows append-only auditing predating libraries. Unknown/unscoped audit evidence
blocks conversion; historical per-library events also protect the shared policy.
Out-of-band database writes have no provenance and cannot be inferred here.
"""

from django.conf import settings
from django.db import migrations, models
from django.db.migrations.recorder import MigrationRecorder

import apps.file_library.models

LEGACY = 1_073_741_824
FIELDS = ("department_files_bytes", "station_files_bytes")


def backfill(apps, schema_editor):
    alias = schema_editor.connection.alias
    Policy = apps.get_model("file_library", "DepartmentQuotaPolicy")
    Library = apps.get_model("file_library", "Library")
    Audit = apps.get_model("audit", "AuditEvent")
    history = {
        (row.app, row.name): row.applied
        for row in MigrationRecorder(schema_editor.connection).migration_qs.filter(
            app__in=("file_library", "audit")
        )
    }
    initial = history.get(("file_library", "0001_initial"))
    immutable = history.get(("audit", "0002_immutable_audit_event"))
    shared = history.get(("file_library", "0008_department_quota_policy"))
    defaults = (
        settings.FILE_LIBRARY_DEPARTMENT_QUOTA_BYTES,
        settings.FILE_LIBRARY_STATION_QUOTA_BYTES,
    )
    confident_history = (
        initial is not None
        and immutable is not None
        and shared is not None
        and immutable <= initial <= shared
        and settings.FILE_LIBRARY_QUOTA_BYTES == LEGACY
    )
    # Read audit evidence only after waiting for in-flight quota writers. Those
    # writers hold this row until their audit INSERT and policy update commit.
    # Newly inserted policies are deliberately outside this historical snapshot.
    policies = list(Policy.objects.using(alias).select_for_update().order_by("pk"))
    libraries = {
        str(pk): (department_id or station_department_id, FIELDS[0] if department_id else FIELDS[1])
        for pk, department_id, station_department_id in Library.objects.using(alias).values_list(
            "id", "department_id", "station__department_id"
        )
    }
    blocked = set()
    unknown_departments = set()
    unknown_global = False
    for event in Audit.objects.using(alias).filter(action="file_library.quota_changed").iterator():
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        field = metadata.get("policy")
        if (
            event.department_id is not None
            and event.target_type == "department_quota_policy"
            and event.target_uuid == event.department_id
            and field in FIELDS
            and event.station_id is None
            and shared is not None
            and event.timestamp >= shared
        ):
            blocked.add((event.department_id, field))
        elif event.target_type == "library" and str(event.target_uuid) in libraries:
            department_id, field = libraries[str(event.target_uuid)]
            if event.department_id == department_id:
                blocked.add((department_id, field))
            else:
                unknown_global = True
        elif event.department_id is not None:
            unknown_departments.add(event.department_id)
        else:
            unknown_global = True

    counts = {field: {"updated": 0, "preserved": 0} for field in FIELDS}
    for policy in policies:
        for field, default, standard in zip(
            FIELDS, defaults, (5_000_000_000, 2_000_000_000), strict=True
        ):
            if (
                confident_history
                and default == standard
                and not unknown_global
                and policy.department_id not in unknown_departments
                and (policy.department_id, field) not in blocked
                and getattr(policy, field) == LEGACY
            ):
                Policy.objects.using(alias).filter(pk=policy.pk, **{field: LEGACY}).update(
                    **{field: default}
                )
                counts[field]["updated"] += 1
            else:
                counts[field]["preserved"] += 1
    for field in FIELDS:
        print(
            f"Quota defaults: {field}: {counts[field]['updated']} updated, {counts[field]['preserved']} preserved."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("file_library", "0008_department_quota_policy"),
        ("audit", "0004_restore_audit_truncate_immutability"),
    ]

    operations = [
        migrations.AlterField(
            model_name="departmentquotapolicy",
            name="department_files_bytes",
            field=models.PositiveBigIntegerField(
                default=apps.file_library.models.default_department_quota
            ),
        ),
        migrations.AlterField(
            model_name="departmentquotapolicy",
            name="station_files_bytes",
            field=models.PositiveBigIntegerField(
                default=apps.file_library.models.default_station_quota
            ),
        ),
        # Deliberately do not reverse data: without provenance, reverting a value
        # could overwrite an administrator's subsequent explicit configuration.
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
