"""Require typed wire metadata and canonical snapshots at generic closeout."""

from django.db import migrations


def harden_generation_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_functiondef('publications_document_generation_complete(publications_datasetpublication)'::regprocedure)"
        )
        definition = cursor.fetchone()[0]
    old = (
        "(e - CASE\n              WHEN metadata_key='fire_plan' THEN 'path' ELSE 'content_id' END)"
    )
    if old not in definition:
        raise RuntimeError("Unexpected generic generation readiness function.")
    definition = definition.replace(
        old, "(CASE WHEN metadata_key='file' THEN e - 'content_id' ELSE e END)"
    )
    definition = (
        definition.replace(
            "AND d->>'generation_wrapped_cek' ~",
            "AND jsonb_typeof(d->'generation_wrapped_cek')='string' AND d->>'generation_wrapped_cek' ~",
        )
        .replace(
            "AND d->>'ciphertext_size'=a.ciphertext_size::text",
            "AND jsonb_typeof(d->'ciphertext_size')='number' AND d->>'ciphertext_size'=a.ciphertext_size::text",
        )
        .replace(
            "manifest.payload->>'version' IS DISTINCT FROM pub.version_number::text OR",
            "jsonb_typeof(manifest.payload->'version') IS DISTINCT FROM 'number' OR manifest.payload->>'version' IS DISTINCT FROM pub.version_number::text OR",
        )
    )
    schema_editor.execute(definition)


class Migration(migrations.Migration):
    dependencies = [("publications", "0032_remove_legacy_fire_plan_delivery")]
    operations = [migrations.RunPython(harden_generation_guard)]
