from django.db import migrations


def add_phase7_closeout_guards(apps, schema_editor):
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
            END; $$ LANGUAGE plpgsql;
            CREATE TRIGGER publications_datasetpublication_safe_artifact_path_guard
              BEFORE INSERT OR UPDATE ON publications_datasetpublication
              FOR EACH ROW EXECUTE FUNCTION publications_guard_phase7_closeout();

            CREATE OR REPLACE FUNCTION publications_prevent_activation_mutation() RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'Publication activation history is immutable';
            END; $$ LANGUAGE plpgsql;
            CREATE TRIGGER publications_publicationactivation_immutable_guard
              BEFORE UPDATE OR DELETE ON publications_publicationactivation
              FOR EACH ROW EXECUTE FUNCTION publications_prevent_activation_mutation();
            """
        )
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute(
            """
            CREATE TRIGGER publications_publicationactivation_immutable_update
              BEFORE UPDATE ON publications_publicationactivation
              BEGIN SELECT RAISE(ABORT, 'Publication activation history is immutable'); END;
            CREATE TRIGGER publications_publicationactivation_immutable_delete
              BEFORE DELETE ON publications_publicationactivation
              BEGIN SELECT RAISE(ABORT, 'Publication activation history is immutable'); END;
            """
        )


def remove_phase7_closeout_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            """
            DROP TRIGGER IF EXISTS publications_datasetpublication_safe_artifact_path_guard ON publications_datasetpublication;
            DROP FUNCTION IF EXISTS publications_guard_phase7_closeout();
            DROP TRIGGER IF EXISTS publications_publicationactivation_immutable_guard ON publications_publicationactivation;
            DROP FUNCTION IF EXISTS publications_prevent_activation_mutation();
            """
        )
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute(
            """
            DROP TRIGGER IF EXISTS publications_publicationactivation_immutable_update;
            DROP TRIGGER IF EXISTS publications_publicationactivation_immutable_delete;
            """
        )


class Migration(migrations.Migration):
    dependencies = [("publications", "0003_phase7_artifacts")]

    operations = [migrations.RunPython(add_phase7_closeout_guards, remove_phase7_closeout_guards)]
