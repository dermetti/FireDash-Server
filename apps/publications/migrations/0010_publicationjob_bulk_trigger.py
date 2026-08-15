from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("publications", "0008_publication_job_debounce")]

    operations = [
        migrations.AlterField(
            model_name="publicationjob",
            name="trigger_type",
            field=models.CharField(
                choices=[
                    ("USER_REQUEST", "User request"),
                    ("BULK_REQUEST", "Bulk request"),
                    ("DATA_CHANGE", "Data change"),
                ],
                max_length=16,
            ),
        ),
    ]
