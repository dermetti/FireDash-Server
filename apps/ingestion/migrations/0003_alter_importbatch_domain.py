from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ingestion", "0002_alter_importbatch_domain")]

    operations = [
        migrations.AlterField(
            model_name="importbatch",
            name="domain",
            field=models.CharField(
                choices=[
                    ("hydrants", "Hydrants"),
                    ("personnel", "Personnel"),
                    ("fire_plans", "Fire plans"),
                    ("klgv_plans", "KLGV plans"),
                    ("station_vehicles", "Stations and vehicles"),
                    ("phonebook", "Phonebook"),
                ],
                max_length=32,
            ),
        )
    ]
