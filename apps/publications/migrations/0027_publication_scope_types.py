from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q


def classify_existing_scopes(apps, schema_editor):
    for model_name in ("DatasetScopeState", "DatasetPublication", "PublicationJob"):
        model = apps.get_model("publications", model_name)
        model.objects.filter(station__isnull=False).update(scope_type="STATION")
        model.objects.filter(station__isnull=True).update(scope_type="DEPARTMENT")
    publication = apps.get_model("publications", "DatasetPublication")
    publication.objects.filter(artifact_status="READY", artifact_path__gt="").update(
        artifact_signature_algorithm="Ed25519-legacy-scope"
    )


def update_artifact_path_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            """
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
        migrations.RunPython(classify_existing_scopes, migrations.RunPython.noop),
        migrations.RunPython(update_artifact_path_guard, migrations.RunPython.noop),
        migrations.AddConstraint(model_name="datasetpublication", constraint=models.CheckConstraint(name="dataset_publication_owner_shape", condition=Q(scope_type="SYSTEM", department__isnull=True, station__isnull=True) | Q(scope_type="DEPARTMENT", department__isnull=False, station__isnull=True) | Q(scope_type="STATION", department__isnull=False, station__isnull=False))),
        migrations.AddConstraint(model_name="datasetscopestate", constraint=models.CheckConstraint(name="dataset_scope_owner_shape", condition=Q(scope_type="SYSTEM", department__isnull=True, station__isnull=True) | Q(scope_type="DEPARTMENT", department__isnull=False, station__isnull=True) | Q(scope_type="STATION", department__isnull=False, station__isnull=False))),
        migrations.AddConstraint(model_name="publicationjob", constraint=models.CheckConstraint(name="publication_job_owner_shape", condition=Q(scope_type="SYSTEM", department__isnull=True, station__isnull=True) | Q(scope_type="DEPARTMENT", department__isnull=False, station__isnull=True) | Q(scope_type="STATION", department__isnull=False, station__isnull=False))),
        migrations.AddConstraint(model_name="datasetpublication", constraint=models.UniqueConstraint(fields=("scope_type", "department", "station", "dataset_type_code", "version_number"), nulls_distinct=False, name="unique_dataset_publication_version")),
        migrations.AddConstraint(model_name="datasetpublication", constraint=models.UniqueConstraint(fields=("scope_type", "department", "station", "dataset_type_code"), condition=Q(status="PUBLISHED"), nulls_distinct=False, name="one_current_published_dataset_publication")),
        migrations.AddConstraint(model_name="datasetscopestate", constraint=models.UniqueConstraint(fields=("scope_type", "department", "station", "dataset_type_code"), nulls_distinct=False, name="unique_dataset_scope_state")),
        migrations.AddConstraint(model_name="publicationjob", constraint=models.UniqueConstraint(fields=("scope_type", "department", "station", "dataset_type_code"), condition=Q(status__in=("PENDING", "RUNNING")), nulls_distinct=False, name="one_active_publication_job_per_scope")),
    ]
