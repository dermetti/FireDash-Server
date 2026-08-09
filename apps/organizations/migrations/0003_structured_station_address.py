from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("organizations", "0002_vehicle")]
    operations = [
        migrations.AddField(
            model_name="station",
            name="city",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="station",
            name="house_number",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="station",
            name="postal_code",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="station",
            name="street",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
