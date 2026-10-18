from django.db import migrations


def register(apps, schema_editor):
    registry = apps.get_model("publications", "DatasetTypeRegistry")
    for code, scope in (("department_files", "department"), ("station_files", "station")):
        registry.objects.update_or_create(
            code=code,
            defaults={
                "scope": scope,
                "current_schema_version": 2,
                "supported_schema_versions": [2],
                "required": True,
                "feature_code": "publications",
            },
        )
    # Preserve the existing legacy signature-upgrade exception verbatim.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_functiondef('publications_guard_artifact()'::regprocedure)")
        sql = cursor.fetchone()[0]
    schema_editor.execute(
        sql.replace(
            "NEW.dataset_type_code = 'department_klgv_plans'",
            "NEW.dataset_type_code IN ('department_klgv_plans', 'department_files', 'station_files')",
        )
    )


def reverse(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_functiondef('publications_guard_artifact()'::regprocedure)")
        sql = cursor.fetchone()[0]
    schema_editor.execute(
        sql.replace(
            "NEW.dataset_type_code IN ('department_klgv_plans', 'department_files', 'station_files')",
            "NEW.dataset_type_code = 'department_klgv_plans'",
        )
    )
    apps.get_model("publications", "DatasetTypeRegistry").objects.filter(
        code__in=("department_files", "station_files")
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("publications", "0027_publication_scope_types")]
    operations = [migrations.RunPython(register, reverse)]
