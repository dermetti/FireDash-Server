import hashlib
import io
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.conf import settings
from django.contrib.gis.geos import Point
from django.db import close_old_connections, connection

from apps.accounts.models import User
from apps.assignments.models import PersonnelStationAssignment
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.ingestion.models import ImportBatch
from apps.ingestion.services import (
    ImportError,
    apply_preview,
    cancel_preview,
    create_preview,
    create_single_preview,
)
from apps.organizations.models import Department, Station
from apps.personnel.models import Person
from apps.publications.feature_services import set_department_feature
from apps.publications.models import (
    DatasetKeyGrant,
    DatasetPublication,
    DatasetScopeState,
    PublicationJob,
    SignedManifest,
)
from apps.reference_data.models import FirePlan, Hydrant, KlgvPlan
from apps.reference_data.services import set_fire_plan_active, set_klgv_plan_active


@pytest.fixture
def context(db, settings, tmp_path):
    settings.INGESTION_STAGING_ROOT = tmp_path / "private-import-staging"
    actor = User.objects.create_user("import-admin@example.test", "Import Admin", "safe-password")
    department = Department.objects.create(name="Ingestion", short_code="ING", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    return actor, department


def hydrant_csv(*rows):
    header = "external_identifier,longitude,latitude,hydrant_type,diameter_mm,status\n"
    return (header + "\n".join(rows)).encode()


def pdf_package(manifest: str, filename: str, pdf: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.csv", manifest)
        archive.writestr(filename, pdf)
    return output.getvalue()


@pytest.fixture
def sanitizer_stub(settings, tmp_path, monkeypatch):
    quarantine_root = tmp_path / "quarantine"
    output_root = tmp_path / "sanitized"
    accepted_root = tmp_path / "accepted"
    settings.REFERENCE_DATA_QUARANTINE_ROOT = quarantine_root
    settings.REFERENCE_DATA_SANITIZER_OUTPUT_ROOT = output_root
    settings.REFERENCE_DATA_ACCEPTED_ROOT = accepted_root

    def write(upload):
        path = quarantine_root / "job" / "input.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(upload.read())
        return path

    def output_path(*, job_id):
        path = output_root / job_id / "sanitized.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    calls: list[object] = []
    settings.INGESTION_TEST_SANITIZER_CALLS = calls

    def sanitize(*, quarantined_input, sanitized_output):
        calls.append(quarantined_input)
        shutil.copyfile(quarantined_input, sanitized_output)

    def validate(path, **kwargs):
        content = path.read_bytes()
        return len(content), 1, hashlib.sha256(content).hexdigest()

    def promote(source, key):
        accepted_root.mkdir(parents=True, exist_ok=True)
        destination = accepted_root / key
        shutil.move(source, destination)
        return destination

    monkeypatch.setattr("apps.ingestion.services.write_quarantine", write)
    monkeypatch.setattr("apps.ingestion.services.output_path", output_path)
    monkeypatch.setattr("apps.ingestion.services.sanitize", sanitize)
    monkeypatch.setattr("apps.ingestion.services.validate_pdf", validate)
    monkeypatch.setattr("apps.ingestion.services.promote_to_accepted", promote)


@pytest.mark.django_db(transaction=True)
def test_preview_is_side_effect_free_and_apply_dirties_one_scope(context):
    actor, department = context
    batch = create_preview(
        actor=actor,
        department=department,
        domain="hydrants",
        import_format="csv",
        import_mode="merge",
        filename="hydrants.csv",
        payload=hydrant_csv("H-1,8.1,50.2,wet,150,ACTIVE", "H-2,8.2,50.3,dry,,ACTIVE"),
    )
    assert batch.status == ImportBatch.Status.PREVIEW_READY
    assert not Hydrant.objects.exists()
    assert not DatasetScopeState.objects.exists()
    assert not DatasetPublication.objects.exists()
    assert not DatasetKeyGrant.objects.exists()
    assert not SignedManifest.objects.exists()

    apply_preview(actor=actor, batch_id=batch.id)
    scope = DatasetScopeState.objects.get(
        department=department, dataset_type_code="department_hydrants"
    )
    assert Hydrant.objects.count() == 2
    assert scope.source_revision == 1
    assert ImportBatch.objects.get(pk=batch.id).affected_scopes == [
        {"dataset_type_code": "department_hydrants", "station_id": None}
    ]
    assert AuditEvent.objects.filter(action="ingestion.applied", target_uuid=batch.id).exists()


@pytest.mark.django_db(transaction=True)
def test_noop_and_double_confirm_do_not_dirty_twice(context):
    actor, department = context
    payload = hydrant_csv("H-1,8.1,50.2,wet,150,ACTIVE")
    first = create_preview(
        actor=actor,
        department=department,
        domain="hydrants",
        import_format="csv",
        import_mode="merge",
        filename="h.csv",
        payload=payload,
    )
    apply_preview(actor=actor, batch_id=first.id)
    second = create_preview(
        actor=actor,
        department=department,
        domain="hydrants",
        import_format="csv",
        import_mode="merge",
        filename="h.csv",
        payload=payload,
    )
    apply_preview(actor=actor, batch_id=second.id)
    scope = DatasetScopeState.objects.get(
        department=department, dataset_type_code="department_hydrants"
    )
    assert scope.source_revision == 1
    with pytest.raises(ImportError, match="not confirmable"):
        apply_preview(actor=actor, batch_id=second.id)


@pytest.mark.django_db(transaction=True)
def test_hash_mismatch_and_stale_preview_fail_closed(context):
    actor, department = context
    batch = create_preview(
        actor=actor,
        department=department,
        domain="hydrants",
        import_format="csv",
        import_mode="merge",
        filename="h.csv",
        payload=hydrant_csv("H-1,8.1,50.2,wet,150,ACTIVE"),
    )
    path = settings.INGESTION_STAGING_ROOT / batch.staging_key
    path.write_bytes(b"tampered")
    with pytest.raises(ImportError, match="changed"):
        apply_preview(actor=actor, batch_id=batch.id)
    assert not Hydrant.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_authoritative_snapshot_deactivates_only_in_scope(context):
    actor, department = context
    Hydrant.objects.create(
        department=department,
        external_identifier="OLD",
        location=Point(8, 50, srid=4326),
        status="ACTIVE",
    )
    batch = create_preview(
        actor=actor,
        department=department,
        domain="hydrants",
        import_format="csv",
        import_mode="authoritative_snapshot",
        filename="h.csv",
        payload=hydrant_csv("NEW,8.1,50.2,wet,150,ACTIVE"),
    )
    assert batch.deactivate_count == 1
    apply_preview(actor=actor, batch_id=batch.id)
    assert (
        Hydrant.objects.get(department=department, external_identifier="OLD").status == "INACTIVE"
    )


@pytest.mark.django_db(transaction=True)
def test_invalid_preview_has_no_staged_canonical_side_effects(context):
    actor, department = context
    batch = create_preview(
        actor=actor,
        department=department,
        domain="hydrants",
        import_format="csv",
        import_mode="merge",
        filename="bad.csv",
        payload=b"wrong\nvalue\n",
    )
    assert batch.status == ImportBatch.Status.INVALID
    assert not Hydrant.objects.exists()
    assert not DatasetScopeState.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_cancelled_preview_never_mutates_canonical_data_or_publication_state(context):
    actor, department = context
    batch = create_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.HYDRANTS,
        import_format=ImportBatch.Format.CSV,
        import_mode=ImportBatch.Mode.MERGE,
        filename="h.csv",
        payload=hydrant_csv("H-CANCEL,8.1,50.2,wet,150,ACTIVE"),
    )
    cancel_preview(actor=actor, batch_id=batch.id)
    assert ImportBatch.objects.get(pk=batch.id).status == ImportBatch.Status.CANCELLED
    assert not Hydrant.objects.filter(department=department).exists()
    assert not DatasetScopeState.objects.exists()
    with pytest.raises(ImportError, match="not confirmable"):
        apply_preview(actor=actor, batch_id=batch.id)


@pytest.mark.django_db(transaction=True)
def test_manual_hydrant_uses_same_preview_apply_and_noop_rules(context):
    actor, department = context
    first = create_single_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.HYDRANTS,
        values={
            "external_identifier": "H-MANUAL",
            "longitude": 8.1,
            "latitude": 50.2,
            "hydrant_type": "wet",
            "diameter_mm": 100,
            "status": "ACTIVE",
        },
    )
    assert first.import_format == ImportBatch.Format.CSV
    apply_preview(actor=actor, batch_id=first.id)
    second = create_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.HYDRANTS,
        import_format=ImportBatch.Format.JSON,
        import_mode=ImportBatch.Mode.MERGE,
        filename="same.json",
        payload=(
            b'[{"external_identifier":"H-MANUAL","longitude":8.1,"latitude":50.2,'
            b'"hydrant_type":"wet","diameter_mm":100,"status":"ACTIVE"}]'
        ),
    )
    apply_preview(actor=actor, batch_id=second.id)
    assert (
        DatasetScopeState.objects.get(
            department=department, dataset_type_code="department_hydrants"
        ).source_revision
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_single_and_batch_personnel_inputs_share_identity_and_noop_rules(context):
    actor, department = context
    station = Station.objects.create(department=department, name="Station", short_code="ST")
    single = create_single_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.PERSONNEL,
        station=station,
        values={
            "personnel_number": "P-1",
            "first_name": "Alex",
            "last_name": "Member",
            "incident_commander_eligible": False,
        },
    )
    apply_preview(actor=actor, batch_id=single.id)
    batch = create_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.PERSONNEL,
        import_format=ImportBatch.Format.JSON,
        import_mode=ImportBatch.Mode.UPSERT,
        filename="personnel.json",
        payload=(
            b'[{"personnel_number":"P-1","first_name":"Alex","last_name":"Member",'
            b'"incident_commander_eligible":false}]'
        ),
    )
    apply_preview(actor=actor, batch_id=batch.id)
    assert Person.objects.filter(department=department, personnel_number="P-1").count() == 1
    assert (
        DatasetScopeState.objects.get(
            department=department, station=station, dataset_type_code="station_personnel"
        ).source_revision
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_postgresql_concurrent_double_confirm_applies_exactly_once(context):
    if connection.vendor != "postgresql":
        pytest.skip("ImportBatch locking coverage requires PostgreSQL.")
    actor, department = context
    batch = create_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.HYDRANTS,
        import_format=ImportBatch.Format.CSV,
        import_mode=ImportBatch.Mode.MERGE,
        filename="h.csv",
        payload=hydrant_csv("H-1,8.1,50.2,wet,150,ACTIVE"),
    )
    start = Event()

    def confirm_once():
        close_old_connections()
        start.wait(timeout=5)
        try:
            apply_preview(actor=User.objects.get(pk=actor.pk), batch_id=batch.id)
            return "applied"
        except ImportError:
            return "rejected"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(confirm_once) for _ in range(2)]
        start.set()
        outcomes = [future.result(timeout=10) for future in futures]

    assert sorted(outcomes) == ["applied", "rejected"]
    assert Hydrant.objects.filter(department=department, external_identifier="H-1").count() == 1
    assert (
        DatasetScopeState.objects.get(
            department=department, dataset_type_code="department_hydrants"
        ).source_revision
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_postgresql_stale_preview_from_another_connection_requires_repreview(context):
    if connection.vendor != "postgresql":
        pytest.skip("ImportBatch stale-preview coverage requires PostgreSQL.")
    actor, department = context
    batch = create_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.HYDRANTS,
        import_format=ImportBatch.Format.CSV,
        import_mode=ImportBatch.Mode.MERGE,
        filename="h.csv",
        payload=hydrant_csv("H-1,8.1,50.2,wet,150,ACTIVE"),
    )

    def concurrent_change():
        close_old_connections()
        try:
            Hydrant.objects.create(
                department=Department.objects.get(pk=department.pk),
                external_identifier="H-CONCURRENT",
                location=Point(8.2, 50.3, srid=4326),
                status=Hydrant.Status.ACTIVE,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(concurrent_change).result(timeout=10)
    with pytest.raises(ImportError, match="re-preview"):
        apply_preview(actor=actor, batch_id=batch.id)
    assert ImportBatch.objects.get(pk=batch.id).status == ImportBatch.Status.PREVIEW_READY


@pytest.mark.django_db(transaction=True)
def test_single_and_batch_fire_plan_inputs_share_identity_noop_and_dirty_once(
    context, sanitizer_stub
):
    actor, department = context
    source_pdf = b"test-pdf-content"
    single = create_single_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.FIRE_PLANS,
        values={
            "external_identifier": "FP-1",
            "object_name": "School",
            "address": "Main 1",
            "postal_code": "12345",
            "city": "Town",
            "longitude": "8.2",
            "latitude": "50.1",
        },
        pdf_bytes=source_pdf,
    )
    apply_preview(actor=actor, batch_id=single.id)
    original = FirePlan.objects.get(department=department, external_identifier="FP-1")
    assert original.source_pdf_sha256 == hashlib.sha256(source_pdf).hexdigest()

    manifest = (
        "external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,action\n"
        "FP-1,renamed.pdf,School,Main 1,12345,Town,8.2,50.1,upsert\n"
    )
    batch = create_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.FIRE_PLANS,
        import_format=ImportBatch.Format.ZIP,
        import_mode=ImportBatch.Mode.UPSERT,
        filename="fire-plans.zip",
        payload=pdf_package(manifest, "renamed.pdf", source_pdf),
    )
    assert batch.unchanged_count == 1
    apply_preview(actor=actor, batch_id=batch.id)
    assert FirePlan.objects.get(pk=original.pk).document_key == original.document_key
    assert (
        DatasetScopeState.objects.get(
            department=department, dataset_type_code="department_fire_plans"
        ).source_revision
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_address_identity_single_and_zip_inputs_converge_without_sanitizer_churn(
    context, sanitizer_stub, settings
):
    actor, department = context
    values: dict[str, object] = {
        "external_identifier": "",
        "object_name": "Bauhaus Wandsbek",
        "address": "Wandsbeker Zollstrasse 95",
        "postal_code": "22041",
        "city": "Hamburg",
        "longitude": "10.123",
        "latitude": "53.456",
    }
    first = create_single_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.FIRE_PLANS,
        values=values,
        pdf_bytes=b"address-plan",
    )
    apply_preview(actor=actor, batch_id=first.id)
    plan = FirePlan.objects.get(department=department, external_identifier="")
    manifest = (
        "external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,action\n"
        ",renamed.pdf,Bauhaus Wandsbek,Wandsbeker Zollstrasse 95,"
        "22041,Hamburg,10.123,53.456,upsert\n"
    )
    repeat = create_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.FIRE_PLANS,
        import_format=ImportBatch.Format.ZIP,
        import_mode=ImportBatch.Mode.UPSERT,
        filename="address-plan.zip",
        payload=pdf_package(manifest, "renamed.pdf", b"address-plan"),
    )
    assert (repeat.add_count, repeat.update_count, repeat.unchanged_count) == (0, 0, 1)
    assert len(settings.INGESTION_TEST_SANITIZER_CALLS) == 1
    apply_preview(actor=actor, batch_id=repeat.id)
    assert FirePlan.objects.get(pk=plan.pk).address == "Wandsbeker Zollstrasse 95"


