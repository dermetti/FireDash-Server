"""Promote Fire Plans and require complete generic generations at closeout."""

from django.db import migrations

SQL = r"""
CREATE FUNCTION publications_document_generation_complete(pub publications_datasetpublication)
RETURNS boolean AS $$
DECLARE entries jsonb; manifest publications_documentgenerationmanifest; metadata_key text;
BEGIN
 entries := pub.source_snapshot -> CASE pub.dataset_type_code
   WHEN 'department_fire_plans' THEN 'fire_plans'
   WHEN 'department_klgv_plans' THEN 'klgv_plans' ELSE 'documents' END;
 metadata_key := CASE pub.dataset_type_code WHEN 'department_fire_plans' THEN 'fire_plan'
   WHEN 'department_klgv_plans' THEN 'klgv_plan' ELSE 'file' END;
 SELECT * INTO manifest FROM publications_documentgenerationmanifest WHERE publication_id=pub.id;
 IF pub.artifact_status <> 'READY' OR pub.artifact_path <> '' OR
    jsonb_typeof(entries) IS DISTINCT FROM 'array' OR manifest.publication_id IS NULL OR
    manifest.signature_algorithm <> 'Ed25519' OR octet_length(manifest.signature) <> 64 OR
    manifest.signing_key_version = '' OR
    manifest.payload->>'format' IS DISTINCT FROM 'document-generation-v2' OR
    manifest.payload->>'dataset_type' IS DISTINCT FROM pub.dataset_type_code OR
    manifest.payload->>'publication_id' IS DISTINCT FROM pub.id::text OR
    manifest.payload->'schema_version' IS DISTINCT FROM '2'::jsonb OR
    manifest.payload->>'version' IS DISTINCT FROM pub.version_number::text OR
    jsonb_typeof(manifest.payload->'documents') IS DISTINCT FROM 'array' OR
    NOT EXISTS (SELECT 1 FROM publications_documentgenerationkey WHERE publication_id=pub.id
      AND octet_length(wrapped_key)=40 AND wrapping_algorithm='AES-KW-RFC3394' AND kek_version<>'') THEN
   RETURN false;
 END IF;
 IF jsonb_array_length(entries) <> jsonb_array_length(manifest.payload->'documents') OR
    jsonb_array_length(entries) <> (SELECT count(*) FROM publications_publicationdocumentartifactreference WHERE publication_id=pub.id) OR
    jsonb_array_length(entries) <> (SELECT count(DISTINCT e->>'id') FROM jsonb_array_elements(entries) e) OR
    EXISTS (
      SELECT 1 FROM jsonb_array_elements(entries) e WHERE NOT EXISTS (
        SELECT 1 FROM publications_publicationdocumentartifactreference r
        JOIN publications_documentartifact a ON a.id=r.document_artifact_id
        WHERE r.publication_id=pub.id AND r.canonical_document_id::text=e->>'id'
          AND a.canonical_document_id=r.canonical_document_id AND a.dataset_type_code=pub.dataset_type_code
          AND a.sanitized_pdf_sha256=e->>'sha256' AND a.artifact_path<>''
          AND a.ciphertext_size>0 AND length(a.ciphertext_sha256)=64
          AND octet_length(a.nonce)=12 AND octet_length(a.wrapped_cek)=40
          AND a.encryption_algorithm='AES-256-GCM' AND a.wrapping_algorithm='AES-KW-RFC3394'
          AND a.kek_version<>'' AND octet_length(a.signature)=64
          AND a.signature_algorithm='Ed25519' AND a.signing_key_version<>''
          AND (SELECT count(*) FROM jsonb_array_elements(manifest.payload->'documents') d
            WHERE d->>'artifact_id'=a.id::text AND d->metadata_key = (e - CASE
              WHEN metadata_key='fire_plan' THEN 'path' ELSE 'content_id' END)
              AND d->>'sanitized_pdf_sha256'=a.sanitized_pdf_sha256
              AND d->>'ciphertext_sha256'=a.ciphertext_sha256
              AND d->>'ciphertext_size'=a.ciphertext_size::text
              AND d->>'nonce'=encode(a.nonce,'base64')
              AND d->>'encryption_algorithm'=a.encryption_algorithm
              AND d->>'wrapping_algorithm'=a.wrapping_algorithm
              AND d->>'kek_version'=a.kek_version
              AND d->>'signature'=replace(encode(a.signature,'base64'), E'\n', '')
              AND d->>'signature_algorithm'=a.signature_algorithm
              AND d->>'signing_key_version'=a.signing_key_version
              AND d->>'generation_key_wrapping_algorithm'='AES-KW-RFC3394'
              AND d->>'generation_wrapped_cek' ~ '^[A-Za-z0-9+/]{54}==$'
              AND d->>'download_path'='/api/v1/tablet/document-generations/' || pub.id::text || '/artifacts/' || a.id::text || '/download'
          )=1
      )
    ) THEN RETURN false;
 END IF;
 RETURN true;
END; $$ LANGUAGE plpgsql;

CREATE FUNCTION publications_guard_document_generation() RETURNS trigger AS $$
BEGIN
 IF (NEW.schema_version=2 AND NEW.dataset_type_code IN
   ('department_fire_plans','department_klgv_plans','department_files','station_files')) OR
   (TG_OP='UPDATE' AND OLD.schema_version=2 AND OLD.dataset_type_code IN
   ('department_fire_plans','department_klgv_plans','department_files','station_files')) THEN
   IF TG_OP='UPDATE' AND OLD.status <> 'STAGED' AND (
     (NEW.scope_type,NEW.department_id,NEW.station_id,NEW.dataset_type_code,NEW.schema_version,NEW.version_number,NEW.source_revision,NEW.source_fingerprint)
     IS DISTINCT FROM (OLD.scope_type,OLD.department_id,OLD.station_id,OLD.dataset_type_code,OLD.schema_version,OLD.version_number,OLD.source_revision,OLD.source_fingerprint)
     OR (NEW.source_snapshot IS DISTINCT FROM OLD.source_snapshot AND NOT
       (NEW.source_snapshot IS NULL AND NEW.status IN ('FAILED','CANCELLED','REJECTED','OBSOLETE')))
   ) THEN RAISE EXCEPTION 'Frozen document generations are immutable'; END IF;
   IF NEW.status IN ('READY_FOR_REVIEW','PUBLISHED') AND NOT publications_document_generation_complete(NEW) THEN
     RAISE EXCEPTION 'Publication requires a complete ready document generation';
   END IF;
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER publications_document_generation_guard BEFORE INSERT OR UPDATE
ON publications_datasetpublication FOR EACH ROW EXECUTE FUNCTION publications_guard_document_generation();

CREATE FUNCTION publications_guard_document_reference() RETURNS trigger AS $$
DECLARE pub publications_datasetpublication; art publications_documentartifact; entries jsonb;
BEGIN
 IF TG_OP='UPDATE' THEN RAISE EXCEPTION 'Document references are immutable'; END IF;
 SELECT * INTO pub FROM publications_datasetpublication WHERE id=CASE WHEN TG_OP='DELETE' THEN OLD.publication_id ELSE NEW.publication_id END FOR UPDATE;
 IF pub.dataset_type_code NOT IN ('department_fire_plans','department_klgv_plans') THEN
   IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
 END IF;
 IF TG_OP='DELETE' THEN
   IF pub.status NOT IN ('FAILED','CANCELLED','REJECTED','OBSOLETE') THEN
     RAISE EXCEPTION 'Live document references are protected'; END IF;
   RETURN OLD;
 END IF;
 SELECT * INTO art FROM publications_documentartifact WHERE id=NEW.document_artifact_id FOR UPDATE;
 entries := pub.source_snapshot->CASE pub.dataset_type_code WHEN 'department_fire_plans' THEN 'fire_plans' ELSE 'klgv_plans' END;
 IF pub.status <> 'BUILDING' OR pub.schema_version<>2 OR pub.scope_type<>'DEPARTMENT' OR pub.department_id IS NULL OR pub.station_id IS NOT NULL OR
    art.dataset_type_code IS DISTINCT FROM pub.dataset_type_code OR
    art.canonical_document_id IS DISTINCT FROM NEW.canonical_document_id OR
    NOT EXISTS (SELECT 1 FROM jsonb_array_elements(entries) e WHERE e->>'id'=NEW.canonical_document_id::text AND e->>'sha256'=art.sanitized_pdf_sha256) OR
    (pub.dataset_type_code='department_fire_plans' AND NOT EXISTS (SELECT 1 FROM reference_data_fireplan WHERE id=NEW.canonical_document_id AND department_id=pub.department_id AND sha256=art.sanitized_pdf_sha256)) OR
    (pub.dataset_type_code='department_klgv_plans' AND NOT EXISTS (SELECT 1 FROM reference_data_klgvplan WHERE id=NEW.canonical_document_id AND department_id=pub.department_id AND sha256=art.sanitized_pdf_sha256)) THEN
   RAISE EXCEPTION 'Invalid frozen document artifact reference';
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER publications_document_reference_guard BEFORE INSERT OR UPDATE OR DELETE
ON publications_publicationdocumentartifactreference FOR EACH ROW EXECUTE FUNCTION publications_guard_document_reference();
CREATE FUNCTION publications_guard_document_crypto_delete() RETURNS trigger AS $$
DECLARE pub publications_datasetpublication;
BEGIN
 SELECT * INTO pub FROM publications_datasetpublication WHERE id=OLD.publication_id FOR UPDATE;
 IF pub.dataset_type_code IN ('department_fire_plans','department_klgv_plans','department_files','station_files')
    AND pub.status NOT IN ('STAGED','BUILDING','FAILED','CANCELLED','REJECTED','OBSOLETE') THEN
   RAISE EXCEPTION 'Live document cryptographic metadata is protected';
 END IF;
 RETURN OLD;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER publications_document_key_delete_guard BEFORE DELETE ON publications_documentgenerationkey
FOR EACH ROW EXECUTE FUNCTION publications_guard_document_crypto_delete();
CREATE TRIGGER publications_document_manifest_delete_guard BEFORE DELETE ON publications_documentgenerationmanifest
FOR EACH ROW EXECUTE FUNCTION publications_guard_document_crypto_delete();
"""


