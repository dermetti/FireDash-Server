import pytest
from django.db import connection

from tools.migration_catalog import historical_executor as MigrationExecutor


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "disposable_migration_database",
    [("tablets", "0008_remove_obsolete_reactivation_flow")],
    indirect=True,
)
def test_storage_telemetry_migration_applies_forward(disposable_migration_database):
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("tablets", "0008_remove_obsolete_reactivation_flow")])
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(
                cursor, "tablets_appinstallation"
            )
        assert "free_storage_bytes" not in {column.name for column in columns}
    finally:
        MigrationExecutor(connection).migrate(latest)
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, "tablets_appinstallation")
    assert constraints["installation_storage_complete"]["check"]
    assert constraints["installation_storage_free_lte_total"]["check"]
