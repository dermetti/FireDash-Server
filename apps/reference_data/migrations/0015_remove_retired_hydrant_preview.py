"""Retire empty legacy previews; populated previews require explicit disposition."""

from django.db import migrations


def drop_empty_preview(apps, schema_editor):
    model = apps.get_model("reference_data", "HydrantImportPreview")
    table = schema_editor.quote_name(model._meta.db_table)
    # Hold the lock through the transaction so a writer cannot race the check.
    schema_editor.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
    count = model.objects.using(schema_editor.connection.alias).count()
    if count:
        raise RuntimeError(
            f"Refusing to drop {model._meta.db_table}: {count} rows require explicit disposition."
        )
    schema_editor.execute(f"DROP TABLE {table} RESTRICT")


class Migration(migrations.Migration):
    dependencies = [("reference_data", "0014_phonebook_entries")]
    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(drop_empty_preview)],
            state_operations=[migrations.DeleteModel(name="HydrantImportPreview")],
        ),
    ]
