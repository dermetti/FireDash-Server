from django.db import migrations

SQL = """
CREATE FUNCTION file_library_guard_publication_content() RETURNS trigger AS $$
DECLARE pub publications_datasetpublication; lib file_library_library;
BEGIN
 IF TG_OP = 'DELETE' THEN
   SELECT * INTO pub FROM publications_datasetpublication WHERE id = OLD.publication_id;
   IF pub.status NOT IN ('FAILED','CANCELLED','REJECTED','OBSOLETE') THEN
     RAISE EXCEPTION 'Live publication source references are protected';
   END IF;
   RETURN OLD;
 END IF;
 IF TG_OP = 'UPDATE' THEN RAISE EXCEPTION 'Publication source references are immutable'; END IF;
 SELECT * INTO pub FROM publications_datasetpublication WHERE id = NEW.publication_id;
 SELECT l.* INTO lib FROM file_library_library l JOIN file_library_content c ON c.library_id=l.id
 WHERE c.id=NEW.content_id;
 IF pub.status NOT IN ('STAGED','BUILDING') OR
    (lib.department_id IS NOT NULL AND (pub.dataset_type_code <> 'department_files'
      OR pub.department_id IS DISTINCT FROM lib.department_id OR pub.station_id IS NOT NULL)) OR
    (lib.station_id IS NOT NULL AND (pub.dataset_type_code <> 'station_files'
      OR pub.station_id IS DISTINCT FROM lib.station_id)) OR
    NOT EXISTS (SELECT 1 FROM jsonb_array_elements(pub.source_snapshot->'documents') e
      WHERE e->>'id'=NEW.document_id::text AND e->>'content_id'=NEW.content_id::text) THEN
   RAISE EXCEPTION 'Invalid frozen library content reference';
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER file_library_publication_content_guard BEFORE INSERT OR UPDATE OR DELETE
ON file_library_publicationcontent FOR EACH ROW EXECUTE FUNCTION file_library_guard_publication_content();

CREATE FUNCTION file_library_guard_generation() RETURNS trigger AS $$
BEGIN
 IF TG_OP = 'UPDATE' AND OLD.dataset_type_code IN ('department_files','station_files') AND OLD.status <> 'STAGED' THEN
   IF (NEW.id,NEW.department_id,NEW.station_id,NEW.dataset_type_code,NEW.version_number,NEW.source_revision)
      IS DISTINCT FROM (OLD.id,OLD.department_id,OLD.station_id,OLD.dataset_type_code,OLD.version_number,OLD.source_revision)
      OR (NEW.source_snapshot IS DISTINCT FROM OLD.source_snapshot AND NOT
          (NEW.source_snapshot IS NULL AND NEW.status IN ('FAILED','CANCELLED','REJECTED','OBSOLETE'))) THEN
     RAISE EXCEPTION 'Frozen file library generations are immutable';
   END IF;
 END IF;
 IF NEW.dataset_type_code IN ('department_files','station_files') AND NEW.status='PUBLISHED' THEN
   IF NEW.schema_version <> 2 OR NEW.artifact_path <> '' OR NEW.source_snapshot IS NULL OR
      NOT EXISTS (SELECT 1 FROM publications_documentgenerationmanifest WHERE publication_id=NEW.id) OR
      NOT EXISTS (SELECT 1 FROM publications_documentgenerationkey WHERE publication_id=NEW.id) OR
      (SELECT count(*) FROM publications_publicationdocumentartifactreference WHERE publication_id=NEW.id)
        <> jsonb_array_length(NEW.source_snapshot->'documents') OR
      (SELECT count(*) FROM file_library_publicationcontent WHERE publication_id=NEW.id)
        <> jsonb_array_length(NEW.source_snapshot->'documents') THEN
      RAISE EXCEPTION 'File library publication requires a complete document generation';
   END IF;
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER file_library_generation_guard BEFORE INSERT OR UPDATE ON publications_datasetpublication
FOR EACH ROW EXECUTE FUNCTION file_library_guard_generation();

CREATE FUNCTION file_library_guard_artifact_reference() RETURNS trigger AS $$
DECLARE pub publications_datasetpublication; art publications_documentartifact;
BEGIN
 IF TG_OP = 'DELETE' THEN
   SELECT * INTO pub FROM publications_datasetpublication WHERE id=OLD.publication_id;
   IF pub.dataset_type_code IN ('department_files','station_files') AND
      pub.status NOT IN ('FAILED','CANCELLED','REJECTED','OBSOLETE') THEN
     RAISE EXCEPTION 'Live file library artifacts are protected';
   END IF;
   RETURN OLD;
 END IF;
 SELECT * INTO pub FROM publications_datasetpublication WHERE id=NEW.publication_id;
 IF pub.dataset_type_code NOT IN ('department_files','station_files') THEN RETURN NEW; END IF;
 SELECT * INTO art FROM publications_documentartifact WHERE id=NEW.document_artifact_id;
 IF pub.status <> 'BUILDING' OR art.dataset_type_code <> pub.dataset_type_code OR
    art.canonical_document_id <> NEW.canonical_document_id OR
    NOT EXISTS (SELECT 1 FROM file_library_publicationcontent r JOIN file_library_content c ON c.id=r.content_id
       WHERE r.publication_id=NEW.publication_id AND r.document_id=NEW.canonical_document_id
         AND c.sha256=art.sanitized_pdf_sha256) THEN
    RAISE EXCEPTION 'Invalid file library artifact reference';
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER file_library_artifact_reference_guard BEFORE INSERT OR UPDATE OR DELETE
ON publications_publicationdocumentartifactreference FOR EACH ROW EXECUTE FUNCTION file_library_guard_artifact_reference();

CREATE FUNCTION file_library_guard_crypto_metadata() RETURNS trigger AS $$
DECLARE code text;
BEGIN
 IF TG_TABLE_NAME='publications_documentartifact' THEN
   code := OLD.dataset_type_code;
 ELSE
   SELECT dataset_type_code INTO code FROM publications_datasetpublication WHERE id=OLD.publication_id;
 END IF;
 IF code IN ('department_files','station_files') THEN
   RAISE EXCEPTION 'File library cryptographic metadata is immutable';
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER file_library_artifact_immutable BEFORE UPDATE ON publications_documentartifact
FOR EACH ROW EXECUTE FUNCTION file_library_guard_crypto_metadata();
CREATE TRIGGER file_library_key_immutable BEFORE UPDATE ON publications_documentgenerationkey
FOR EACH ROW EXECUTE FUNCTION file_library_guard_crypto_metadata();
CREATE TRIGGER file_library_manifest_immutable BEFORE UPDATE ON publications_documentgenerationmanifest
FOR EACH ROW EXECUTE FUNCTION file_library_guard_crypto_metadata();
"""

REVERSE = """
DROP TRIGGER file_library_artifact_immutable ON publications_documentartifact;
DROP TRIGGER file_library_key_immutable ON publications_documentgenerationkey;
DROP TRIGGER file_library_manifest_immutable ON publications_documentgenerationmanifest;
DROP FUNCTION file_library_guard_crypto_metadata();
DROP TRIGGER file_library_artifact_reference_guard ON publications_publicationdocumentartifactreference;
DROP FUNCTION file_library_guard_artifact_reference();
DROP TRIGGER file_library_generation_guard ON publications_datasetpublication;
DROP FUNCTION file_library_guard_generation();
DROP TRIGGER file_library_publication_content_guard ON file_library_publicationcontent;
DROP FUNCTION file_library_guard_publication_content();
"""


class Migration(migrations.Migration):
    dependencies = [("file_library", "0003_publicationcontent")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
