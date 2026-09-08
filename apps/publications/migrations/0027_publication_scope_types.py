import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


def classify_existing_scopes(apps, schema_editor):
    for model_name in ("DatasetScopeState", "DatasetPublication", "PublicationJob"):
        model = apps.get_model("publications", model_name)
        model.objects.filter(station__isnull=False).update(scope_type="STATION")
        model.objects.filter(station__isnull=True).update(scope_type="DEPARTMENT")
    publication = apps.get_model("publications", "DatasetPublication")
    marker = apps.get_model("publications", "LegacyArtifactScopeSignatureUpgrade")
    marker.objects.bulk_create(
        [
            marker(publication_id=publication_id)
            for publication_id in publication.objects.filter(
                artifact_status="READY",
                artifact_path__gt="",
                scope_state__delivery_format="V1_ARTIFACT",
            ).values_list("id", flat=True)
        ],
        ignore_conflicts=True,
    )


def update_artifact_path_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            """
            CREATE OR REPLACE FUNCTION publications_guard_artifact() RETURNS trigger AS $$
            BEGIN
              IF NEW.status IN ('READY_FOR_REVIEW','PUBLISHED')
                 AND NOT (NEW.dataset_type_code = 'department_klgv_plans' AND NEW.schema_version = 2)
                 AND (NEW.artifact_status <> 'READY' OR NEW.artifact_path = '' OR NEW.artifact_size IS NULL OR
                      NEW.artifact_sha256 = '' OR NEW.artifact_nonce IS NULL OR NEW.artifact_wrapped_cek IS NULL OR
                      NEW.artifact_encryption_algorithm <> 'AES-256-GCM' OR NEW.artifact_wrapping_algorithm <> 'AES-KW-RFC3394' OR
                      NEW.artifact_kek_version = '' OR NEW.artifact_signature IS NULL OR NEW.artifact_signature_algorithm <> 'Ed25519') THEN
                RAISE EXCEPTION 'Review-ready and published publications require complete ready artifacts';
              END IF;
              IF OLD.artifact_status = 'READY' AND (NEW.artifact_path, NEW.artifact_size, NEW.artifact_sha256, NEW.artifact_nonce, NEW.artifact_wrapped_cek, NEW.artifact_encryption_algorithm, NEW.artifact_wrapping_algorithm, NEW.artifact_kek_version, NEW.artifact_signature, NEW.artifact_signature_algorithm)
                 IS DISTINCT FROM (OLD.artifact_path, OLD.artifact_size, OLD.artifact_sha256, OLD.artifact_nonce, OLD.artifact_wrapped_cek, OLD.artifact_encryption_algorithm, OLD.artifact_wrapping_algorithm, OLD.artifact_kek_version, OLD.artifact_signature, OLD.artifact_signature_algorithm) THEN
                IF NOT (
                  EXISTS (SELECT 1 FROM publications_legacyartifactscopesignatureupgrade WHERE publication_id = OLD.id)
                  AND NEW.artifact_path IS NOT DISTINCT FROM OLD.artifact_path
                  AND NEW.artifact_size IS NOT DISTINCT FROM OLD.artifact_size
                  AND NEW.artifact_sha256 IS NOT DISTINCT FROM OLD.artifact_sha256
                  AND NEW.artifact_nonce IS NOT DISTINCT FROM OLD.artifact_nonce
                  AND NEW.artifact_wrapped_cek IS NOT DISTINCT FROM OLD.artifact_wrapped_cek
                  AND NEW.artifact_encryption_algorithm IS NOT DISTINCT FROM OLD.artifact_encryption_algorithm
                  AND NEW.artifact_wrapping_algorithm IS NOT DISTINCT FROM OLD.artifact_wrapping_algorithm
                  AND NEW.artifact_kek_version IS NOT DISTINCT FROM OLD.artifact_kek_version
                  AND NEW.artifact_signature_algorithm IS NOT DISTINCT FROM OLD.artifact_signature_algorithm
                  AND NEW.artifact_signature IS DISTINCT FROM OLD.artifact_signature
                ) THEN
                  RAISE EXCEPTION 'Ready publication artifact metadata is immutable';
                END IF;
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE OR REPLACE FUNCTION publications_guard_phase7_closeout() RETURNS trigger AS $$
            BEGIN
              IF NEW.artifact_path <> '' AND NEW.artifact_path <> (
                 CASE WHEN NEW.scope_type = 'SYSTEM'
                   THEN 'system/' || NEW.id::text || '/artifact.bin'
                   ELSE NEW.department_id::text || '/' || NEW.id::text || '/artifact.bin'
                 END) THEN
                RAISE EXCEPTION 'Artifact path must be a generated publication path';
              END IF;
              RETURN NEW;
            END; $$ LANGUAGE plpgsql;
            """
        )


