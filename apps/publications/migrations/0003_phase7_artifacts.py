from django.db import migrations, models
from django.db.models import Q


def demote_metadata_only_publications(apps, schema_editor):
    Publication = apps.get_model("publications", "DatasetPublication")
    Scope = apps.get_model("publications", "DatasetScopeState")
    # Phase 6 publications have no encrypted artifact and cannot remain active.
    Publication.objects.filter(status="PUBLISHED", artifact_ready=False).update(status="OBSOLETE")
    Scope.objects.filter(current_published_publication__artifact_ready=False).update(
        current_published_publication=None
    )


def add_artifact_guards(apps, schema_editor):
    table = "publications_datasetpublication"
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(f"""
            CREATE OR REPLACE FUNCTION publications_guard_artifact() RETURNS trigger AS $$
            BEGIN
              IF NEW.status IN ('READY_FOR_REVIEW', 'PUBLISHED') AND
                 (NEW.artifact_status <> 'READY' OR NEW.artifact_path = '' OR NEW.artifact_size IS NULL OR
                  NEW.artifact_sha256 = '' OR NEW.artifact_nonce IS NULL OR NEW.artifact_wrapped_cek IS NULL OR
                  NEW.artifact_encryption_algorithm <> 'AES-256-GCM' OR NEW.artifact_wrapping_algorithm <> 'AES-KW-RFC3394' OR
                  NEW.artifact_kek_version = '' OR NEW.artifact_signature IS NULL OR NEW.artifact_signature_algorithm <> 'Ed25519') THEN
                RAISE EXCEPTION 'Review-ready and published publications require complete ready artifacts';
              END IF;
              IF OLD.artifact_status = 'READY' AND (NEW.artifact_path, NEW.artifact_size, NEW.artifact_sha256, NEW.artifact_nonce, NEW.artifact_wrapped_cek, NEW.artifact_encryption_algorithm, NEW.artifact_wrapping_algorithm, NEW.artifact_kek_version, NEW.artifact_signature, NEW.artifact_signature_algorithm)
                 IS DISTINCT FROM (OLD.artifact_path, OLD.artifact_size, OLD.artifact_sha256, OLD.artifact_nonce, OLD.artifact_wrapped_cek, OLD.artifact_encryption_algorithm, OLD.artifact_wrapping_algorithm, OLD.artifact_kek_version, OLD.artifact_signature, OLD.artifact_signature_algorithm) THEN
                RAISE EXCEPTION 'Ready artifact metadata is immutable';
              END IF;
              RETURN NEW;
            END; $$ LANGUAGE plpgsql;
            CREATE TRIGGER publications_datasetpublication_artifact_guard BEFORE UPDATE ON {table}
              FOR EACH ROW EXECUTE FUNCTION publications_guard_artifact();
        """)
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute(f"""
            CREATE TRIGGER publications_datasetpublication_artifact_guard BEFORE UPDATE ON {table}
            WHEN (NEW.status IN ('READY_FOR_REVIEW','PUBLISHED') AND (NEW.artifact_status <> 'READY' OR NEW.artifact_path = '' OR NEW.artifact_size IS NULL OR NEW.artifact_sha256 = '' OR NEW.artifact_nonce IS NULL OR NEW.artifact_wrapped_cek IS NULL OR NEW.artifact_encryption_algorithm <> 'AES-256-GCM' OR NEW.artifact_wrapping_algorithm <> 'AES-KW-RFC3394' OR NEW.artifact_kek_version = '' OR NEW.artifact_signature IS NULL OR NEW.artifact_signature_algorithm <> 'Ed25519'))
              OR (OLD.artifact_status = 'READY' AND (NEW.artifact_path <> OLD.artifact_path OR NEW.artifact_size <> OLD.artifact_size OR NEW.artifact_sha256 <> OLD.artifact_sha256 OR NEW.artifact_nonce <> OLD.artifact_nonce OR NEW.artifact_wrapped_cek <> OLD.artifact_wrapped_cek OR NEW.artifact_encryption_algorithm <> OLD.artifact_encryption_algorithm OR NEW.artifact_wrapping_algorithm <> OLD.artifact_wrapping_algorithm OR NEW.artifact_kek_version <> OLD.artifact_kek_version OR NEW.artifact_signature <> OLD.artifact_signature OR NEW.artifact_signature_algorithm <> OLD.artifact_signature_algorithm))
            BEGIN SELECT RAISE(ABORT, 'Invalid or mutable artifact metadata'); END;
        """)


def remove_artifact_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS publications_datasetpublication_artifact_guard ON publications_datasetpublication;"
        )
        schema_editor.execute("DROP FUNCTION IF EXISTS publications_guard_artifact();")
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS publications_datasetpublication_artifact_guard;"
        )


class Migration(migrations.Migration):
    dependencies = [("publications", "0002_phase6_registry_features")]
    operations = [
        migrations.AddField(
            model_name="datasetpublication",
            name="artifact_status",
            field=models.CharField(
                choices=[("PENDING", "Pending"), ("READY", "Ready"), ("FAILED", "Failed")],
                default="PENDING",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="datasetpublication",
            name="artifact_path",
            field=models.CharField(blank=True, max_length=512),
        ),
        migrations.AddField(
            model_name="datasetpublication",
            name="artifact_size",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="datasetpublication",
            name="artifact_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="datasetpublication",
            name="artifact_nonce",
            field=models.BinaryField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="datasetpublication",
            name="artifact_wrapped_cek",
            field=models.BinaryField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="datasetpublication",
            name="artifact_encryption_algorithm",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="datasetpublication",
            name="artifact_wrapping_algorithm",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="datasetpublication",
            name="artifact_kek_version",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="datasetpublication",
            name="artifact_signature",
            field=models.BinaryField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="datasetpublication",
            name="artifact_signature_algorithm",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddConstraint(
            model_name="datasetpublication",
            constraint=models.CheckConstraint(
                condition=~Q(status__in=("READY_FOR_REVIEW", "PUBLISHED"))
                | Q(artifact_status="READY"),
                name="review_publication_requires_ready_artifact",
            ),
        ),
        migrations.RunPython(demote_metadata_only_publications, migrations.RunPython.noop),
        migrations.RunPython(add_artifact_guards, remove_artifact_guards),
    ]
