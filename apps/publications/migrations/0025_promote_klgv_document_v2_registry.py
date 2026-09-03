from django.db import migrations


def promote_klgv_document_v2_registry(apps, schema_editor):
    registry = apps.get_model("publications", "DatasetTypeRegistry")
    registry.objects.filter(code="department_klgv_plans").update(
        scope="department",
        current_schema_version=2,
        supported_schema_versions=[2],
        required=True,
        feature_code="klgv_plans",
    )


class Migration(migrations.Migration):
    dependencies = [("publications", "0024_dangerous_goods_source_revisions")]

    operations = [migrations.RunPython(promote_klgv_document_v2_registry, migrations.RunPython.noop)]
