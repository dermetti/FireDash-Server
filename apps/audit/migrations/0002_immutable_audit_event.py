from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]

    operations = [
        migrations.RunSQL(
            sql="""
                REVOKE ALL ON TABLE audit_event FROM PUBLIC;
                REVOKE UPDATE, DELETE, TRUNCATE ON TABLE audit_event FROM application_runtime;
                GRANT SELECT, INSERT ON TABLE audit_event TO application_runtime;
                CREATE OR REPLACE FUNCTION reject_audit_event_mutation()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'audit_event is append-only';
                END;
                $$;
                CREATE TRIGGER audit_event_immutable
                BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_event
                FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_event_mutation();
            """,
            reverse_sql="""
                DROP TRIGGER IF EXISTS audit_event_immutable ON audit_event;
                DROP FUNCTION IF EXISTS reject_audit_event_mutation();
                REVOKE SELECT, INSERT ON TABLE audit_event FROM application_runtime;
            """,
        )
    ]
