from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("organizations", "0003_structured_station_address")]

    operations = [
        migrations.RemoveField(
            model_name="station",
            name="address",
        ),
    ]