def forward(apps, schema_editor):
    apps.get_model("publications", "DatasetTypeRegistry").objects.filter(
        code="department_fire_plans"
    ).update(current_schema_version=2, supported_schema_versions=[2])
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_functiondef('publications_guard_artifact()'::regprocedure)")
        guard = cursor.fetchone()[0]
        cursor.execute(
            "SELECT pg_get_functiondef('file_library_guard_crypto_metadata()'::regprocedure)"
        )
        crypto_guard = cursor.fetchone()[0]
    old_codes = "'department_klgv_plans', 'department_files', 'station_files'"
    if old_codes not in guard:
        raise RuntimeError("Unexpected publication artifact guard definition")
    schema_editor.execute(guard.replace(old_codes, "'department_fire_plans', " + old_codes))
    # Extend the existing immutable crypto guard; retain its File Library behavior.
    schema_editor.execute(
        crypto_guard.replace(
            "'department_files','station_files'",
            "'department_fire_plans','department_klgv_plans','department_files','station_files'",
        )
    )
    schema_editor.execute(SQL)


def reverse(apps, schema_editor):
    schema_editor.execute("""
      DROP TRIGGER publications_document_key_delete_guard ON publications_documentgenerationkey;
      DROP TRIGGER publications_document_manifest_delete_guard ON publications_documentgenerationmanifest;
      DROP FUNCTION publications_guard_document_crypto_delete();
      DROP TRIGGER publications_document_reference_guard ON publications_publicationdocumentartifactreference;
      DROP FUNCTION publications_guard_document_reference();
      DROP TRIGGER publications_document_generation_guard ON publications_datasetpublication;
      DROP FUNCTION publications_guard_document_generation();
      DROP FUNCTION publications_document_generation_complete(publications_datasetpublication);
    """)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_functiondef('publications_guard_artifact()'::regprocedure)")
        guard = cursor.fetchone()[0]
        cursor.execute(
            "SELECT pg_get_functiondef('file_library_guard_crypto_metadata()'::regprocedure)"
        )
        crypto_guard = cursor.fetchone()[0]
    schema_editor.execute(
        guard.replace("'department_fire_plans', 'department_klgv_plans'", "'department_klgv_plans'")
    )
    schema_editor.execute(
        crypto_guard.replace(
            "'department_fire_plans','department_klgv_plans','department_files','station_files'",
            "'department_files','station_files'",
        )
    )
    apps.get_model("publications", "DatasetTypeRegistry").objects.filter(
        code="department_fire_plans"
    ).update(current_schema_version=1, supported_schema_versions=[1, 2])


class Migration(migrations.Migration):
    dependencies = [
        ("publications", "0030_correct_legacy_bulk_rebuild_intent"),
        ("file_library", "0010_publication_capacity_reviews"),
    ]
    operations = [migrations.RunPython(forward, reverse)]
