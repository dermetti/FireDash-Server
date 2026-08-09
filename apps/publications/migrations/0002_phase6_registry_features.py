import django.db.models.deletion
from django.db import migrations, models

REGISTRY = (
    ("department_hydrants", "department", 1, "[1]", True, "publications"),
    ("department_fire_plans", "department", 1, "[1]", True, "publications"),
    ("station_personnel", "station", 1, "[1]", True, "publications"),
    ("test_department_incidents", "department", 1, "[1]", False, "publications"),
)


def install_registry_and_triggers(apps, schema_editor):
    Registry = apps.get_model("publications", "DatasetTypeRegistry")
    for code, scope, version, _supported, required, feature in REGISTRY:
        Registry.objects.update_or_create(
            code=code,
            defaults={
                "scope": scope,
                "current_schema_version": version,
                "supported_schema_versions": [version],
                "required": required,
                "feature_code": feature,
            },
        )

    vendor = schema_editor.connection.vendor
    tables = (
        "publications_datasetscopestate",
        "publications_datasetpublication",
        "publications_publicationjob",
    )
    if vendor == "postgresql":
        schema_editor.execute(
            """
            CREATE OR REPLACE FUNCTION publications_validate_dataset_scope() RETURNS trigger AS $$
            DECLARE registry_scope text;
            DECLARE station_department uuid;
            BEGIN
              SELECT scope INTO registry_scope FROM publications_datasettyperegistry WHERE code = NEW.dataset_type_code;
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
            END; $$ LANGUAGE plpgsql;
            """
        )
        for table in tables:
            schema_editor.execute(
                f"CREATE TRIGGER {table}_dataset_scope BEFORE INSERT OR UPDATE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION publications_validate_dataset_scope();"
            )
    elif vendor == "sqlite":
        for table in tables:
            for event in ("INSERT", "UPDATE"):
                schema_editor.execute(
                    f"""
                    CREATE TRIGGER {table}_dataset_scope_{event.lower()} BEFORE {event} ON {table}
                    BEGIN
                      SELECT RAISE(ABORT, 'Unknown dataset type code')
                        WHERE NOT EXISTS (SELECT 1 FROM publications_datasettyperegistry r WHERE r.code = NEW.dataset_type_code);
                      SELECT RAISE(ABORT, 'Dataset type station scope is invalid')
                        WHERE (SELECT scope FROM publications_datasettyperegistry r WHERE r.code = NEW.dataset_type_code) = 'department' AND NEW.station_id IS NOT NULL;
                      SELECT RAISE(ABORT, 'Dataset type station scope is invalid')
                        WHERE (SELECT scope FROM publications_datasettyperegistry r WHERE r.code = NEW.dataset_type_code) = 'station' AND NEW.station_id IS NULL;
                      SELECT RAISE(ABORT, 'Station must belong to the scope department')
                        WHERE NEW.station_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM organizations_station s WHERE s.id = NEW.station_id AND s.department_id = NEW.department_id);
                    END;
                    """
                )


def remove_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    tables = (
        "publications_datasetscopestate",
        "publications_datasetpublication",
        "publications_publicationjob",
    )
    if vendor == "postgresql":
        for table in tables:
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {table}_dataset_scope ON {table};")
        schema_editor.execute("DROP FUNCTION IF EXISTS publications_validate_dataset_scope();")
    elif vendor == "sqlite":
        for table in tables:
            for event in ("insert", "update"):
                schema_editor.execute(f"DROP TRIGGER IF EXISTS {table}_dataset_scope_{event};")


class Migration(migrations.Migration):
    dependencies = [("publications", "0001_initial")]

    operations = [
        migrations.RemoveConstraint(
            model_name="datasetscopestate", name="registered_dataset_scope_state_scope"
        ),
        migrations.RemoveConstraint(
            model_name="datasetpublication", name="registered_dataset_publication_scope"
        ),
        migrations.RemoveConstraint(
            model_name="publicationjob", name="registered_publication_job_scope"
        ),
        migrations.CreateModel(
            name="DatasetTypeRegistry",
            fields=[
                ("code", models.CharField(max_length=100, primary_key=True, serialize=False)),
                ("scope", models.CharField(max_length=16)),
                ("current_schema_version", models.PositiveIntegerField()),
                ("supported_schema_versions", models.JSONField(default=list)),
                ("required", models.BooleanField(default=True)),
                ("feature_code", models.CharField(max_length=100)),
            ],
        ),
        migrations.CreateModel(
            name="DepartmentFeature",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("feature_code", models.CharField(max_length=100)),
                ("enabled", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "department",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="features",
                        to="organizations.department",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("department", "feature_code"), name="unique_department_feature"
                    )
                ]
            },
        ),
        migrations.AddField(
            model_name="datasetpublication",
            name="artifact_ready",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="datasetpublication",
            name="change_summary",
            field=models.JSONField(default=dict),
        ),
        migrations.RunPython(install_registry_and_triggers, remove_triggers),
    ]
