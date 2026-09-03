import base64
import hashlib
import hmac
import json
import uuid
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.authorization.models import DepartmentMembership
from apps.ingestion.dangerous_goods import validate_dangerous_goods
from apps.ingestion.services import apply_dangerous_goods_preview, create_dangerous_goods_preview
from apps.organizations.models import Department, Station, Vehicle
from apps.publications.artifacts import _signature_payload
from apps.publications.hpke import (
    HPKE_CIPHERSUITE,
    HPKEContext,
    hpke_open,
    serialize_p256_public_key,
)
from apps.publications.manifests import canonical_manifest_payload, request_manifest
from apps.publications.models import DatasetPublication, PublicationJob, SignedManifest
from apps.publications.services import process_next_job
from apps.publications.worker_grants import (
    process_next_dataset_key_grant,
    process_next_signed_manifest,
)
from apps.tablets.models import AppInstallation, Tablet
from apps.tablets.services import generate_credential


def compact_document() -> dict[str, object]:
    """Small, representative schema-1 fixture; never use a curated production file."""
    cards = {
        "3-01": [["title", "Kraftstoff"], ["heading", "Gefahr"], ["item", "Abstand halten."]],
        "7-01": [["title", "Radioaktiv"], ["item", "Fachberatung hinzuziehen."]],
    }
    return {
        "dataset_type": "dangerous_goods",
        "schema_version": 1,
        "metadata": {
            "publication_profile": "compact",
            "record_count": 5,
            "eri_card_count": len(cards),
            "default_name_language": "de",
            "placard_catalog": {
                "scheme": "adr",
                "delivery": "bundled_with_tablet_app",
                "available_assets": {
                    "3": "unused",
                    "6.1": "unused",
                    "7A": "unused",
                    "7B": "unused",
                    "7C": "unused",
                },
                "special_values": {
                    "7X": {
                        "kind": "variable",
                        "candidate_codes": ["7A", "7B", "7C"],
                        "selection_basis": "transport_index_and_dose_rate",
                    }
                },
            },
        },
        "goods": [
            {
                "id": "bam-fixed",
                "un_number": "1203",
                "names": {
                    "official": {"de": "BENZIN", "en": "GASOLINE", "fr": "ESSENCE"},
                    "aliases": {"de": ["MOTORBENZIN"]},
                },
                "adr": {
                    "hazard_identification_number": "33",
                    "class": "3",
                    "classification_code": "F1",
                    "packing_group": "II",
                    "placards": ["3"],
                },
                "eri": ["3-01"],
            },
            {
                "id": "bam-conditional",
                "un_number": "1204",
                "names": {"official": {"es": "CONDICIONAL"}},
                "adr": {"placards": [{"kind": "conditional", "code": "6.1"}]},
                "eri": ["3-01"],
            },
            {
                "id": "bam-variable",
                "un_number": "2919",
                "names": {"official": {"de": "RADIOAKTIV"}},
                "adr": {
                    "placards": [
                        {
                            "kind": "variable",
                            "candidate_codes": ["7A", "7B", "7C"],
                            "selection_basis": "transport_index_and_dose_rate",
                        }
                    ]
                },
                "eri": ["7-01"],
            },
            {
                "id": "bam-none",
                "un_number": "1205",
                "names": {"official": {"de": "OHNE"}},
                "adr": {"placards": [{"kind": "none"}]},
                "eri": [],
            },
            {
                "id": "bam-reference",
                "un_number": "1206",
                "names": {"official": {"de": "VERWEIS"}},
                "adr": {"placards": [{"kind": "reference", "reference": "ADR 5.2.2.1.12"}]},
                "eri": None,
            },
        ],
        "eri_defaults": {
            "1203": "3-01",
            "1204": "3-01",
            "2919": "7-01",
            "1205": "3-01",
            "1206": "3-01",
        },
        "eri_cards": cards,
        "sources": [
            {
                "id": "bam",
                "provider": "BAM",
                "dataset": "ADR",
                "source_file": "bam.json",
                "sha256": "source",
                "source_url": "https://example.test/bam",
                "legal": {
                    "legal_url": "https://example.test/legal",
                    "license": {},
                    "attribution": {},
                    "processing": {},
                },
            },
            {
                "id": "ericards",
                "provider": "Cefic",
                "dataset": "ERI",
                "source_file": "eri.json",
                "sha256": "source",
                "source_url": "https://example.test/eri",
                "legal": {
                    "terms_url": "https://example.test/terms",
                    "guidance_url": "https://example.test/guidance",
                    "disclaimer_url": "https://example.test/disclaimer",
                    "attribution": {},
                    "reproduction": {},
                },
            },
        ],
    }