@pytest.mark.django_db(transaction=True)
def test_fire_plan_external_identifier_address_change_is_update_but_address_identity_change_is_add(
    context, sanitizer_stub
):
    actor, department = context
    external: dict[str, object] = {
        "external_identifier": "PLAN-123",
        "object_name": "Plan",
        "address": "Old 1",
        "postal_code": "",
        "city": "",
        "longitude": "",
        "latitude": "",
    }
    apply_preview(
        actor=actor,
        batch_id=create_single_preview(
            actor=actor,
            department=department,
            domain=ImportBatch.Domain.FIRE_PLANS,
            values=external,
            pdf_bytes=b"same-pdf",
        ).id,
    )
    assert (
        create_single_preview(
            actor=actor,
            department=department,
            domain=ImportBatch.Domain.FIRE_PLANS,
            values=external | {"address": "New 1"},
            pdf_bytes=b"same-pdf",
        ).update_count
        == 1
    )
    address_identity = external | {"external_identifier": "", "address": "Address 1"}
    apply_preview(
        actor=actor,
        batch_id=create_single_preview(
            actor=actor,
            department=department,
            domain=ImportBatch.Domain.FIRE_PLANS,
            values=address_identity,
            pdf_bytes=b"same-pdf",
        ).id,
    )
    changed_address = create_single_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.FIRE_PLANS,
        values=address_identity | {"address": "Address 2"},
        pdf_bytes=b"same-pdf",
    )
    assert (changed_address.add_count, changed_address.update_count) == (1, 0)