def restore_legacy_artifact_path_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            """
            CREATE OR REPLACE FUNCTION publications_guard_phase7_closeout() RETURNS trigger AS $$
            BEGIN
              IF NEW.artifact_path <> '' AND NEW.artifact_path <>
                 NEW.department_id::text || '/' || NEW.id::text || '/artifact.bin' THEN
                RAISE EXCEPTION 'Artifact path must be a generated publication path';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )


class Migration(migrations.Migration):
    dependencies = [("publications", "0026_rename_publications_scope_s_631ca6_idx_pub_src_scope_rev_idx")]

    operations = [
        migrations.RemoveConstraint(model_name="datasetpublication", name="unique_dataset_publication_version"),
        migrations.RemoveConstraint(model_name="datasetpublication", name="one_current_published_dataset_publication"),
        migrations.RemoveConstraint(model_name="datasetscopestate", name="unique_dataset_scope_state"),
        migrations.RemoveConstraint(model_name="publicationjob", name="one_active_publication_job_per_scope"),
        migrations.AddField(model_name="datasetpublication", name="scope_type", field=models.CharField(max_length=16, choices=[("SYSTEM", "System"), ("DEPARTMENT", "Department"), ("STATION", "Station")], default="DEPARTMENT")),
        migrations.AddField(model_name="datasetscopestate", name="scope_type", field=models.CharField(max_length=16, choices=[("SYSTEM", "System"), ("DEPARTMENT", "Department"), ("STATION", "Station")], default="DEPARTMENT")),
        migrations.AddField(model_name="publicationjob", name="scope_type", field=models.CharField(max_length=16, choices=[("SYSTEM", "System"), ("DEPARTMENT", "Department"), ("STATION", "Station")], default="DEPARTMENT")),
        migrations.AlterField(model_name="datasetpublication", name="department", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="dataset_publications", to="organizations.department")),
        migrations.AlterField(model_name="datasetscopestate", name="department", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="dataset_scopes", to="organizations.department")),
        migrations.AlterField(model_name="publicationjob", name="department", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="publication_jobs", to="organizations.department")),
        migrations.CreateModel(
            name="LegacyArtifactScopeSignatureUpgrade",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("publication", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="legacy_scope_signature_upgrade", to="publications.datasetpublication")),
            ],
        ),
        migrations.RunPython(classify_existing_scopes, migrations.RunPython.noop),
        migrations.RunPython(update_artifact_path_guard, restore_legacy_artifact_path_guard),
        migrations.AddConstraint(model_name="datasetpublication", constraint=models.CheckConstraint(name="dataset_publication_owner_shape", condition=Q(scope_type="SYSTEM", department__isnull=True, station__isnull=True) | Q(scope_type="DEPARTMENT", department__isnull=False, station__isnull=True) | Q(scope_type="STATION", department__isnull=False, station__isnull=False))),
        migrations.AddConstraint(model_name="datasetscopestate", constraint=models.CheckConstraint(name="dataset_scope_owner_shape", condition=Q(scope_type="SYSTEM", department__isnull=True, station__isnull=True) | Q(scope_type="DEPARTMENT", department__isnull=False, station__isnull=True) | Q(scope_type="STATION", department__isnull=False, station__isnull=False))),
        migrations.AddConstraint(model_name="publicationjob", constraint=models.CheckConstraint(name="publication_job_owner_shape", condition=Q(scope_type="SYSTEM", department__isnull=True, station__isnull=True) | Q(scope_type="DEPARTMENT", department__isnull=False, station__isnull=True) | Q(scope_type="STATION", department__isnull=False, station__isnull=False))),
        migrations.AddConstraint(model_name="datasetpublication", constraint=models.UniqueConstraint(fields=("scope_type", "department", "station", "dataset_type_code", "version_number"), nulls_distinct=False, name="unique_dataset_publication_version")),
        migrations.AddConstraint(model_name="datasetpublication", constraint=models.UniqueConstraint(fields=("scope_type", "department", "station", "dataset_type_code"), condition=Q(status="PUBLISHED"), nulls_distinct=False, name="one_current_published_dataset_publication")),
        migrations.AddConstraint(model_name="datasetscopestate", constraint=models.UniqueConstraint(fields=("scope_type", "department", "station", "dataset_type_code"), nulls_distinct=False, name="unique_dataset_scope_state")),
        migrations.AddConstraint(model_name="publicationjob", constraint=models.UniqueConstraint(fields=("scope_type", "department", "station", "dataset_type_code"), condition=Q(status__in=("PENDING", "RUNNING")), nulls_distinct=False, name="one_active_publication_job_per_scope")),
    ]
