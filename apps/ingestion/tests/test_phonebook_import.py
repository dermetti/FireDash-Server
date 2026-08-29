from pathlib import Path

import pytest

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.ingestion.models import ImportBatch
from apps.ingestion.services import (
    ImportError,
    apply_preview,
    create_preview,
    phonebook_review_context,
    set_phonebook_reconciliation,
)
from apps.organizations.models import Department, Station
from apps.reference_data.models import PhonebookEntry
from apps.reference_data.services import create_phonebook_entry, update_phonebook_entry


@pytest.fixture
def scope(db, settings):
    settings.INGESTION_STAGING_ROOT = Path.cwd() / ".private" / "phonebook-import-staging"
    actor = User.objects.create_user("import-phonebook@example.test", "Import", "password")
    department = Department.objects.create(name="One", short_code="ONE", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    station = Station.objects.create(
        department=department, name="FF Bramfeld 2921", short_code="F2921"
    )
    other = Department.objects.create(name="Other", short_code="OTH", created_by=actor)
    return actor, department, station, other


def payload(rows, headers=None):
    headers = headers or [
        "first_name",
        "last_name",
        "organization_unit",
        "function",
        "phone_number",
        "scope",
    ]
    return (
        ",".join(headers)
        + "\n"
        + "\n".join(",".join(row.get(header, "") for header in headers) for row in rows)
    ).encode()


def preview(actor, department, rows, headers=None):
    return create_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.PHONEBOOK,
        import_format="csv",
        import_mode="upsert",
        filename="phonebook.csv",
        payload=payload(rows, headers),
    )


@pytest.mark.django_db
def test_csv_scope_resolution_header_order_and_create(scope):
    actor, department, station, _ = scope
    batch = preview(
        actor,
        department,
        [
            {
                "first_name": "Ada",
                "last_name": "Lovelace",
                "organization_unit": "Ops",
                "function": "Chief",
                "phone_number": "040 428512300",
                "scope": "  ff   bramfeld 2921 ",
            }
        ],
        list(
            reversed(
                [
                    "first_name",
                    "last_name",
                    "organization_unit",
                    "function",
                    "phone_number",
                    "scope",
                ]
            )
        ),
    )
    assert batch.status == ImportBatch.Status.PREVIEW_READY
    assert batch.normalized_intent["rows"][0]["phone_number"] == "040 42851 2300"
    assert batch.normalized_intent["rows"][0]["station_id"] == str(station.id)
    apply_preview(actor=actor, batch_id=batch.id)
    entry = PhonebookEntry.objects.get(department=department)
    assert entry.station_id == station.id and entry.phone_number == "040 42851 2300"


@pytest.mark.django_db
def test_review_update_create_skip_claim_and_stale_protection(scope):
    actor, department, station, other = scope
    existing = create_phonebook_entry(
        actor=actor,
        department=department,
        station=station,
        first_name="Ada",
        last_name="Lovelace",
        organization_unit="Ops",
        function="Chief",
        phone_number="040 42851 2300",
    )
    rows = [
        {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "organization_unit": "Ops",
            "function": "Deputy",
            "phone_number": "040 428512300",
            "scope": "F2921",
        },
        {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "organization_unit": "Ops",
            "function": "Chief",
            "phone_number": "040 428512300",
            "scope": "F2921",
        },
    ]
    batch = preview(actor, department, rows)
    assert phonebook_review_context(batch)["current"]["candidate"]["id"] == str(existing.id)
    set_phonebook_reconciliation(actor=actor, batch_id=batch.id, row_index=0, action="update")
    with pytest.raises(ImportError):
        set_phonebook_reconciliation(actor=actor, batch_id=batch.id, row_index=1, action="update")
    set_phonebook_reconciliation(actor=actor, batch_id=batch.id, row_index=1, action="create")
    apply_preview(actor=actor, batch_id=batch.id)
    existing.refresh_from_db()
    assert (
        existing.function == "Deputy"
        and PhonebookEntry.objects.filter(department=department).count() == 2
    )
    stale = preview(actor, department, rows[:1])
    set_phonebook_reconciliation(actor=actor, batch_id=stale.id, row_index=0, action="update")
    update_phonebook_entry(actor=actor, entry=existing, function="Changed")
    with pytest.raises(ImportError):
        apply_preview(actor=actor, batch_id=stale.id)
    assert PhonebookEntry.objects.filter(department=other).count() == 0


@pytest.mark.django_db
def test_phonebook_headers_and_scope_errors_are_rejected(scope):
    actor, department, _, _ = scope
    missing = preview(
        actor,
        department,
        [
            {
                "first_name": "Ada",
                "last_name": "L",
                "organization_unit": "",
                "function": "",
                "phone_number": "1",
                "scope": "department",
            }
        ],
        ["first_name", "last_name", "organization_unit", "function", "phone_number"],
    )
    assert missing.status == ImportBatch.Status.INVALID
    bad_scope = preview(
        actor,
        department,
        [
            {
                "first_name": "Ada",
                "last_name": "L",
                "organization_unit": "",
                "function": "",
                "phone_number": "1",
                "scope": "unknown",
            }
        ],
    )
    assert bad_scope.status == ImportBatch.Status.INVALID
