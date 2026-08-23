"""Delete development-only REMOVED tablet rows and their protected dependents."""

from django.db import migrations
from django.db.models import Q


def delete_removed_tablets(apps, schema_editor):
    """Delete only data owned by legacy REMOVED tablets.

    Audit events use an append-only generic UUID reference rather than a foreign
    key, so they intentionally remain as historical records. Every protected
    foreign-key dependent is removed explicitly before the tablet row.
    """

    Tablet = apps.get_model("tablets", "Tablet")
    AppInstallation = apps.get_model("tablets", "AppInstallation")
    AdoptionInvitation = apps.get_model("tablets", "AdoptionInvitation")
    ReactivationInvitation = apps.get_model("tablets", "ReactivationInvitation")
    AdoptionRequest = apps.get_model("tablets", "AdoptionRequest")
    TabletApiActivity = apps.get_model("tablets", "TabletApiActivity")
    TabletVehicleAssignment = apps.get_model("assignments", "TabletVehicleAssignment")
    DatasetKeyGrant = apps.get_model("publications", "DatasetKeyGrant")
    SignedManifest = apps.get_model("publications", "SignedManifest")

    tablet_ids = list(Tablet.objects.filter(status="REMOVED").values_list("id", flat=True))
    if not tablet_ids:
        return

    installation_ids = list(
        AppInstallation.objects.filter(tablet_id__in=tablet_ids).values_list("id", flat=True)
    )
    adoption_invitation_ids = list(
        AdoptionInvitation.objects.filter(tablet_id__in=tablet_ids).values_list("id", flat=True)
    )
    reactivation_invitation_ids = list(
        ReactivationInvitation.objects.filter(app_installation_id__in=installation_ids).values_list(
            "id", flat=True
        )
    )

    AdoptionRequest.objects.filter(
        Q(invitation_id__in=adoption_invitation_ids)
        | Q(reactivation_invitation_id__in=reactivation_invitation_ids)
    ).delete()
    SignedManifest.objects.filter(app_installation_id__in=installation_ids).delete()
    DatasetKeyGrant.objects.filter(app_installation_id__in=installation_ids).delete()
    TabletApiActivity.objects.filter(app_installation_id__in=installation_ids).delete()
    ReactivationInvitation.objects.filter(pk__in=reactivation_invitation_ids).delete()
    AppInstallation.objects.filter(pk__in=installation_ids).delete()
    AdoptionInvitation.objects.filter(pk__in=adoption_invitation_ids).delete()
    TabletVehicleAssignment.objects.filter(tablet_id__in=tablet_ids).delete()
    Tablet.objects.filter(pk__in=tablet_ids).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("assignments", "0001_initial"),
        ("publications", "0011_department_klgv_plans_registry"),
        ("tablets", "0005_alter_tablet_status"),
    ]

    operations = [migrations.RunPython(delete_removed_tablets, migrations.RunPython.noop)]
