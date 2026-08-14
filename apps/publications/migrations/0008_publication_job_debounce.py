from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("publications", "0007_datasetpublication_artifact_signing_key_version")]

    operations = [
        migrations.AddField(
            model_name="publicationjob",
            name="not_before",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="publicationjob",
            name="debounce_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
