import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


def remove_legacy(apps, schema_editor):
    db = schema_editor.connection.alias

    def model(name):
        return apps.get_model("publications", name).objects.using(db)

    scopes = model("DatasetScopeState").filter(
        dataset_type_code="dangerous_goods", scope_type="DEPARTMENT"
    )
    scope_ids = list(scopes.values_list("pk", flat=True))
    publications = model("DatasetPublication").filter(scope_state_id__in=scope_ids)
    pub_ids = list(publications.values_list("pk", flat=True))
    jobs = model("PublicationJob").filter(scope_state_id__in=scope_ids)
    job_ids = list(jobs.values_list("pk", flat=True))
    batches = (
        apps.get_model("ingestion", "ImportBatch")
        .objects.using(db)
        .filter(domain="dangerous_goods")
    )
    batch_ids = list(batches.values_list("pk", flat=True))
    audit = apps.get_model("audit", "AuditEvent").objects.using(db)
    legacy_audit = audit.filter(
        Q(target_type="dataset_scope_state", target_uuid__in=scope_ids)
        | Q(target_type="dataset_publication", target_uuid__in=pub_ids)
        | Q(target_type="publication_job", target_uuid__in=job_ids)
        | Q(target_type="import_batch", target_uuid__in=batch_ids)
    )
    if legacy_audit.exists():
        # The migration role owns the table; the exclusive DDL lock prevents
        # concurrent writes until the immutable trigger is restored at commit.
        with schema_editor.connection.cursor() as cursor:
            cursor.execute("ALTER TABLE audit_event DISABLE TRIGGER audit_event_immutable")
        legacy_audit.delete()
        with schema_editor.connection.cursor() as cursor:
            cursor.execute("ALTER TABLE audit_event ENABLE TRIGGER audit_event_immutable")
    # Obsolete all cached envelopes containing a legacy delivery entry.
    manifests = model("SignedManifest")
    for manifest in manifests.exclude(status="OBSOLETE").iterator():
        payload = manifest.payload if isinstance(manifest.payload, dict) else {}
        if any(
            isinstance(entry, dict) and entry.get("type") == "dangerous_goods"
            for entry in payload.get("datasets", [])
        ):
            manifests.filter(pk=manifest.pk).update(status="OBSOLETE", payload={}, signature=None)
    activations = model("PublicationActivation").filter(
        Q(scope_state_id__in=scope_ids)
        | Q(publication_id__in=pub_ids)
        | Q(previous_publication_id__in=pub_ids)
    )
    if activations.exists():
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE publications_publicationactivation DISABLE TRIGGER "
                "publications_publicationactivation_immutable_guard"
            )
        activations.delete()
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE publications_publicationactivation ENABLE TRIGGER "
                "publications_publicationactivation_immutable_guard"
            )
    model("DatasetKeyGrant").filter(publication_id__in=pub_ids).delete()
    model("LegacyArtifactScopeSignatureUpgrade").filter(publication_id__in=pub_ids).delete()
    jobs.delete()
    model("DatasetSourceRevision").filter(scope_state_id__in=scope_ids).delete()
    scopes.update(current_published_publication=None, latest_built_publication=None)
    publications.update(supersedes=None)
    publications.delete()
    scopes.delete()
    batches.delete()
    model("DatasetTypeRegistry").filter(code="dangerous_goods").update(scope="system")
    model("DatasetScopeState").get_or_create(
        scope_type="SYSTEM", department=None, station=None, dataset_type_code="dangerous_goods"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("ingestion", "0004_dangerous_goods_import_domain"),
        ("publications", "0030_correct_legacy_bulk_rebuild_intent"),
        ("audit", "0004_restore_audit_truncate_immutability"),
        ("file_library", "0010_publication_capacity_reviews"),
    ]
    operations = [
        migrations.RunPython(remove_legacy, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="importbatch",
            name="department",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="import_batches",
                to="organizations.department",
            ),
        ),
        migrations.AddConstraint(
            model_name="importbatch",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        domain="dangerous_goods", department__isnull=True, station__isnull=True
                    )
                    | (~models.Q(domain="dangerous_goods") & models.Q(department__isnull=False))
                ),
                name="import_batch_domain_owner_shape",
            ),
        ),
    ]