def compact_payload() -> bytes:
    return json.dumps(compact_document(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _authorization(credential: str) -> dict[str, str]:
    return {"HTTP_AUTHORIZATION": f"Bearer {credential}"}


def _installation(*, department: Department, actor: User, private_key):
    now = timezone.now()
    station = Station.objects.create(
        department=department, name="Contract station", short_code=f"S{department.short_code}"
    )
    vehicle = Vehicle.objects.create(
        department=department, station=station, display_name="Contract engine"
    )
    tablet = Tablet.objects.create(
        department=department, display_name="Contract tablet", status=Tablet.Status.ACTIVE
    )
    credential = generate_credential()
    installation = AppInstallation.objects.create(
        tablet=tablet,
        installation_uuid=uuid.uuid4(),
        credential_hash=hmac.new(
            settings.SECRET_KEY.encode(), credential.encode(), hashlib.sha256
        ).hexdigest(),
        status=AppInstallation.Status.ACTIVE,
        app_version="1.0.0",
        adopted_app_version="1.0.0",
        app_version_seen_at=now,
        hpke_public_key=serialize_p256_public_key(private_key.public_key()),
        hpke_ciphersuite=HPKE_CIPHERSUITE,
        hpke_key_fingerprint="a" * 64,
        hpke_key_verified_at=now,
        adopted_at=now,
        authorization_valid_until=now + timedelta(days=1),
    )
    TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=now, created_by=actor
    )
    return installation, credential


def test_compact_fixture_freezes_documented_schema_forms():
    payload = compact_payload()
    document, summary = validate_dangerous_goods(payload)
    assert summary == {"goods_count": 5, "eri_card_count": 2}
    assert document["metadata"]["publication_profile"] == "compact"
    assert document["goods"][0]["names"]["official"] == {
        "de": "BENZIN",
        "en": "GASOLINE",
        "fr": "ESSENCE",
    }
    assert document["goods"][0]["names"]["aliases"] == {"de": ["MOTORBENZIN"]}
    assert document["eri_defaults"]["1203"] == "3-01"
    assert document["eri_cards"]["3-01"] == [
        ["title", "Kraftstoff"],
        ["heading", "Gefahr"],
        ["item", "Abstand halten."],
    ]
    placards = [good["adr"]["placards"][0] for good in document["goods"]]
    assert placards == [
        "3",
        {"kind": "conditional", "code": "6.1"},
        {
            "kind": "variable",
            "candidate_codes": ["7A", "7B", "7C"],
            "selection_basis": "transport_index_and_dose_rate",
        },
        {"kind": "none"},
        {"kind": "reference", "reference": "ADR 5.2.2.1.12"},
    ]


@pytest.mark.django_db(transaction=True)
def test_tablet_receives_required_dangerous_goods_through_generic_encrypted_lifecycle(
    settings, tmp_path, monkeypatch
):
    settings.INGESTION_STAGING_ROOT = tmp_path / "staging"
    actor = User.objects.create_user("contract@example.test", "Contract", "password")
    department = Department.objects.create(name="Contract", short_code="DGC", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    payload = compact_payload()
    preview = create_dangerous_goods_preview(
        actor=actor, department=department, filename="dangerous_goods_v1.json", payload=payload
    )
    apply_dangerous_goods_preview(actor=actor, batch_id=preview.id)
    job = PublicationJob.objects.get(department=department, dataset_type_code="dangerous_goods")
    job.not_before = timezone.now()
    job.save(update_fields=("not_before",))

    kek_path, signing_path, ring_path = (
        tmp_path / "kek",
        tmp_path / "signing",
        tmp_path / "ring.json",
    )
    kek_path.write_bytes(b"k" * 32)
    signing_path.write_bytes(b"s" * 32)
    signing_public = Ed25519PrivateKey.from_private_bytes(b"s" * 32).public_key().public_bytes_raw()
    ring_path.write_text(
        json.dumps({"keys": {"1": base64.b64encode(signing_public).decode("ascii")}}),
        encoding="ascii",
    )
    monkeypatch.setattr("apps.publications.artifacts.grp", None)
    private_key = ec.generate_private_key(ec.SECP256R1())
    with override_settings(
        PUBLICATION_ARTIFACT_ROOT=tmp_path / "artifacts",
        PUBLICATION_ARTIFACT_TEMP_ROOT=tmp_path / "temp",
        PUBLICATION_KEK_CREDENTIAL_PATH=kek_path,
        PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH=signing_path,
        PUBLICATION_SIGNING_PUBLIC_KEY_RING_CREDENTIAL_PATH=ring_path,
    ):
        completed = process_next_job()
        assert completed is not None and completed.status == PublicationJob.Status.SUCCEEDED
        publication = DatasetPublication.objects.get(
            department=department, dataset_type_code="dangerous_goods"
        )
        installation, credential = _installation(
            department=department, actor=actor, private_key=private_key
        )
        assert request_manifest(installation=installation).unavailable
        assert process_next_signed_manifest().status == SignedManifest.Status.PENDING
        assert process_next_dataset_key_grant().status.name == "READY"
        assert process_next_signed_manifest().status == SignedManifest.Status.READY

        client = Client()
        manifest_response = client.get("/api/v1/tablet/manifest", **_authorization(credential))
        assert manifest_response.status_code == 200
        manifest = manifest_response.json()
        unsigned = {key: value for key, value in manifest.items() if key != "signature"}
        Ed25519PrivateKey.from_private_bytes(b"s" * 32).public_key().verify(
            base64.b64decode(manifest["signature"]), canonical_manifest_payload(unsigned)
        )
        entry = next(item for item in manifest["datasets"] if item["type"] == "dangerous_goods")
        assert manifest["configuration"]["department_id"] == str(department.id)
        assert {
            key: entry[key]
            for key in ("type", "scope", "schema_version", "required", "artifact_format")
        } == {
            "type": "dangerous_goods",
            "scope": "department",
            "schema_version": 1,
            "required": True,
            "artifact_format": "json",
        }
        assert entry["download_url"] == f"/api/v1/tablet/datasets/{publication.id}/download"

        response = client.get(
            entry["download_url"],
            HTTP_ACCEPT="application/octet-stream",
            **_authorization(credential),
        )
        assert response.status_code == 200
        assert (
            response["X-Accel-Redirect"]
            == f"/internal-protected-datasets/{publication.artifact_path}"
        )
        ciphertext = (tmp_path / "artifacts" / publication.artifact_path).read_bytes()
        assert hashlib.sha256(ciphertext).hexdigest() == entry["ciphertext_sha256"]
        Ed25519PrivateKey.from_private_bytes(b"s" * 32).public_key().verify(
            bytes(publication.artifact_signature),
            _signature_payload(
                publication=publication,
                wrapped_cek=bytes(publication.artifact_wrapped_cek),
                nonce=bytes(publication.artifact_nonce),
                ciphertext=ciphertext,
            ),
        )
        grant = entry["key_grant"]
        context = HPKEContext(
            publication_id=publication.id,
            installation_id=installation.id,
            tablet_id=installation.tablet_id,
            department_id=department.id,
            station_id=None,
            dataset_type_code="dangerous_goods",
            version_number=publication.version_number,
            schema_version=1,
            ciphertext_sha256=publication.artifact_sha256,
        )
        assert json.loads(context.info())["scope"] == {
            "dataset_type_code": "dangerous_goods",
            "department_id": str(department.id),
            "station_id": None,
        }
        cek = hpke_open(
            encapsulated_key=base64.b64decode(grant["encapsulated_key"]),
            ciphertext=base64.b64decode(grant["wrapped_content_key"]),
            recipient_private_key=private_key,
            context=context,
        )
        assert AESGCM(cek).decrypt(bytes(publication.artifact_nonce), ciphertext, None) == payload

        other_department = Department.objects.create(
            name="Other", short_code="DGO", created_by=actor
        )
        other_installation, other_credential = _installation(
            department=other_department,
            actor=actor,
            private_key=ec.generate_private_key(ec.SECP256R1()),
        )
        assert request_manifest(installation=other_installation).unavailable
        assert process_next_signed_manifest().status == SignedManifest.Status.READY
        denied = client.get(entry["download_url"], **_authorization(other_credential))
        assert denied.status_code == 404
