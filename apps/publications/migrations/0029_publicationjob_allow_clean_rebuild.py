from django.db import migrations, models


def preserve_explicit_requests(apps, schema_editor):
    # Before this flag, un-debounced explicit requests were the durable force
    # rebuild representation. A staged DATA_CHANGE promoted with Build now
    # retains its debounce origin and is not a force request.
    apps.get_model("publications", "PublicationJob").objects.filter(
        status__in=("PENDING", "RUNNING"),
        trigger_type__in=("USER_REQUEST", "BULK_REQUEST"),
        debounce_started_at__isnull=True,
    ).update(allow_clean_rebuild=True)


class Migration(migrations.Migration):
    dependencies = [("publications", "0028_file_library_registry")]

    operations = [
        migrations.AddField(
            model_name="publicationjob",
            name="allow_clean_rebuild",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(preserve_explicit_requests, migrations.RunPython.noop),
    ]
