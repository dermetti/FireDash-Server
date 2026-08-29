from django.db import migrations


def add_phonebook_registry_entries(apps, schema_editor):
    registry = apps.get_model("publications", "DatasetTypeRegistry")
    for code, scope in (("department_phonebook", "department"), ("station_phonebook", "station")):
        registry.objects.update_or_create(
            code=code,
            defaults={
                "scope": scope,
                "current_schema_version": 1,
                "supported_schema_versions": [1],
                "required": False,
                "feature_code": "publications",
            },
        )


_POSTGRES_VALIDATOR = """
CREATE OR REPLACE FUNCTION publications_validate_dataset_scope() RETURNS trigger AS $$
DECLARE registry_scope text;
DECLARE station_department uuid;
BEGIN
  SELECT scope INTO registry_scope
    FROM publications_datasettyperegistry WHERE code = NEW.dataset_type_code;
  IF registry_scope IS NULL THEN RAISE EXCEPTION 'Unknown dataset type code'; END IF;
  IF (registry_scope = 'department' AND NEW.station_id IS NOT NULL)
     OR (registry_scope = 'station' AND NEW.station_id IS NULL) THEN
    RAISE EXCEPTION 'Dataset type station scope is invalid';
  END IF;
  IF NEW.station_id IS NOT NULL THEN
    SELECT department_id INTO station_department FROM organizations_station WHERE id = NEW.station_id;
    IF station_department IS DISTINCT FROM NEW.department_id THEN
      RAISE EXCEPTION 'Station must belong to the scope department';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def refresh_postgres_scope_validator(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(_POSTGRES_VALIDATOR)


class Migration(migrations.Migration):
    dependencies = [("publications", "0022_klgv_document_v2_ready_guard")]

    operations = [
        migrations.RunPython(add_phonebook_registry_entries, migrations.RunPython.noop),
        migrations.RunPython(refresh_postgres_scope_validator, migrations.RunPython.noop),
    ]
