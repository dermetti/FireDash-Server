from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tablets", "0003_squashed_0009_beta")]

    operations = [
        migrations.AlterField(
            model_name="appinstallation",
            name="credential_hash",
            field=models.CharField(max_length=64, db_index=True),
        ),
    ]
