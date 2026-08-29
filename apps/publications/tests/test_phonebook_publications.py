import json
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.db import DatabaseError
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station, Vehicle
from apps.publications.artifacts import build_encrypted_artifact
from apps.publications.builders import build_artifact, build_source_payload, source_fingerprint
from apps.publications.hpke import (
    HPKE_CIPHERSUITE,
    HPKEContext,
    hpke_open,
    serialize_p256_public_key,
)
from apps.publications.manifests import (
    ManifestError,
    authorized_publications,
    request_dataset_key_grant,
)
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.registry import get_dataset_definition
from apps.publications.services import mark_dirty
from apps.publications.worker_grants import process_next_dataset_key_grant
from apps.reference_data.services import (
    create_phonebook_entry,
    delete_phonebook_entry,
    update_phonebook_entry,
)
from apps.tablets.models import AppInstallation, Tablet


@pytest.fixture
def phonebook_scope(db):
    actor = User.objects.create_user("publication-phonebook@example.test", "Publisher", "password")
    department = Department.objects.create(name="One", short_code="ONE", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    first = Station.objects.create(department=department, name="First", short_code="F1")
    second = Station.objects.create(department=department, name="Second", short_code="F2")
    return actor, department, first, second


@pytest.mark.django_db
def test_phonebook_projection_is_pure_scoped_and_deterministic(phonebook_scope):
    actor, department, first, second = phonebook_scope
    department_entry = create_phonebook_entry(
        actor=actor,
        department=department,
        organization_unit="Control",
        phone_number="040 428512300",
    )
    first_entry = create_phonebook_entry(
        actor=actor,
        department=department,
        station=first,
        organization_unit="First",
        phone_number="040 428512301",
    )
    create_phonebook_entry(
        actor=actor,
        department=department,
        station=second,
        organization_unit="Second",
        phone_number="040 428512302",
    )
    department_definition = get_dataset_definition("department_phonebook")
    station_definition = get_dataset_definition("station_phonebook")
    department_payload = build_source_payload(
        definition=department_definition, department=department, station=None
    )
    station_payload = build_source_payload(
        definition=station_definition, department=department, station=first
    )
    assert [entry["id"] for entry in department_payload["entries"]] == [str(department_entry.id)]
    assert [entry["id"] for entry in station_payload["entries"]] == [str(first_entry.id)]
    artifact = build_artifact(
        definition=station_definition, department=department, station=first, source_revision=1
    )
    assert json.loads(artifact)["entries"] == station_payload["entries"]
    assert source_fingerprint(
        definition=department_definition, department=department, station=None
    ) == source_fingerprint(definition=department_definition, department=department, station=None)


@pytest.mark.django_db
def test_phonebook_mutations_dirty_only_old_and_new_pure_scopes(phonebook_scope):
    actor, department, first, second = phonebook_scope
    entry = create_phonebook_entry(
        actor=actor,
        department=department,
        station=first,
        organization_unit="First",
        phone_number="1",
    )
    assert DatasetScopeState.objects.filter(
        department=department, station=first, dataset_type_code="station_phonebook"
    ).exists()
    assert not DatasetScopeState.objects.filter(
        department=department, station__isnull=True, dataset_type_code="department_phonebook"
    ).exists()
    update_phonebook_entry(actor=actor, entry=entry, station=second)
    assert DatasetScopeState.objects.filter(
        department=department, station=second, dataset_type_code="station_phonebook"
    ).exists()
    update_phonebook_entry(actor=actor, entry=entry, station=None)
    assert DatasetScopeState.objects.filter(
        department=department, station=None, dataset_type_code="department_phonebook"
    ).exists()
    delete_phonebook_entry(actor=actor, entry=entry)
    assert PublicationJob.objects.filter(
        dataset_type_code__in=("department_phonebook", "station_phonebook"), department=department
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_postgres_scope_guard_accepts_phonebook_and_rejects_invalid_scope(phonebook_scope):
    actor, department, first, _ = phonebook_scope
    department_scope = mark_dirty(
        department=department, dataset_type_code="department_phonebook", actor=actor
    )
    station_scope = mark_dirty(
        department=department, station=first, dataset_type_code="station_phonebook", actor=actor
    )
    assert department_scope.station_id is None
    assert station_scope.station_id == first.id
    with pytest.raises(DatabaseError, match="Dataset type station scope is invalid"):
        DatasetScopeState.objects.create(
            department=department, station=first, dataset_type_code="department_phonebook"
        )
    with pytest.raises(DatabaseError, match="Dataset type station scope is invalid"):
        DatasetScopeState.objects.create(
            department=department, dataset_type_code="station_phonebook"
        )
    foreign_department = Department.objects.create(
        name="Foreign", short_code="FOR", created_by=actor
    )
    foreign_station = Station.objects.create(
        department=foreign_department, name="Foreign station", short_code="FS"
    )
    with pytest.raises(DatabaseError, match="Station must belong to the scope department"):
        DatasetScopeState.objects.create(
            department=department, station=foreign_station, dataset_type_code="station_phonebook"
        )
    with pytest.raises(DatabaseError, match="Unknown dataset type code"):
        DatasetScopeState.objects.create(department=department, dataset_type_code="unknown")
    assert (
        mark_dirty(
            department=department, dataset_type_code="department_hydrants", actor=actor
        ).dataset_type_code
        == "department_hydrants"
    )


@pytest.mark.django_db(transaction=True)
def test_phonebook_json_artifact_and_hpke_grant_obey_existing_authorization_contract(
    phonebook_scope, request
):
    actor, department, first, second = phonebook_scope
    create_phonebook_entry(
        actor=actor,
        department=department,
        organization_unit="Department control",
        phone_number="040 428512300",
    )
    create_phonebook_entry(
        actor=actor,
        department=department,
        station=first,
        organization_unit="Station control",
        phone_number="040 428512301",
    )
    private_key = ec.generate_private_key(ec.SECP256R1())
    tablet = Tablet.objects.create(
        department=department, display_name="First tablet", status=Tablet.Status.ACTIVE
    )
    vehicle = Vehicle.objects.create(department=department, station=first, display_name="Engine")
    TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=timezone.now(), created_by=actor
    )
    installation = AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=tablet.id,
        credential_hash="a" * 64,
        status=AppInstallation.Status.ACTIVE,
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=timezone.now(),
        hpke_public_key=serialize_p256_public_key(private_key.public_key()),
        hpke_ciphersuite=HPKE_CIPHERSUITE,
        hpke_key_fingerprint="b" * 64,
        hpke_key_verified_at=timezone.now(),
        adopted_at=timezone.now(),
        authorization_valid_until=timezone.now() + timedelta(days=1),
    )
    test_root = Path.cwd() / ".private" / f"phonebook-publication-contract-{uuid.uuid4()}"
    test_root.mkdir(parents=True, exist_ok=True)
    request.addfinalizer(lambda: shutil.rmtree(test_root, ignore_errors=True))
    kek_path, signing_path = test_root / "kek", test_root / "signing"
    kek_path.write_bytes(b"k" * 32)
    signing_path.write_bytes(b"s" * 32)
    publications = []
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=test_root / "artifacts",
        PUBLICATION_ARTIFACT_TEMP_ROOT=test_root / "artifacts" / ".tmp",
        PUBLICATION_KEK_CREDENTIAL_PATH=kek_path,
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=signing_path,
        PUBLICATION_KEK_VERSION="1",
        PUBLICATION_SIGNING_KEY_VERSION="1",
    ):
        for code, dataset_station in (("department_phonebook", None), ("station_phonebook", first)):
            scope = DatasetScopeState.objects.get(
                department=department, station=dataset_station, dataset_type_code=code
            )
            publication = DatasetPublication.objects.get(scope_state=scope, version_number=1)
            publication.status = DatasetPublication.Status.BUILDING
            publication.save(update_fields=("status",))
            metadata = build_encrypted_artifact(
                publication=publication,
                plaintext=build_artifact(
                    definition=get_dataset_definition(code),
                    department=department,
                    station=dataset_station,
                    source_revision=1,
                ),
            )
            for field, value in metadata.items():
                setattr(publication, field, value)
            publication.artifact_ready = True
            publication.artifact_status = DatasetPublication.ArtifactStatus.READY
            publication.status = DatasetPublication.Status.PUBLISHED
            publication.save()
            scope.current_published_publication = publication
            scope.save(update_fields=("current_published_publication",))
            publications.append(publication)

        _, assigned_vehicle, discovered = authorized_publications(installation=installation)
        assert assigned_vehicle.station_id == first.id
        assert {publication.dataset_type_code for publication in discovered} == {
            "department_phonebook",
            "station_phonebook",
        }
        for publication in publications:
            request_dataset_key_grant(publication=publication, installation=installation)
        while process_next_dataset_key_grant() is not None:
            pass
        department_publication = next(
            publication
            for publication in publications
            if publication.dataset_type_code == "department_phonebook"
        )
        grant = department_publication.key_grants.get(app_installation=installation)
        cek = hpke_open(
            encapsulated_key=bytes(grant.hpke_encapsulated_key),
            ciphertext=bytes(grant.hpke_wrapped_content_key),
            recipient_private_key=private_key,
            context=HPKEContext(
                publication_id=department_publication.id,
                installation_id=installation.id,
                tablet_id=tablet.id,
                department_id=department.id,
                station_id=None,
                dataset_type_code="department_phonebook",
                version_number=1,
                schema_version=1,
                ciphertext_sha256=department_publication.artifact_sha256,
            ),
        )
        ciphertext = (test_root / "artifacts" / department_publication.artifact_path).read_bytes()
        assert json.loads(
            AESGCM(cek).decrypt(bytes(department_publication.artifact_nonce), ciphertext, None)
        )["entries"][0]["organization_unit"] == "Department control"

        wrong_station_tablet = Tablet.objects.create(
            department=department, display_name="Second tablet", status=Tablet.Status.ACTIVE
        )
        wrong_station_vehicle = Vehicle.objects.create(
            department=department, station=second, display_name="Second engine"
        )
        TabletVehicleAssignment.objects.create(
            tablet=wrong_station_tablet,
            vehicle=wrong_station_vehicle,
            valid_from=timezone.now(),
            created_by=actor,
        )
        wrong_station_installation = AppInstallation.objects.create(
            tablet=wrong_station_tablet,
            installation_uuid=wrong_station_tablet.id,
            credential_hash="c" * 64,
            status=AppInstallation.Status.ACTIVE,
            app_version="1.0.0",
            adopted_app_version="1.0.0",
            app_version_seen_at=timezone.now(),
            hpke_public_key=serialize_p256_public_key(ec.generate_private_key(ec.SECP256R1()).public_key()),
            hpke_ciphersuite=HPKE_CIPHERSUITE,
            hpke_key_fingerprint="d" * 64,
            hpke_key_verified_at=timezone.now(),
            adopted_at=timezone.now(),
            authorization_valid_until=timezone.now() + timedelta(days=1),
        )
        with pytest.raises(ManifestError, match="not authorized"):
            request_dataset_key_grant(
                publication=next(
                    publication
                    for publication in publications
                    if publication.dataset_type_code == "station_phonebook"
                ),
                installation=wrong_station_installation,
            )

        foreign_department = Department.objects.create(
            name="Foreign", short_code="FOR", created_by=actor
        )
        foreign_station = Station.objects.create(
            department=foreign_department, name="Foreign station", short_code="FOR1"
        )
        foreign_tablet = Tablet.objects.create(
            department=foreign_department,
            display_name="Foreign tablet",
            status=Tablet.Status.ACTIVE,
        )
        foreign_vehicle = Vehicle.objects.create(
            department=foreign_department, station=foreign_station, display_name="Foreign engine"
        )
        TabletVehicleAssignment.objects.create(
            tablet=foreign_tablet,
            vehicle=foreign_vehicle,
            valid_from=timezone.now(),
            created_by=actor,
        )
        foreign_installation = AppInstallation.objects.create(
            tablet=foreign_tablet,
            installation_uuid=foreign_tablet.id,
            credential_hash="e" * 64,
            status=AppInstallation.Status.ACTIVE,
            app_version="1.0.0",
            adopted_app_version="1.0.0",
            app_version_seen_at=timezone.now(),
            hpke_public_key=serialize_p256_public_key(ec.generate_private_key(ec.SECP256R1()).public_key()),
            hpke_ciphersuite=HPKE_CIPHERSUITE,
            hpke_key_fingerprint="f" * 64,
            hpke_key_verified_at=timezone.now(),
            adopted_at=timezone.now(),
            authorization_valid_until=timezone.now() + timedelta(days=1),
        )
        with pytest.raises(ManifestError, match="not authorized"):
            request_dataset_key_grant(
                publication=department_publication, installation=foreign_installation
            )
