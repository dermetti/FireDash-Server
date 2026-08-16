from django.db import migrations


def add_department_klgv_plans_registry(apps, schema_editor):
    registry = apps.get_model("publications", "DatasetTypeRegistry")
    registry.objects.update_or_create(
        code="department_klgv_plans",
        defaults={
            "scope": "department",
            "current_schema_version": 1,
            "supported_schema_versions": [1],
            "required": False,
            "feature_code": "klgv_plans",
        },
    )


class Migration(migrations.Migration):
    dependencies = [("publications", "0010_publicationjob_bulk_trigger")]

    operations = [migrations.RunPython(add_department_klgv_plans_registry, migrations.RunPython.noop)]
