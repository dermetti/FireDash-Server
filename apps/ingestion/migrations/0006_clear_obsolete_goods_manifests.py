from django.db import migrations


def clear_legacy_manifests(apps, schema_editor):
    manifests = apps.get_model("publications", "SignedManifest").objects.using(
        schema_editor.connection.alias
    )
    for manifest in manifests.iterator():
        payload = manifest.payload if isinstance(manifest.payload, dict) else {}
        datasets = payload.get("datasets", [])
        if not isinstance(datasets, list):
            continue
        if any(
            isinstance(entry, dict)
            and entry.get("type") == "dangerous_goods"
            and isinstance(entry.get("scope"), dict)
            and entry["scope"].get("type") == "DEPARTMENT"
            for entry in datasets
        ):
            manifests.filter(pk=manifest.pk).update(status="OBSOLETE", payload={}, signature=None)


class Migration(migrations.Migration):
    dependencies = [("ingestion", "0005_system_dangerous_goods")]
    operations = [migrations.RunPython(clear_legacy_manifests, migrations.RunPython.noop)]
