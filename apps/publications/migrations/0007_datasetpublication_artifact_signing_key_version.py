from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("publications", "0006_signed_manifest")]

    operations = [
        migrations.AddField(
            model_name="datasetpublication",
            name="artifact_signing_key_version",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
