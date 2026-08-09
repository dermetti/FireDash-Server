from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("audit", "0003_allow_test_database_flush")]

    operations = [
        migrations.RunSQL(
            sql="""
                DROP TRIGGER IF EXISTS audit_event_immutable ON audit_event;
                CREATE TRIGGER audit_event_immutable
                BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_event
                FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_event_mutation();
            """,
            reverse_sql="""
                DROP TRIGGER IF EXISTS audit_event_immutable ON audit_event;
                CREATE TRIGGER audit_event_immutable
                BEFORE UPDATE OR DELETE ON audit_event
                FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_event_mutation();
            """,
        )
    ]