@pytest.mark.django_db(transaction=True)
def test_single_and_batch_klgv_inputs_share_normal_publication_scope(context, sanitizer_stub):
    actor, department = context
    set_department_feature(
        actor=actor, department=department, feature_code="klgv_plans", enabled=True
    )
    source_pdf = b"test-klgv-pdf"
    single = create_single_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.KLGV_PLANS,
        values={"external_id": "KLGV-1", "title": "Plan", "category": "site"},
        pdf_bytes=source_pdf,
    )
    apply_preview(actor=actor, batch_id=single.id)
    plan = KlgvPlan.objects.get(department=department, external_identifier="KLGV-1")
    assert plan.source_pdf_sha256 == hashlib.sha256(source_pdf).hexdigest()
    assert (
        DatasetScopeState.objects.get(
            department=department, dataset_type_code="department_klgv_plans"
        ).source_revision
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_personnel_repeat_is_semantic_noop_for_manual_csv_and_json(context):
    actor, department = context
    station = Station.objects.create(department=department, name="Home", short_code="HOM")
    values = {
        "personnel_number": "P-1",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "incident_commander_eligible": True,
    }
    first = create_single_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.PERSONNEL,
        values=values,
        station=station,
    )
    apply_preview(actor=actor, batch_id=first.id)
    person = Person.objects.get(department=department, personnel_number="P-1")
    updated_at = person.updated_at
    scope = DatasetScopeState.objects.get(department=department, station=station)
    revision = scope.source_revision
    job_count = PublicationJob.objects.filter(department=department).count()
    for import_format, payload in (
        (
            ImportBatch.Format.CSV,
            b"personnel_number,first_name,last_name,incident_commander_eligible\nP-1,Ada,Lovelace,true\n",
        ),
        (
            ImportBatch.Format.JSON,
            b'[{"personnel_number":"P-1","first_name":"Ada","last_name":"Lovelace","incident_commander_eligible":true}]',
        ),
    ):
        repeat = create_preview(
            actor=actor,
            department=department,
            domain=ImportBatch.Domain.PERSONNEL,
            import_format=import_format,
            import_mode=ImportBatch.Mode.UPSERT,
            filename="people",
            payload=payload,
            station=station,
        )
        assert (repeat.add_count, repeat.update_count, repeat.unchanged_count) == (0, 0, 1)
        apply_preview(actor=actor, batch_id=repeat.id)
    person.refresh_from_db()
    scope.refresh_from_db()
    assert person.updated_at == updated_at
    assert scope.source_revision == revision
    assert PublicationJob.objects.filter(department=department).count() == job_count


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("domain", [ImportBatch.Domain.FIRE_PLANS, ImportBatch.Domain.KLGV_PLANS])
def test_identical_pdf_reimport_skips_sanitizer_and_noop_dirtying(
    context, sanitizer_stub, settings, domain
):
    actor, department = context
    if domain == ImportBatch.Domain.KLGV_PLANS:
        set_department_feature(
            actor=actor, department=department, feature_code="klgv_plans", enabled=True
        )
        values: dict[str, object] = {"external_id": "D-1", "title": "Plan", "category": "site"}
        code = "department_klgv_plans"
    else:
        values = {
            "external_identifier": "D-1",
            "object_name": "Plan",
            "address": "Road",
            "postal_code": "",
            "city": "",
            "longitude": "",
            "latitude": "",
        }
        code = "department_fire_plans"
    first = create_single_preview(
        actor=actor, department=department, domain=domain, values=values, pdf_bytes=b"pdf-one"
    )
    assert len(settings.INGESTION_TEST_SANITIZER_CALLS) == 1
    apply_preview(actor=actor, batch_id=first.id)
    scope = DatasetScopeState.objects.get(department=department, dataset_type_code=code)
    revision = scope.source_revision
    repeat = create_single_preview(
        actor=actor, department=department, domain=domain, values=values, pdf_bytes=b"pdf-one"
    )
    assert (repeat.add_count, repeat.update_count, repeat.unchanged_count) == (0, 0, 1)
    assert len(settings.INGESTION_TEST_SANITIZER_CALLS) == 1
    apply_preview(actor=actor, batch_id=repeat.id)
    scope.refresh_from_db()
    assert scope.source_revision == revision


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "change",
    [
        {"first_name": "Grace"},
        {"last_name": "Hopper"},
        {"incident_commander_eligible": False},
    ],
)
def test_personnel_managed_field_changes_are_updates_and_stale_previews_fail(context, change):
    actor, department = context
    station = Station.objects.create(department=department, name="P", short_code="PER")
    values = {
        "personnel_number": "P-2",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "incident_commander_eligible": True,
    }
    apply_preview(
        actor=actor,
        batch_id=create_single_preview(
            actor=actor,
            department=department,
            domain=ImportBatch.Domain.PERSONNEL,
            values=values,
            station=station,
        ).id,
    )
    candidate = create_single_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.PERSONNEL,
        values=values | change,
        station=station,
    )
    assert (candidate.add_count, candidate.update_count, candidate.unchanged_count) == (0, 1, 0)
    applied = apply_preview(actor=actor, batch_id=candidate.id)
    assert (applied.add_count, applied.update_count, applied.unchanged_count) == (0, 1, 0)
    stale = create_single_preview(
        actor=actor,
        department=department,
        domain=ImportBatch.Domain.PERSONNEL,
        values=values | change,
        station=station,
    )
    person = Person.objects.get(department=department, personnel_number="P-2")
    person.first_name = "Changed"
    person.save(update_fields=("first_name", "updated_at"))
    with pytest.raises(ImportError, match="re-preview"):
        apply_preview(actor=actor, batch_id=stale.id)
    assert PersonnelStationAssignment.objects.filter(person=person, ended_at__isnull=True).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("domain", [ImportBatch.Domain.FIRE_PLANS, ImportBatch.Domain.KLGV_PLANS])
