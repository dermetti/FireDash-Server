# Generated manually for the v1 compatibility freeze.

from django.db import migrations, models

import apps.tablets.versions


def canonicalize_versions(apps, schema_editor):
    Installation = apps.get_model("tablets", "AppInstallation")
    Request = apps.get_model("tablets", "AdoptionRequest")

    def canonical(value):
        pieces = value.split(".")
        if len(pieces) == 2 and all(piece.isdecimal() for piece in pieces):
            return f"{int(pieces[0])}.{int(pieces[1])}.0"
        if len(pieces) == 3 and all(piece.isdecimal() for piece in pieces):
            return ".".join(str(int(piece)) for piece in pieces)
        raise RuntimeError(f"Cannot canonicalize persisted FireDash app version: {value!r}")

    for installation in Installation.objects.all().iterator():
        value = canonical(installation.app_version)
        installation.app_version = value
        installation.adopted_app_version = value
        installation.app_version_seen_at = installation.adopted_at
        installation.save(
            update_fields=("app_version", "adopted_app_version", "app_version_seen_at")
        )
    for request in Request.objects.all().iterator():
        request.app_version = canonical(request.app_version)
        request.save(update_fields=("app_version",))


class Migration(migrations.Migration):
    dependencies = [("tablets", "0002_tablet_asset_number_tablet_created_by_and_more")]

    operations = [
        migrations.AddField(
            model_name="appinstallation",
            name="adopted_app_version",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="appinstallation",
            name="app_build",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appinstallation",
            name="app_version_seen_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="adoptionrequest",
            name="app_build",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="adoptionrequest",
            name="completion_replay_invalidated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="adoptionrequest",
            name="completion_replay_valid_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(canonicalize_versions, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="appinstallation",
            name="adopted_app_version",
            field=models.CharField(
                max_length=64, validators=[apps.tablets.versions.validate_app_version]
            ),
        ),
        migrations.AlterField(
            model_name="appinstallation",
            name="app_version_seen_at",
            field=models.DateTimeField(),
        ),
        migrations.AlterField(
            model_name="appinstallation",
            name="app_version",
            field=models.CharField(
                max_length=64, validators=[apps.tablets.versions.validate_app_version]
            ),
        ),
        migrations.AlterField(
            model_name="adoptionrequest",
            name="app_version",
            field=models.CharField(
                max_length=64, validators=[apps.tablets.versions.validate_app_version]
            ),
        ),
    ]
