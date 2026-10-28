"""Remove obsolete delivery schema while preserving the durable cleanup queue."""

from django.db import migrations


def remove_legacy_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("""
        DROP TRIGGER publications_fire_plan_artifact_reference_guard
            ON publications_publicationfireplanartifactreference;
        DROP TRIGGER publications_fire_plan_document_artifact_immutable
            ON publications_fireplandocumentartifact;
        DROP FUNCTION publications_guard_fire_plan_artifact_reference();
        DROP FUNCTION publications_guard_fire_plan_document_artifact();
    """)


def drop_legacy_tables(apps, schema_editor):
    # Django's default DeleteModel emits DROP TABLE ... CASCADE. Use explicit
    # dependency order and restrictive drops so unexpected dependents fail closed.
    for model_name in (
        "PublicationFirePlanArtifactReference",
        "FirePlanGenerationKeyGrant",
        "FirePlanGenerationManifest",
        "FirePlanGenerationKey",
        "FirePlanDocumentArtifact",
    ):
        model = apps.get_model("publications", model_name)
        schema_editor.execute(f"DROP TABLE {schema_editor.quote_name(model._meta.db_table)}")


class Migration(migrations.Migration):
    dependencies = [("publications", "0031_unified_document_generation")]

    operations = [
        migrations.RunPython(remove_legacy_guards),
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(drop_legacy_tables)],
            state_operations=[
                migrations.DeleteModel(name="PublicationFirePlanArtifactReference"),
                migrations.DeleteModel(name="FirePlanGenerationKeyGrant"),
                migrations.DeleteModel(name="FirePlanGenerationManifest"),
                migrations.DeleteModel(name="FirePlanGenerationKey"),
                migrations.DeleteModel(name="FirePlanDocumentArtifact"),
            ],
        ),
        migrations.RenameModel(
            old_name="FirePlanDocumentArtifactCleanup", new_name="DocumentArtifactCleanup"
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    "ALTER TABLE publications_datasetscopestate DROP COLUMN delivery_format"
                )
            ],
            state_operations=[
                migrations.RemoveField(model_name="datasetscopestate", name="delivery_format")
            ],
        ),
    ]
