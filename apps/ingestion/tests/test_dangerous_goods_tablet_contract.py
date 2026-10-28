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
def test_system_dangerous_goods_is_not_exposed_to_tablets_in_phase_one(settings, tmp_path):
    from apps.publications.registry import get_dataset_definition

    definition = get_dataset_definition("dangerous_goods")
    assert definition.scope == "system"
    assert definition.required is True
    assert definition.system_exposure is False
    from types import SimpleNamespace

    from apps.publications.manifests import _publication_is_exposed_to_installation

    publication = SimpleNamespace(scope_type="SYSTEM", dataset_type_code="dangerous_goods")
    assert not _publication_is_exposed_to_installation(publication=publication, installation=None)
