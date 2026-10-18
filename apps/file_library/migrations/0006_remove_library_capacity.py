from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("file_library", "0005_temporarystationaccess")]

    operations = [
        migrations.RemoveConstraint(
            model_name="library",
            name="file_library_capacity",
        ),
    ]
