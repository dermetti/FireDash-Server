from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("authorization", "0001_initial")]
    operations = [
        migrations.AddConstraint(
            model_name="departmentmembership",
            constraint=models.UniqueConstraint(
                fields=("user", "role"),
                condition=models.Q(active=True),
                name="one_active_department_admin",
            ),
        ),
    ]
