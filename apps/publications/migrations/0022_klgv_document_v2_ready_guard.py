from django.db import migrations

_FORWARD = """
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
    RAISE EXCEPTION 'Ready publication artifact metadata is immutable';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


class Migration(migrations.Migration):
    dependencies = [("publications", "0021_klgv_document_manifest_v2")]

    operations = [migrations.RunSQL(_FORWARD, migrations.RunSQL.noop)]
