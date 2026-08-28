from django.contrib.gis.db import models
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("reference_data", "0012_finalize_klgv_required_metadata"),
    ]

    operations = [
        migrations.RenameField(
            model_name="hydrant",
            old_name="location",
            new_name="geometry",
        ),
        migrations.AddField(
            model_name="hydrant",
            name="location",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
