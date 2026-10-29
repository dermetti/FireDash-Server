import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership, SystemRole
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
    SystemRole.objects.create(user=admin)
    outsider = User.objects.create_user("other-ui@example.test", "Other Admin", "safe-password")
    other = Department.objects.create(name="Other UI", short_code="OUI", created_by=outsider)
    DepartmentMembership.objects.create(user=outsider, department=other, created_by=outsider)
    return admin, department, outsider, other


def page_url(department):
    return reverse("ingestion-dangerous-goods", args=())


def modal_url(department):
    return reverse("ingestion-dangerous-goods-modal", args=())


def apply_source(*, actor, department, source):
    preview = create_dangerous_goods_preview(
        actor=actor, filename="dangerous_goods_v1.json", payload=source
    )
    return apply_dangerous_goods_preview(actor=actor, batch_id=preview.id)


@pytest.mark.django_db
def test_single_resource_page_is_scoped_and_hides_audit_and_publication_details(client, goods_ui):
    admin, department, outsider, _ = goods_ui
    client.force_login(admin)
    empty = client.get(page_url(department))
    assert empty.status_code == 200
    assert empty.context["source"] is None

    apply_source(actor=admin, department=department, source=payload())
    current = client.get(page_url(department))
    assert current.context["source"] is not None

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
    assert not DatasetSourceRevision.objects.filter(scope_state__department=department).exists()

    invalid = client.post(
        modal_url(department),
        {"source": SimpleUploadedFile("dangerous_goods_v1.json", b"not json", "application/json")},
        HTTP_HX_REQUEST="true",
    )
    assert invalid.status_code == 400

    batch = preview.context["batch"]
    applied = client.post(
        reverse("ingestion-dangerous-goods-apply", args=(batch.id,)),
        HTTP_HX_REQUEST="true",
    )
    assert applied.status_code == 200
    assert "dangerous-goods-modal-close" in applied["HX-Trigger"]
    assert "dangerous-goods-status-refresh" in applied["HX-Trigger"]
    scope = DatasetScopeState.objects.get(department=None, dataset_type_code="dangerous_goods")
    retained = DatasetSourceRevision.objects.get(scope_state=scope, source_revision=1)
    assert bytes(retained.plaintext) == original

    no_op = client.post(
        modal_url(department),
        {"source": SimpleUploadedFile("dangerous_goods_v1.json", original, "application/json")},
        HTTP_HX_REQUEST="true",
    )
    assert no_op.context["is_noop"] is True

    stale = create_dangerous_goods_preview(
        actor=admin,
        filename="stale.json",
        payload=payload(first_name="Stale"),
    )
    apply_source(actor=admin, department=department, source=payload(first_name="Current"))
    rejected = client.post(
        reverse("ingestion-dangerous-goods-apply", args=(stale.id,)),
        HTTP_HX_REQUEST="true",
    )
    assert rejected.status_code == 409
    assert DatasetScopeState.objects.get(pk=scope.pk).source_revision == 2


@pytest.mark.django_db
def test_inspection_searches_variants_aliases_and_renders_tablet_fields(client, goods_ui):
    admin, department, _, _ = goods_ui
    apply_source(actor=admin, department=department, source=payload())
    client.force_login(admin)
    inspect_url = reverse("ingestion-dangerous-goods-inspect", args=())

    un_response = client.get(inspect_url, {"q": "UN-1234"}, HTTP_HX_REQUEST="true")
    assert un_response.status_code == 200
    assert len(un_response.context["results"]) == 2

    for query in ("atzende flussigkeit", "líquido especial", "sonder alias"):
        response = client.get(inspect_url, {"q": query}, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        assert len(response.context["results"]) == 1
        assert response["Content-Type"].startswith("text/html")


@pytest.mark.django_db
def test_inspection_is_department_isolated(client, goods_ui):
    admin, department, outsider, other = goods_ui
    apply_source(actor=admin, department=department, source=payload())
    client.force_login(outsider)
    url = reverse("ingestion-dangerous-goods-inspect", args=())
    assert client.get(url, {"q": "1234"}).status_code == 403
    client.force_login(admin)
    assert client.get(url, {"q": "1234"}).status_code == 200


@pytest.mark.django_db(transaction=True)
def test_system_publications_and_scope_detail_authorization(client, goods_ui):
    admin, department, outsider, _ = goods_ui
    client.force_login(admin)
    response = client.get(reverse("system-publications-list"))
    assert response.status_code == 200
    apply_source(actor=admin, department=department, source=payload())
    scope = DatasetScopeState.objects.get(dataset_type_code="dangerous_goods", scope_type="SYSTEM")
    detail = reverse("publications-scope-detail", args=(scope.id,))
    assert client.get(detail).status_code == 200
    client.force_login(outsider)
    assert client.get(reverse("system-publications-list")).status_code == 403
    assert client.get(detail).status_code == 403
    for route in (
        "publications-scope-rebuild",
        "publications-scope-stage-update",
        "publications-scope-build-now",
    ):
        assert client.post(reverse(route, args=(scope.id,))).status_code == 403
    assert client.get(reverse("ingestion-dangerous-goods-modal")).status_code == 403


@pytest.mark.django_db
def test_goods_routes_and_department_admin_denied(client, goods_ui):
    import uuid

    from django.urls import Resolver404, resolve

    admin, _department, outsider, _ = goods_ui
    batch_id = uuid.uuid4()
    assert reverse("ingestion-dangerous-goods") == "/system/data/dangerous-goods/"
    for suffix in ("", "edit/", "inspect/", f"imports/{batch_id}/apply/"):
        with pytest.raises(Resolver404):
            resolve(f"/system/publications/dangerous-goods/{suffix}")
    client.force_login(admin)
    response = client.get(reverse("ingestion-dangerous-goods"))
    assert response.status_code == 200
    client.force_login(outsider)
    assert client.get(reverse("portal-system-data-hub")).status_code == 403
    for route in (
        "ingestion-dangerous-goods",
        "ingestion-dangerous-goods-modal",
        "ingestion-dangerous-goods-inspect",
    ):
        assert client.get(reverse(route)).status_code == 403
    assert client.post(reverse("ingestion-dangerous-goods-modal")).status_code == 403
    response = client.post(reverse("ingestion-dangerous-goods-apply", args=(batch_id,)))
    assert response.status_code == 403
