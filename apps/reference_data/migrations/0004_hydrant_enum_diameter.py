from django.db import migrations, models


def preserve_existing_flow(apps, schema_editor):
    Hydrant = apps.get_model("reference_data", "Hydrant")
    for h in Hydrant.objects.filter(flow_information__regex=r"^\d+$"):
        try:
            h.diameter_mm = int(h.flow_information)
            h.save(update_fields=["diameter_mm"])
        except (ValueError, TypeError):
            pass


class Migration(migrations.Migration):
    dependencies = [
        (
            "reference_data",
            "0002_rename_reference_d_departm_3ca664_idx_reference_d_departm_f01f3d_idx_and_more",
        )
    ]

    operations = [
        migrations.AddField(
            model_name="hydrant",
            name="diameter_mm",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(preserve_existing_flow, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="hydrant",
            name="active",
        ),
    ]