def test_pdf_document_convergence_update_stale_zip_and_lifecycle_matrix(
    context, sanitizer_stub, settings, domain
):
    actor, department = context
    model: type[FirePlan] | type[KlgvPlan]
    if domain == ImportBatch.Domain.KLGV_PLANS:
        set_department_feature(
            actor=actor, department=department, feature_code="klgv_plans", enabled=True
        )
        values: dict[str, object] = {"external_id": "M-1", "title": "Plan", "category": "one"}
        changed_values = values | {"title": "Renamed", "category": "two"}
        model, code = KlgvPlan, "department_klgv_plans"
        manifest = "external_id,filename,title,category,action\nM-1,same.pdf,Plan,one,upsert\n"
    else:
        values = {
            "external_identifier": "M-1",
            "object_name": "Plan",
            "address": "Road",
            "postal_code": "1",
            "city": "Town",
            "longitude": "",
            "latitude": "",
        }
        changed_values = values | {
            "object_name": "Renamed",
            "address": "New road",
            "postal_code": "2",
            "city": "Elsewhere",
        }
        model, code = FirePlan, "department_fire_plans"
        manifest = (
            "external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,action\n"
            "M-1,same.pdf,Plan,Road,1,Town,,,upsert\n"
        )
    first = create_single_preview(
        actor=actor, department=department, domain=domain, values=values, pdf_bytes=b"one"
    )
    assert first.add_count == 1
    apply_preview(actor=actor, batch_id=first.id)
    document = model.objects.get(department=department, external_identifier="M-1")
    scope = DatasetScopeState.objects.get(department=department, dataset_type_code=code)
    revision, updated_at, jobs = (
        scope.source_revision,
        document.updated_at,
        PublicationJob.objects.filter(department=department).count(),
    )
    zip_repeat = create_preview(
        actor=actor,
        department=department,
        domain=domain,
        import_format="zip",
        import_mode="upsert",
        filename="same.zip",
        payload=pdf_package(manifest, "same.pdf", b"one"),
    )
    assert (zip_repeat.update_count, zip_repeat.unchanged_count) == (0, 1)
    apply_preview(actor=actor, batch_id=zip_repeat.id)
    document.refresh_from_db()
    scope.refresh_from_db()
    assert (
        document.updated_at,
        scope.source_revision,
        PublicationJob.objects.filter(department=department).count(),
    ) == (updated_at, revision, jobs)
    metadata = create_single_preview(
        actor=actor, department=department, domain=domain, values=changed_values, pdf_bytes=b"one"
    )
    assert metadata.update_count == 1
    assert metadata.validation_summary["updates"]
    assert apply_preview(actor=actor, batch_id=metadata.id).update_count == 1
    replacement = create_single_preview(
        actor=actor, department=department, domain=domain, values=changed_values, pdf_bytes=b"two"
    )
    assert replacement.update_count == 1
    assert len(settings.INGESTION_TEST_SANITIZER_CALLS) == 2
    apply_preview(actor=actor, batch_id=replacement.id)
    stale = create_single_preview(
        actor=actor, department=department, domain=domain, values=changed_values, pdf_bytes=b"two"
    )
    document.refresh_from_db()
    document.active = False
    document.save(update_fields=("active", "updated_at"))
    with pytest.raises(ImportError, match="re-preview"):
        apply_preview(actor=actor, batch_id=stale.id)
    document.refresh_from_db()
    scope.refresh_from_db()
    before_revision, before_jobs, before_updated = (
        scope.source_revision,
        PublicationJob.objects.filter(department=department).count(),
        document.updated_at,
    )
    if domain == ImportBatch.Domain.KLGV_PLANS:
        assert isinstance(document, KlgvPlan)
        set_klgv_plan_active(actor=actor, klgv_plan=document, active=False)
    else:
        set_fire_plan_active(actor=actor, fire_plan=document, active=False)
    document.refresh_from_db()
    scope.refresh_from_db()
    assert (
        document.updated_at,
        scope.source_revision,
        PublicationJob.objects.filter(department=department).count(),
    ) == (before_updated, before_revision, before_jobs)
    if domain == ImportBatch.Domain.KLGV_PLANS:
        assert isinstance(document, KlgvPlan)
        set_klgv_plan_active(actor=actor, klgv_plan=document, active=True)
        set_klgv_plan_active(actor=actor, klgv_plan=document, active=True)
    else:
        set_fire_plan_active(actor=actor, fire_plan=document, active=True)
        set_fire_plan_active(actor=actor, fire_plan=document, active=True)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("domain", [ImportBatch.Domain.FIRE_PLANS, ImportBatch.Domain.KLGV_PLANS])
