"""Station/Vehicle CSV ingestion: staged relationships and atomic final apply."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.ingestion.models import ImportBatch
from apps.ingestion.parsers import ImportValidationError, parse_station_vehicles
from apps.ingestion.services import (
    ImportError,
    apply_preview,
    create_preview,
    set_station_vehicle_resolution,
)
from apps.organizations.models import Department, Station, Vehicle
from apps.tablets.models import Tablet

HEADER = (
    "row_type,station_short_code,station_name,street,house_number,postal_code,city,"
    "vehicle_name,vehicle_call_sign,vehicle_asset_identifier\n"
)


def csv_rows(*rows: str) -> bytes:
    return (HEADER + "\n".join(rows) + "\n").encode()


@pytest.fixture
def station_vehicle_context(db, settings, tmp_path):
    settings.INGESTION_STAGING_ROOT = tmp_path / "station-vehicle-staging"
    actor = User.objects.create_user(
        "stations-import@example.test", "Import Admin", "safe-password"
    )
    department = Department.objects.create(name="Stations", short_code="STA", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    other = Department.objects.create(name="Other", short_code="OTH", created_by=actor)
    return actor, department, other


def preview(actor, department, payload):
    return create_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.STATION_VEHICLES,
        import_format=ImportBatch.Format.CSV,
        import_mode=ImportBatch.Mode.UPSERT,
        filename="stations-vehicles.csv",
        payload=payload,
    )


def test_parser_accepts_mixed_rows_and_normalizes_row_type():
    rows = parse_station_vehicles(
        payload=csv_rows(
            " Station , F25 , Station 25 , Musterstraße ,12,22041,Hamburg,,,",
            "vehicle,F25,,,,,,HLF 1,Florian 25/46-1,HH-F25-01",
        ),
        import_format="csv",
    )
    assert rows[0]["row_type"] == "station"
    assert rows[0]["station_short_code"] == "F25"
    assert rows[1]["vehicle_name"] == "HLF 1"


def test_parser_rejects_unknown_type_and_duplicate_staged_station():
    with pytest.raises(ImportValidationError, match="row_type"):
        parse_station_vehicles(
            payload=csv_rows("unknown,F25,Station 25,,,,,,,"), import_format="csv"
        )
    with pytest.raises(ImportValidationError, match="duplicate staged Station"):
        parse_station_vehicles(
            payload=csv_rows(
                "station,F25,Station 25,,,,,,,",
                "station, f25 ,Another Station,,,,,,,",
            ),
            import_format="csv",
        )


def test_parser_rejects_missing_required_station_or_vehicle_fields():
    with pytest.raises(ImportValidationError, match="Station rows require"):
        parse_station_vehicles(payload=csv_rows("station,F25,,,,,,,,"), import_format="csv")
    with pytest.raises(ImportValidationError, match="Vehicle rows require vehicle_name"):
        parse_station_vehicles(payload=csv_rows("vehicle,F25,,,,,,,,"), import_format="csv")


@pytest.mark.django_db(transaction=True)
def test_new_staged_station_and_dependent_vehicle_apply_atomically(station_vehicle_context):
    actor, department, _ = station_vehicle_context
    batch = preview(
        actor,
        department,
        csv_rows(
            "station,F25,Station 25,Musterstraße,12,22041,Hamburg,,,",
            "vehicle,F25,,,,,,HLF 1,Florian 25/46-1,HH-F25-01",
        ),
    )
    assert not Station.objects.filter(department=department).exists()
    assert batch.validation_summary["review_items"] == []

    apply_preview(actor=actor, batch_id=batch.id)

    station = Station.objects.get(department=department, short_code="F25")
    vehicle = Vehicle.objects.get(department=department, display_name="HLF 1")
    assert station.street == "Musterstraße"
    assert vehicle.station_id == station.id
    assert AuditEvent.objects.filter(
        action="organization.station_created", station=station
    ).exists()
    assert AuditEvent.objects.filter(
        action="organization.vehicle_created", target_uuid=vehicle.id
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_existing_station_matches_short_code_and_full_name_department_scoped(
    station_vehicle_context,
):
    actor, department, other = station_vehicle_context
    station = Station.objects.create(department=department, short_code="F25", name="Station 25")
    Station.objects.create(department=other, short_code="F26", name="Other Station")

    by_code = preview(actor, department, csv_rows("vehicle, f25 ,,,,,,HLF 1,,"))
    assert by_code.normalized_intent["rows"][0]["station_resolution"]["station_id"] == str(
        station.id
    )
    apply_preview(actor=actor, batch_id=by_code.id)

    by_name = preview(actor, department, csv_rows("vehicle,, station 25 ,,,,,TLF 1,,"))
    assert by_name.normalized_intent["rows"][0]["station_resolution"]["station_id"] == str(
        station.id
    )
    apply_preview(actor=actor, batch_id=by_name.id)
    assert set(
        Vehicle.objects.filter(department=department).values_list("display_name", flat=True)
    ) == {
        "HLF 1",
        "TLF 1",
    }


@pytest.mark.django_db(transaction=True)
def test_missing_station_is_staged_then_created_with_dependent_vehicle(station_vehicle_context):
    actor, department, _ = station_vehicle_context
    batch = preview(actor, department, csv_rows("vehicle,F99,,,,,,HLF 99,,"))
    item = batch.validation_summary["review_items"][0]
    assert item["kind"] == "missing"
    assert not Station.objects.filter(department=department).exists()

    set_station_vehicle_resolution(
        actor=actor,
        batch_id=batch.id,
        key=item["key"],
        resolution_kind="missing",
        values={
            "short_code": "F99",
            "name": "Station 99",
            "street": "Example Street",
            "house_number": "9",
            "postal_code": "22099",
            "city": "Hamburg",
        },
    )
    batch.refresh_from_db()
    assert not Station.objects.filter(department=department).exists()
    assert batch.validation_summary["review_decisions"][item["key"]]["decision"] == "approved"

    apply_preview(actor=actor, batch_id=batch.id)
    station = Station.objects.get(department=department, short_code="F99")
    assert (
        Vehicle.objects.get(department=department, display_name="HLF 99").station_id == station.id
    )


@pytest.mark.django_db(transaction=True)
def test_missing_station_review_binds_errors_and_does_not_advance(client, station_vehicle_context):
    actor, department, _ = station_vehicle_context
    batch = preview(actor, department, csv_rows("vehicle,F99,,,,,,HLF 99,,"))
    item = batch.validation_summary["review_items"][0]
    client.force_login(actor)

    response = client.post(
        reverse("ingestion-review-station-resolution", args=(department.id, batch.id, item["key"])),
        {"short_code": "", "name": "", "street": "Kept value"},
        HTTP_HX_REQUEST="true",
    )
    content = response.content.decode()
    batch.refresh_from_db()
    assert response.status_code == 200
    assert '<div id="import-review-region">' in content
    assert 'value="Kept value"' in content
    assert "This field is required to stage the missing Station" in content
    assert item["key"] not in batch.validation_summary["review_decisions"]
    assert not Station.objects.filter(department=department).exists()


@pytest.mark.django_db(transaction=True)
def test_ambiguous_station_needs_department_scoped_resolution(client, station_vehicle_context):
    actor, department, other = station_vehicle_context
    first = Station.objects.create(department=department, short_code="F1", name="Mitte")
    Station.objects.create(department=department, short_code="F2", name="Mitte")
    foreign = Station.objects.create(department=other, short_code="F3", name="Mitte")
    batch = preview(actor, department, csv_rows("vehicle,,Mitte,,,,,HLF Mitte,,"))
    item = batch.validation_summary["review_items"][0]
    assert item["kind"] == "ambiguous"

    client.force_login(actor)
    response = client.post(
        reverse("ingestion-review-station-resolution", args=(department.id, batch.id, item["key"])),
        {"station_id": str(foreign.id)},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert "Select a valid choice" in response.content.decode()
    batch.refresh_from_db()
    assert item["key"] not in batch.validation_summary["review_decisions"]

    response = client.post(
        reverse("ingestion-review-station-resolution", args=(department.id, batch.id, item["key"])),
        {"station_id": str(first.id)},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    batch.refresh_from_db()
    assert batch.validation_summary["review_decisions"][item["key"]]["decision"] == "approved"


@pytest.mark.django_db(transaction=True)
def test_apply_rejects_unresolved_and_has_no_tablet_side_effect(station_vehicle_context):
    actor, department, _ = station_vehicle_context
    existing = Station.objects.create(department=department, short_code="F25", name="Station 25")
    tablet_count = Tablet.objects.count()
    batch = preview(actor, department, csv_rows("vehicle,F25,,,,,,HLF 1,,"))
    assert batch.status == ImportBatch.Status.PREVIEW_READY
    apply_preview(actor=actor, batch_id=batch.id)
    assert Tablet.objects.count() == tablet_count
    assert Vehicle.objects.get(department=department).station_id == existing.id

    unresolved = preview(actor, department, csv_rows("vehicle,F77,,,,,,HLF 77,,"))
    with pytest.raises(ImportError, match="Resolve each Station relationship"):
        apply_preview(actor=actor, batch_id=unresolved.id)
    assert not Station.objects.filter(department=department, short_code="F77").exists()


@pytest.mark.django_db(transaction=True)
def test_failed_dependent_vehicle_rolls_back_new_station(monkeypatch, station_vehicle_context):
    actor, department, _ = station_vehicle_context
    batch = preview(
        actor,
        department,
        csv_rows("station,F25,Station 25,,,,,,,", "vehicle,F25,,,,,,HLF 1,,"),
    )

    def fail_vehicle(**kwargs):
        raise ImportError("simulated vehicle failure")

    monkeypatch.setattr("apps.ingestion.services.create_vehicle", fail_vehicle)
    with pytest.raises(ImportError, match="simulated vehicle failure"):
        apply_preview(actor=actor, batch_id=batch.id)
    assert not Station.objects.filter(department=department).exists()
    assert not Vehicle.objects.filter(department=department).exists()


@pytest.mark.django_db
def test_station_vehicle_import_page_has_csv_template_recent_batches_and_back_link(
    client, station_vehicle_context
):
    actor, department, _ = station_vehicle_context
    ImportBatch.objects.create(
        department=department,
        actor=actor,
        domain=ImportBatch.Domain.STATION_VEHICLES,
        import_format=ImportBatch.Format.CSV,
        import_mode=ImportBatch.Mode.UPSERT,
        original_filename="newest.csv",
        upload_sha256="a" * 64,
        staging_key="station-vehicles/newest",
    )
    ImportBatch.objects.create(
        department=department,
        actor=actor,
        domain=ImportBatch.Domain.HYDRANTS,
        import_format=ImportBatch.Format.CSV,
        import_mode=ImportBatch.Mode.MERGE,
        original_filename="hydrants.csv",
        upload_sha256="b" * 64,
        staging_key="station-vehicles/hydrants",
    )
    client.force_login(actor)
    response = client.get(reverse("ingestion-import-station-vehicles", args=(department.id,)))
    content = response.content.decode()
    assert response.status_code == 200
    assert "Import Stations and Vehicles" in content
    assert "Import and review" in content
    assert "Download Station and Vehicle CSV template" in content
    assert "newest.csv" in content and "hydrants.csv" not in content
    assert "Back to Stations" in content
    assert 'name="import_format"' in content and 'type="hidden"' in content
