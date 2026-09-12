from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("outbound_mail", "0006_systemmailconfiguration_outbound_mail_https_proxy"),
    ]

    operations = [
        migrations.AddField(
            model_name="tabletreportdeliveryrequest",
            name="recipient_email",
            field=models.EmailField(blank=True, default="", max_length=254),
        ),
    ]
