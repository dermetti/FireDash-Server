from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("outbound_mail", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemmailconfiguration",
            name="last_verification_code",
            field=models.CharField(blank=True, default="", editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="systemmailconfiguration",
            name="last_verification_outcome",
            field=models.CharField(blank=True, default="", editable=False, max_length=16),
        ),
        migrations.AddField(
            model_name="systemmailconfiguration",
            name="last_verified_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="systemmailconfiguration",
            name="verification_provider",
            field=models.CharField(blank=True, default="", editable=False, max_length=32),
        ),
    ]