def test_pdf_zip_omission_and_lifecycle_noops_have_no_side_effects(
    context, sanitizer_stub, settings, domain
):
    actor, department = context
    model: type[FirePlan] | type[KlgvPlan]
    if domain == ImportBatch.Domain.KLGV_PLANS:
        set_department_feature(
            actor=actor, department=department, feature_code="klgv_plans", enabled=True
        )

        def values(identifier):
            return {"external_id": identifier, "title": identifier, "category": "site"}

        model, code = KlgvPlan, "department_klgv_plans"
        manifest = "external_id,filename,title,category,action\nA,a.pdf,A,site,upsert\n"
    else:

        def values(identifier):
            return {
                "external_identifier": identifier,
                "object_name": identifier,
                "address": "Road",
                "postal_code": "",
                "city": "",
                "longitude": "",
                "latitude": "",
            }

        model, code = FirePlan, "department_fire_plans"
        manifest = (
            "external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,action\n"
            "A,a.pdf,A,Road,,,,,upsert\n"
        )
    for identifier in ("A", "B"):
        apply_preview(
            actor=actor,
            batch_id=create_single_preview(
                actor=actor,
                department=department,
                domain=domain,
                values=values(identifier),
                pdf_bytes=identifier.encode(),
            ).id,
        )
    omitted = model.objects.get(department=department, external_identifier="B")
    scope = DatasetScopeState.objects.get(department=department, dataset_type_code=code)
    omitted_updated, revision, jobs, calls = (
        omitted.updated_at,
        scope.source_revision,
        PublicationJob.objects.filter(department=department).count(),
        len(settings.INGESTION_TEST_SANITIZER_CALLS),
    )
    only_a = create_preview(
        actor=actor,
        department=department,
        domain=domain,
        import_format="zip",
        import_mode="upsert",
        filename="only-a.zip",
        payload=pdf_package(manifest, "a.pdf", b"A"),
    )
    assert (only_a.deactivate_count, only_a.unchanged_count) == (0, 1)
    apply_preview(actor=actor, batch_id=only_a.id)
    omitted.refresh_from_db()
    scope.refresh_from_db()
    assert (
        omitted.active,
        omitted.updated_at,
        scope.source_revision,
        PublicationJob.objects.filter(department=department).count(),
        len(settings.INGESTION_TEST_SANITIZER_CALLS),
    ) == (True, omitted_updated, revision, jobs, calls)
    if domain == ImportBatch.Domain.KLGV_PLANS:
        assert isinstance(omitted, KlgvPlan)
        set_klgv_plan_active(actor=actor, klgv_plan=omitted, active=False)
    else:
        assert isinstance(omitted, FirePlan)
        set_fire_plan_active(actor=actor, fire_plan=omitted, active=False)
    omitted.refresh_from_db()
    scope.refresh_from_db()
    after_deactivate = (
        omitted.updated_at,
        scope.source_revision,
        PublicationJob.objects.filter(department=department).count(),
    )
    if domain == ImportBatch.Domain.KLGV_PLANS:
        assert isinstance(omitted, KlgvPlan)
        set_klgv_plan_active(actor=actor, klgv_plan=omitted, active=False)
        set_klgv_plan_active(actor=actor, klgv_plan=omitted, active=True)
    else:
        set_fire_plan_active(actor=actor, fire_plan=omitted, active=False)
        set_fire_plan_active(actor=actor, fire_plan=omitted, active=True)
    omitted.refresh_from_db()
    scope.refresh_from_db()
    assert (
        omitted.updated_at,
        scope.source_revision,
        PublicationJob.objects.filter(department=department).count(),
    ) != after_deactivate
    after_activate = (
        omitted.updated_at,
        scope.source_revision,
        PublicationJob.objects.filter(department=department).count(),
    )
    if domain == ImportBatch.Domain.KLGV_PLANS:
        assert isinstance(omitted, KlgvPlan)
        set_klgv_plan_active(actor=actor, klgv_plan=omitted, active=True)
    else:
        set_fire_plan_active(actor=actor, fire_plan=omitted, active=True)
    omitted.refresh_from_db()
    scope.refresh_from_db()
    assert (
        omitted.updated_at,
        scope.source_revision,
        PublicationJob.objects.filter(department=department).count(),
    ) == after_activate
