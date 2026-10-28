"""Department serialization for library quotas, source edits and publications."""

import hashlib

from django.db import connection

FILE_DATASETS = {"department_files", "station_files"}


def department_for(library):
    return library.department if library.department_id else library.station.department


def lock_library_department(library):
    lock_department_sources(department_for(library).id)


def lock_department_sources(department_id):
    """Acquire inside atomic(), before scope/library or lifecycle row locks.

    Keep the historical advisory key so existing source writers serialize with
    upgraded workers during deployment. There are no post-commit callbacks.
    """
    key = int.from_bytes(
        hashlib.sha256(f"library-capacity:{department_id}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def lock_file_scope(*, department, dataset_type_code):
    if dataset_type_code in FILE_DATASETS:
        lock_department_sources(department.id)
