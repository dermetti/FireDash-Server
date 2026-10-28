import hashlib
import hmac
import json
import uuid
from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.ingestion.dangerous_goods import validate_dangerous_goods
from apps.organizations.models import Department, Station, Vehicle
from apps.publications.hpke import (
    HPKE_CIPHERSUITE,
    serialize_p256_public_key,
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
def test_system_goods_end_to_end_across_departments(settings, tmp_path, monkeypatch):
    import base64

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from django.test import Client

    from apps.authorization.models import ApiVersionCompatibilityPolicy, SystemRole
    from apps.ingestion.services import (
        apply_dangerous_goods_preview,
        create_dangerous_goods_preview,
    )
    from apps.publications.artifacts import _signature_payload
    from apps.publications.hpke import HPKEContext, HPKEError, hpke_open
    from apps.publications.manifests import (
        ManifestError,
        canonical_manifest_payload,
        request_dataset_key_grant,
    )
    from apps.publications.models import (
        DatasetKeyGrant,
        DatasetPublication,
        DatasetSourceRevision,
        DepartmentFeature,
        PublicationJob,
    )
    from apps.publications.services import process_next_job
    from apps.publications.worker_grants import (
        process_next_dataset_key_grant,
        process_next_signed_manifest,
    )

    settings.INGESTION_STAGING_ROOT = tmp_path / "staging"
    settings.PUBLICATION_ARTIFACT_ROOT = tmp_path / "artifacts"
    settings.PUBLICATION_ARTIFACT_TEMP_ROOT = tmp_path / "temp"
    settings.PUBLICATION_KEK_CREDENTIAL_PATH = tmp_path / "kek"
    settings.PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH = tmp_path / "signing"
    settings.PUBLICATION_KEK_CREDENTIAL_PATH.write_bytes(b"k" * 32)
    settings.PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH.write_bytes(b"s" * 32)
    monkeypatch.setattr("apps.publications.artifacts.grp", None)
    actor = User.objects.create_user("contract@example.test", "Contract", "password")
    SystemRole.objects.create(user=actor)
    payload = compact_payload()
    preview = create_dangerous_goods_preview(actor=actor, filename="goods.json", payload=payload)
    apply_dangerous_goods_preview(actor=actor, batch_id=preview.id)
    source = DatasetSourceRevision.objects.get(scope_state__dataset_type_code="dangerous_goods")
    assert bytes(source.plaintext) == payload
    assert source.sha256 == hashlib.sha256(payload).hexdigest()
    job = PublicationJob.objects.get(dataset_type_code="dangerous_goods")
    job.not_before = timezone.now()
    job.save(update_fields=("not_before",))
    job = process_next_job()
    assert job.status == PublicationJob.Status.SUCCEEDED, job.error_message
    publication = DatasetPublication.objects.get(dataset_type_code="dangerous_goods")
    ciphertext = (settings.PUBLICATION_ARTIFACT_ROOT / publication.artifact_path).read_bytes()
    public_key = Ed25519PrivateKey.from_private_bytes(b"s" * 32).public_key()
    public_key.verify(
        bytes(publication.artifact_signature),
        _signature_payload(
            publication=publication,
            wrapped_cek=bytes(publication.artifact_wrapped_cek),
            nonce=bytes(publication.artifact_nonce),
            ciphertext=ciphertext,
        ),
    )
    clients = []
    for code in ("ONE", "TWO"):
        department = Department.objects.create(name=code, short_code=code, created_by=actor)
        private = ec.generate_private_key(ec.SECP256R1())
        installation, credential = _installation(
            department=department, actor=actor, private_key=private
        )
        client = Client()
        assert (
            client.get("/api/v1/tablet/manifest", **_authorization(credential)).status_code == 202
        )
        grant = process_next_dataset_key_grant()
        assert grant.status == "READY", grant.error_message
        manifest = process_next_signed_manifest()
        assert manifest.status == "READY", manifest.error_message
        response = client.get("/api/v1/tablet/manifest", **_authorization(credential))
        assert response.status_code == 200
        body = response.json()
        public_key.verify(
            base64.b64decode(body["signature"]),
            canonical_manifest_payload({k: v for k, v in body.items() if k != "signature"}),
        )
        entries = [entry for entry in body["datasets"] if entry["type"] == "dangerous_goods"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["scope"] == {"type": "SYSTEM"}
        assert entry["publication_id"] == str(publication.id)
        assert entry["schema_version"] == 1 and entry["required"] is True
        assert entry["ciphertext_sha256"] == hashlib.sha256(ciphertext).hexdigest()
        context = HPKEContext(
            publication_id=publication.id,
            installation_id=installation.id,
            tablet_id=installation.tablet_id,
            department_id=None,
            station_id=None,
            dataset_type_code="dangerous_goods",
            version_number=publication.version_number,
            schema_version=1,
            ciphertext_sha256=entry["ciphertext_sha256"],
        )
        assert json.loads(context.info())["scope"] == {
            "dataset_type_code": "dangerous_goods",
            "department_id": None,
            "station_id": None,
        }
        grant_wire = entry["key_grant"]
        cek = hpke_open(
            encapsulated_key=base64.b64decode(grant_wire["encapsulated_key"]),
            ciphertext=base64.b64decode(grant_wire["wrapped_content_key"]),
            recipient_private_key=private,
            context=context,
        )
        response = client.get(entry["download_url"], **_authorization(credential))
        assert response.status_code == 200
        assert (
            response["X-Accel-Redirect"]
            == f"/internal-protected-datasets/{publication.artifact_path}"
        )
        assert (
            AESGCM(cek).decrypt(
                base64.b64decode(entry["content_encryption_nonce"]), ciphertext, None
            )
            == payload
        )
        clients.append((private, context, grant_wire, installation, credential, client))
    assert clients[0][2] != clients[1][2]
    with pytest.raises(HPKEError):
        hpke_open(
            encapsulated_key=base64.b64decode(clients[0][2]["encapsulated_key"]),
            ciphertext=base64.b64decode(clients[0][2]["wrapped_content_key"]),
            recipient_private_key=clients[1][0],
            context=clients[1][1],
        )

    _, _, _, installation, credential, client = clients[0]
    url = f"/api/v1/tablet/datasets/{publication.id}/download"
    feature = DepartmentFeature.objects.create(
        department=installation.tablet.department, feature_code="publications", enabled=False
    )
    with pytest.raises(ManifestError):
        request_dataset_key_grant(publication=publication, installation=installation)
    assert client.get(url, **_authorization(credential)).status_code in (403, 404)
    grant = DatasetKeyGrant.objects.get(publication=publication, app_installation=installation)
    grant.status = "PENDING"
    grant.save(update_fields=("status",))
    denied = process_next_dataset_key_grant()
    assert denied.status == "REVOKED"
    feature.delete()

    assignment = TabletVehicleAssignment.objects.get(tablet=installation.tablet)
    assignment.ended_at = timezone.now()
    assignment.save(update_fields=("ended_at",))
    with pytest.raises(ManifestError):
        request_dataset_key_grant(publication=publication, installation=installation)
    assert client.get(url, **_authorization(credential)).status_code in (403, 404)
    assignment.ended_at = None
    assignment.save(update_fields=("ended_at",))

    installation.authorization_valid_until = timezone.now() - timedelta(seconds=1)
    installation.save(update_fields=("authorization_valid_until",))
    with pytest.raises(ManifestError):
        request_dataset_key_grant(publication=publication, installation=installation)
    assert client.get(url, **_authorization(credential)).status_code in (401, 403)
    installation.authorization_valid_until = timezone.now() + timedelta(days=1)
    installation.save(update_fields=("authorization_valid_until",))
    installation.status = AppInstallation.Status.REVOKED
    installation.save(update_fields=("status",))
    with pytest.raises(ManifestError):
        request_dataset_key_grant(publication=publication, installation=installation)
    assert client.get(url, **_authorization(credential)).status_code in (401, 403)
    installation.status = AppInstallation.Status.ACTIVE
    installation.save(update_fields=("status",))
    installation.tablet.status = Tablet.Status.INACTIVE
    installation.tablet.save(update_fields=("status",))
    with pytest.raises(ManifestError):
        request_dataset_key_grant(publication=publication, installation=installation)
    assert client.get(url, **_authorization(credential)).status_code in (403, 404)
    installation.tablet.status = Tablet.Status.ACTIVE
    installation.tablet.save(update_fields=("status",))
    ApiVersionCompatibilityPolicy.objects.create(
        api_major=1, minimum_app_version="2.0.0", updated_by=actor
    )
    assert client.get("/api/v1/tablet/manifest", **_authorization(credential)).status_code == 426
    assert client.get(url, **_authorization(credential)).status_code == 426
