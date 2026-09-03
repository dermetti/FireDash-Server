import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.ingestion.services import apply_dangerous_goods_preview, create_dangerous_goods_preview
from apps.organizations.models import Department
from apps.publications.models import DatasetScopeState, DatasetSourceRevision


def payload(*, first_name="Ätzende Flüssigkeit", second_name="Zweite Variante"):
    """A compact source with variants and every supported placard representation."""
    return json.dumps(
        {
            "dataset_type": "dangerous_goods",
            "schema_version": 1,
            "metadata": {
                "publication_profile": "compact",
                "record_count": 2,
                "eri_card_count": 2,
                "adr_edition": "2025",
                "ericards_version": "2025.1",
                "ericards_database_date": "2025-01-01",
                "placard_catalog": {
                    "available_assets": {
                        "3": "ADR_3.svg",
                        "8": "ADR_8.svg",
                        "7A": "ADR_7A.svg",
                        "7B": "ADR_7B.svg",
                        "7C": "ADR_7C.svg",
                    },
                    "special_values": {
                        "7X": {
                            "kind": "variable",
                            "candidate_codes": ["7A", "7B", "7C"],
                        }
                    },
                },
            },
            "goods": [
                {
                    "id": "bam-first",
                    "un_number": "1234",
                    "names": {
                        "official": {"de": first_name, "en": "Corrosive liquid"},
                        "aliases": {"fr": ["Liquide corrosif"]},
                    },
                    "adr": {
                        "hazard_identification_number": "80",
                        "class": "8",
                        "packing_group": "II",
                        "classification_code": "C1",
                        "placards": [
                            "8",
                            {"kind": "conditional", "code": "3"},
                            {
                                "kind": "variable",
                                "candidate_codes": ["7A", "7B", "7C"],
                                "selection_basis": "transport index",
                            },
                        ],
                    },
                    "eri": ["8-01"],
                },
                {
                    "id": "bam-second",
                    "un_number": "1234",
                    "names": {
                        "official": {"fr": second_name, "es": "Líquido especial"},
                        "aliases": {"de": ["Sonder-Alias"]},
                    },
                    "adr": {
                        "class": "3",
                        "placards": [
                            {"kind": "none"},
                            {"kind": "reference", "reference": "ADR 5.2.2.1.12"},
                        ],
                    },
                    "eri": [],
                },
            ],
            "eri_defaults": {"1234": "8-02"},
            "eri_cards": {
                "8-01": [
                    ["title", "Erste Karte"],
                    ["heading", "Maßnahmen"],
                    ["item", "Wasser verwenden"],
                ],
                "8-02": [["title", "Standardkarte"], ["item", "Bereich räumen"]],
            },
            "sources": [
                {
                    "id": "bam",
                    "provider": "BAM",
                    "dataset": "ADR",
                    "source_file": "bam.json",
                    "sha256": "a",
                    "source_url": "https://example.test/bam",
                    "edition": "2025",
                    "legal": {"legal_url": "x", "license": {}, "attribution": {}, "processing": {}},
                },
                {
                    "id": "ericards",
                    "provider": "Cefic",
                    "dataset": "ERI",
                    "source_file": "eri.json",
                    "sha256": "b",
                    "source_url": "https://example.test/eri",
                    "version": "2025.1",
                    "database_date": "2025-01-01",
                    "legal": {
                        "terms_url": "x",
                        "guidance_url": "x",
                        "disclaimer_url": "x",
                        "attribution": {},
                        "reproduction": {},
                    },
                },
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


@pytest.fixture
def goods_ui(db, settings, tmp_path):
    settings.INGESTION_STAGING_ROOT = tmp_path / "staging"
    admin = User.objects.create_user("goods-ui@example.test", "Goods Admin", "safe-password")
    department = Department.objects.create(name="Goods UI", short_code="GUI", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    outsider = User.objects.create_user("other-ui@example.test", "Other Admin", "safe-password")
    other = Department.objects.create(name="Other UI", short_code="OUI", created_by=outsider)
    DepartmentMembership.objects.create(user=outsider, department=other, created_by=outsider)
    return admin, department, outsider, other


def page_url(department):
    return reverse("ingestion-dangerous-goods", args=(department.id,))


def modal_url(department):
    return reverse("ingestion-dangerous-goods-modal", args=(department.id,))


def apply_source(*, actor, department, source):
    preview = create_dangerous_goods_preview(
        actor=actor, department=department, filename="dangerous_goods_v1.json", payload=source
    )
    return apply_dangerous_goods_preview(actor=actor, batch_id=preview.id)


@pytest.mark.django_db
def test_single_resource_page_is_scoped_and_hides_audit_and_publication_details(client, goods_ui):
    admin, department, outsider, _ = goods_ui
    client.force_login(admin)
    empty = client.get(page_url(department))
    assert empty.status_code == 200
    assert "No dangerous-goods source has been imported" in empty.content.decode()
    assert "Inspect Data" not in empty.content.decode()
    assert "SHA-256" not in empty.content.decode()
    assert "Publication has been scheduled" not in empty.content.decode()
    assert "pagination" not in empty.content.decode().lower()

    apply_source(actor=admin, department=department, source=payload())
    current = client.get(page_url(department))
    text = current.content.decode()
    assert "Current curated source" in text
    assert "ADR edition" in text and "2025" in text
    assert "Dangerous-goods records" in text and "Inspect Data" in text
    assert "SHA-256" not in text and "Publication has been scheduled" not in text

    client.force_login(outsider)
    assert client.get(page_url(department)).status_code == 403


@pytest.mark.django_db(transaction=True)
def test_replace_preview_apply_noop_and_stale_are_safe(client, goods_ui):
    admin, department, _, _ = goods_ui
    client.force_login(admin)
    original = payload()
    preview = client.post(
        modal_url(department),
        {"source": SimpleUploadedFile("dangerous_goods_v1.json", original, "application/json")},
        HTTP_HX_REQUEST="true",
    )
    assert preview.status_code == 200
    assert "Review dangerous-goods source" in preview.content.decode()
    assert not DatasetSourceRevision.objects.filter(scope_state__department=department).exists()

    invalid = client.post(
        modal_url(department),
        {"source": SimpleUploadedFile("dangerous_goods_v1.json", b"not json", "application/json")},
        HTTP_HX_REQUEST="true",
    )
    assert invalid.status_code == 400
    assert "Validation requires attention" in invalid.content.decode()
    assert "Traceback" not in invalid.content.decode()

    batch = preview.context["batch"]
    applied = client.post(
        reverse("ingestion-dangerous-goods-apply", args=(department.id, batch.id)),
        HTTP_HX_REQUEST="true",
    )
    assert applied.status_code == 200
    assert "dangerous-goods-modal-close" in applied["HX-Trigger"]
    assert "dangerous-goods-status-refresh" in applied["HX-Trigger"]
    scope = DatasetScopeState.objects.get(
        department=department, dataset_type_code="dangerous_goods"
    )
    retained = DatasetSourceRevision.objects.get(scope_state=scope, source_revision=1)
    assert bytes(retained.plaintext) == original

    no_op = client.post(
        modal_url(department),
        {"source": SimpleUploadedFile("dangerous_goods_v1.json", original, "application/json")},
        HTTP_HX_REQUEST="true",
    )
    no_op_text = no_op.content.decode()
    assert "No update is required" in no_op_text
    assert "Apply</button>" not in no_op_text

    stale = create_dangerous_goods_preview(
        actor=admin,
        department=department,
        filename="stale.json",
        payload=payload(first_name="Stale"),
    )
    apply_source(actor=admin, department=department, source=payload(first_name="Current"))
    rejected = client.post(
        reverse("ingestion-dangerous-goods-apply", args=(department.id, stale.id)),
        HTTP_HX_REQUEST="true",
    )
    assert rejected.status_code == 409
    assert "re-preview" in rejected.content.decode()
    assert DatasetScopeState.objects.get(pk=scope.pk).source_revision == 2


@pytest.mark.django_db
def test_inspection_searches_variants_aliases_and_renders_tablet_fields(client, goods_ui):
    admin, department, _, _ = goods_ui
    apply_source(actor=admin, department=department, source=payload())
    client.force_login(admin)
    inspect_url = reverse("ingestion-dangerous-goods-inspect", args=(department.id,))

    un_response = client.get(inspect_url, {"q": "UN-1234"}, HTTP_HX_REQUEST="true")
    assert un_response.status_code == 200
    assert un_response.content.decode().count("<article") == 2

    for query in ("atzende flussigkeit", "líquido especial", "sonder alias"):
        response = client.get(inspect_url, {"q": query}, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/html")
        assert response.content.decode().count("<article") == 1

    content = client.get(inspect_url, {"q": "1234"}).content.decode()
    assert "Gefahrnummer:" in content and "80" in content
    assert "Verpackungsgruppe:" in content and "II" in content and "C1" in content
    assert "Gefahrzettel:" in content and "bedingt: 3" in content
    assert "variabel: 7A, 7B, 7C (transport index)" in content
    assert "7D" not in content
    assert "keine" in content and "Verweis: ADR 5.2.2.1.12" in content
    assert "Standard: 8-02" in content and "Standardkarte" in content
    assert "Erste Karte" in content and "Maßnahmen" in content and "Wasser verwenden" in content


@pytest.mark.django_db
def test_inspection_is_department_isolated(client, goods_ui):
    admin, department, outsider, other = goods_ui
    apply_source(actor=admin, department=department, source=payload())
    client.force_login(outsider)
    url = reverse("ingestion-dangerous-goods-inspect", args=(department.id,))
    assert client.get(url, {"q": "1234"}).status_code == 403
    client.force_login(admin)
    other_url = reverse("ingestion-dangerous-goods-inspect", args=(other.id,))
    assert client.get(other_url, {"q": "1234"}).status_code == 403
