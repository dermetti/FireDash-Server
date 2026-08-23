"""Remove the pre-v1 administrator-driven stale reactivation workflow."""

from django.db import migrations, models


def delete_obsolete_reactivation_data(apps, schema_editor):
    AdoptionRequest = apps.get_model("tablets", "AdoptionRequest")
    ReactivationInvitation = apps.get_model("tablets", "ReactivationInvitation")

    AdoptionRequest.objects.filter(reactivation_invitation__isnull=False).delete()
    ReactivationInvitation.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("tablets", "0007_enforce_current_tablet_asset_states")]

    operations = [
        migrations.RunPython(delete_obsolete_reactivation_data, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="adoptionrequest", name="adoption_request_has_one_invitation"
        ),
        migrations.RemoveField(model_name="adoptionrequest", name="reactivation_invitation"),
        migrations.AlterField(
            model_name="adoptionrequest",
            name="invitation",
            field=models.ForeignKey(
                on_delete=models.deletion.PROTECT,
                related_name="requests",
                to="tablets.adoptioninvitation",
            ),
        ),
        migrations.DeleteModel(name="ReactivationInvitation"),
        migrations.RemoveField(model_name="appinstallation", name="reactivated_at"),
        migrations.RemoveField(model_name="appinstallation", name="reactivated_by"),
    ]
