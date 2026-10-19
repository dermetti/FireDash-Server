"""Data Hub publication-state presentation regressions."""

import uuid
from datetime import timedelta
from html.parser import HTMLParser

import pytest
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.authorization.models import (
    DepartmentMembership,
    StationAdminAssignment,
    SystemRole,
    VehicleRescueGuidesConfiguration,
)
from apps.organizations.models import Department, Station
from apps.outbound_mail.models import SystemMailConfiguration
from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.paths import publication_artifact_relative_path
from apps.reference_data.models import PhonebookEntry


@pytest.fixture
def data_hub_scope(client, db):
    admin = User.objects.create_user("data-hub@example.test", "Data Hub", "safe-password")
    department = Department.objects.create(name="Data Hub", short_code="HUB", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    client.force_login(admin)
    return admin, department


def _scope(department):
    return DatasetScopeState.objects.create(
        department=department,
        dataset_type_code="department_hydrants",
        source_revision=17,
    )


def _publication(*, department, scope, version, status):
    publication_id = uuid.uuid4()
    values = {
        "id": publication_id,
        "department": department,
        "dataset_type_code": scope.dataset_type_code,
        "scope_state": scope,
        "version_number": version,
        "schema_version": 1,
        "source_revision": version,
        "status": status,
    }
    if status == DatasetPublication.Status.PUBLISHED:
        values.update(
            {
                "artifact_ready": True,
                "artifact_status": DatasetPublication.ArtifactStatus.READY,
                "artifact_path": publication_artifact_relative_path(
                    department_id=department.id, publication_id=publication_id
                ),
                "artifact_size": 1,
                "artifact_sha256": "a" * 64,
                "artifact_nonce": b"n" * 12,
                "artifact_wrapped_cek": b"w" * 40,
                "artifact_encryption_algorithm": "AES-256-GCM",
                "artifact_wrapping_algorithm": "AES-KW-RFC3394",
                "artifact_kek_version": "test",
                "artifact_signature": b"s" * 64,
                "artifact_signature_algorithm": "Ed25519",
                "artifact_signing_key_version": "test",
            }
        )
    return DatasetPublication.objects.create(**values)


def _activate(*, department, scope, version=17):
    publication = _publication(
        department=department,
        scope=scope,
        version=version,
        status=DatasetPublication.Status.PUBLISHED,
    )
    scope.latest_built_publication = publication
    scope.current_published_publication = publication
    publication.source_fingerprint = "a" * 64
    publication.save(update_fields=("source_fingerprint",))
    scope.current_source_fingerprint = publication.source_fingerprint
    scope.save(
        update_fields=(
            "latest_built_publication",
            "current_published_publication",
            "current_source_fingerprint",
        )
    )
    return publication


def _page(client, department):
    return client.get(reverse("portal-data-hub", args=(department.id,))).content.decode()


@pytest.mark.django_db
def test_data_hub_shows_authoritative_current_publication(data_hub_scope, client):
    _admin, department = data_hub_scope
    _activate(department=department, scope=_scope(department))

    content = _page(client, department)

    assert "v17 &middot; Current" in content


@pytest.mark.django_db
def test_data_hub_shows_scheduled_update_without_promoting_candidate(data_hub_scope, client):
    admin, department = data_hub_scope
    scope = _scope(department)
    _activate(department=department, scope=scope)
    candidate = _publication(
        department=department,
        scope=scope,
        version=18,
        status=DatasetPublication.Status.BUILDING,
    )
    PublicationJob.objects.create(
        department=department,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        source_revision=18,
        status=PublicationJob.Status.PENDING,
        trigger_type=PublicationJob.TriggerType.DATA_CHANGE,
        requested_by=admin,
        not_before=timezone.now() + timedelta(minutes=1),
        build_publication=candidate,
    )

    content = _page(client, department)

    assert "v17 &middot; Update scheduled" in content
    assert "v18 &middot;" not in content


@pytest.mark.django_db
def test_data_hub_shows_building_update_without_promoting_candidate(data_hub_scope, client):
    admin, department = data_hub_scope
    scope = _scope(department)
    _activate(department=department, scope=scope)
    candidate = _publication(
        department=department,
        scope=scope,
        version=18,
        status=DatasetPublication.Status.BUILDING,
    )
    PublicationJob.objects.create(
        department=department,
        dataset_type_code=scope.dataset_type_code,
        scope_state=scope,
        source_revision=18,
        status=PublicationJob.Status.RUNNING,
        trigger_type=PublicationJob.TriggerType.DATA_CHANGE,
        requested_by=admin,
        build_publication=candidate,
    )

    content = _page(client, department)

    assert "v17 &middot; Building update" in content
    assert "v18 &middot;" not in content


@pytest.mark.django_db
def test_data_hub_shows_failed_update_without_replacing_current(data_hub_scope, client):
    _admin, department = data_hub_scope
    scope = _scope(department)
    _activate(department=department, scope=scope)
    failed = _publication(
        department=department,
        scope=scope,
        version=18,
        status=DatasetPublication.Status.FAILED,
    )
    scope.latest_built_publication = failed
    # A failed update is only current card state while canonical source still
    # differs from the active publication.
    scope.current_source_fingerprint = "b" * 64
    scope.save(update_fields=("latest_built_publication", "current_source_fingerprint"))

    content = _page(client, department)

    assert "v17 &middot; Update failed" in content
    assert "v18 &middot;" not in content


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status",
    [
        DatasetPublication.Status.FAILED,
        DatasetPublication.Status.BUILDING,
        DatasetPublication.Status.PUBLISHED,
    ],
)
def test_data_hub_shows_not_published_without_an_active_publication(data_hub_scope, client, status):
    _admin, department = data_hub_scope
    scope = _scope(department)
    candidate = _publication(
        department=department,
        scope=scope,
        version=18,
        status=status,
    )
    scope.latest_built_publication = candidate
    scope.save(update_fields=("latest_built_publication",))

    content = _page(client, department)

    assert "Not published" in content
    assert "v18 &middot;" not in content


@pytest.mark.django_db
def test_data_hub_publication_state_is_department_scoped(data_hub_scope, client):
    _admin, department = data_hub_scope
    _activate(department=department, scope=_scope(department), version=17)
    outsider = User.objects.create_user("other-hub@example.test", "Other Hub", "safe-password")
    other_department = Department.objects.create(
        name="Other Data Hub", short_code="OHD", created_by=outsider
    )
    other_scope = _scope(other_department)
    _publication(
        department=other_department,
        scope=other_scope,
        version=99,
        status=DatasetPublication.Status.FAILED,
    )

    content = _page(client, department)

    assert "v17 &middot; Current" in content
    assert "v99 &middot;" not in content
    assert "Update failed" not in content


class HubCards(HTMLParser):
    """Inspect complete card subtrees, including any accidental nested controls."""

    def __init__(self, content):
        super().__init__()
        self.cards = []
        self.current = None
        self.depth = 0
        self.feed(content)
        assert self.current is None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.current is None and "card" in attrs.get("class", "").split():
            self.current = {"tag": tag, "attrs": attrs, "elements": [], "html": "", "text": ""}
            self.cards.append(self.current)
        if self.current is not None:
            self.current["elements"].append((tag, attrs))
            self.current["html"] += self.get_starttag_text()
            if tag not in {"br", "hr", "img", "input", "meta", "link", "wbr"}:
                self.depth += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.current is not None:
            self.depth -= 1

    def handle_endtag(self, tag):
        if self.current is not None:
            self.current["html"] += f"</{tag}>"
            self.depth -= 1
            if self.depth == 0:
                self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current["html"] += data
            self.current["text"] += data


def _assert_navigation_card(card, *, title, icon, url, system=False):
    assert card["tag"] == "a"
    assert card["attrs"]["href"] == url
    assert [tag for tag, _ in card["elements"] if tag in {"a", "button"}] == ["a"]
    assert '<h2 class="h5 mb-0">' + title + "</h2>" in card["html"]
    # Inspect the actual vendored SVG, including the distinct Phonebook/Personnel icons.
    svg = render_to_string("components/_data_hub_icon.html", {"icon": icon}).strip()
    assert svg in card["html"]
    assert any(
        tag == "svg" and attrs.get("aria-hidden") == "true" for tag, attrs in card["elements"]
    )
    action = "Configure" if system else "Open"
    assert f'<span class="visually-hidden">{action} {title}</span>' in card["html"]


@pytest.mark.django_db
def test_data_hub_cards_preserve_navigation_and_distinct_icons(data_hub_scope, client):
    _admin, department = data_hub_scope
    response = client.get(reverse("portal-data-hub", args=(department.id,)))
    assert response.status_code == 200
    expected = [
        (
            "dangerous_goods",
            "Dangerous Goods",
            "warning",
            "Source revision",
            "Curated ADR dangerous-goods and ERI card reference data.",
            "ingestion-dangerous-goods",
        ),
        (
            "department_phonebook",
            "Phonebook",
            "phone",
            "Entries",
            "Canonical department and station contact entries.",
            "reference-data-phonebook",
        ),
        (
            "department_hydrants",
            "Hydrants",
            "water",
            "Active records",
            "Water supply reference points distributed to operational tablets.",
            "reference-data-hydrants",
        ),
        (
            "station_personnel",
            "Personnel",
            "people",
            "Active records",
            "Active personnel reference records and station context.",
            "personnel-list",
        ),
        (
            "department_fire_plans",
            "Fire Plans",
            "building",
            "Active plans",
            "Current operational fire-plan PDFs and their canonical metadata.",
            "reference-data-fire-plans",
        ),
        (
            "department_klgv_plans",
            "KLGV Plans",
            "garden",
            "Active plans",
            "Allotment garden plans.",
            "reference-data-klgv-plans",
        ),
    ]
    modules = response.context["modules"]
    assert [module["id"] for module in modules] == [
        "file_management",
        *[row[0] for row in expected],
    ]
    assert all(
        not {"name", "description", "icon", "count_label"} & module.keys() for module in modules
    )
    content = response.content.decode()
    cards = HubCards(content).cards
    assert len(cards) == 8  # File Management, six datasets, inherited configuration.
    assert cards[0]["attrs"]["href"] == reverse("file-library-index")
    assert cards[0]["attrs"]["data-module-id"] == "file_management"
    _assert_navigation_card(
        cards[0], title="File Management", icon="files", url=reverse("file-library-index")
    )
    assert "Manage department files and tablet publications." in cards[0]["text"]
    assert "Files" in cards[0]["text"] and "Total size" in cards[0]["text"]
    assert [tag for tag, _ in cards[0]["elements"] if tag in {"a", "button"}] == ["a"]
    for card, (module_id, title, icon, count_label, description, route) in zip(
        cards[1:7], expected, strict=True
    ):
        assert card["attrs"]["data-module-id"] == module_id
        _assert_navigation_card(
            card, title=title, icon=icon, url=reverse(route, args=(department.id,))
        )
        assert description in card["text"]
        assert count_label in card["text"]
        assert "Publication" in card["text"] and "Not published" in card["text"]
    assert "Open module" not in content
    assert "Import and review" not in content
    assert "row-cols-1 row-cols-md-2 g-3" in content
    assert 'aria-label="Distributed data modules"' in content

    inherited = cards[-1]
    assert inherited["tag"] == "article"
    assert inherited["attrs"]["data-module-id"] == "system_vehicle_rescue_guides"
    assert '<h3 class="h5 mb-0">Vehicle Rescue Guides</h3>' in inherited["html"]
    assert (
        "Euro RESCUE web application available through system-managed configuration."
        in inherited["text"]
    )
    assert (
        render_to_string("components/_data_hub_icon.html", {"icon": "vehicle"}).strip()
        in inherited["html"]
    )
    assert "System managed" in inherited["text"] and "Enabled" in inherited["text"]
    assert "Publication" not in inherited["text"]
    links = [attrs for tag, attrs in inherited["elements"] if tag == "a"]
    assert links == [
        {"href": "https://rescue.euroncap.com/", "rel": "noopener noreferrer", "target": "_blank"}
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "mode,provider,delivery,status,css",
    [
        ("DISABLED", "Not active", "Disabled", "Disabled", "secondary"),
        ("API", "Brevo", "Email API", "Api", "success"),
        ("SMTP", "System SMTP", "SMTP", "Smtp", "success"),
    ],
)
def test_system_hub_preserves_cards_and_dynamic_configuration(
    data_hub_scope, client, mode, provider, delivery, status, css
):
    admin, department = data_hub_scope
    SystemRole.objects.create(user=admin)
    SystemMailConfiguration.objects.update_or_create(
        singleton=True, defaults={"delivery_mode": mode, "api_provider": "BREVO"}
    )
    VehicleRescueGuidesConfiguration.objects.update_or_create(
        singleton=True, defaults={"web_url": "https://example.test/rescue?locale=de"}
    )
    response = client.get(reverse("portal-system-data-hub"))
    assert response.status_code == 200
    assert [module["id"] for module in response.context["modules"]] == [
        "system_vehicle_rescue_guides",
        "system_outbound_email",
    ]
    assert all(
        not {"name", "description", "icon", "count_label"} & module.keys()
        for module in response.context["modules"]
    )
    rescue, mail = HubCards(response.content.decode()).cards
    for card, module_id, title, icon, route, description in [
        (
            rescue,
            "system_vehicle_rescue_guides",
            "Vehicle Rescue Guides",
            "vehicle",
            "portal-system-vehicle-rescue-guides",
            "System-managed Euro RESCUE web application configuration.",
        ),
        (
            mail,
            "system_outbound_email",
            "Outbound Email",
            "mail",
            "portal-system-outbound-email",
            "System-level transactional email delivery configuration.",
        ),
    ]:
        assert card["attrs"]["data-module-id"] == module_id
        _assert_navigation_card(card, title=title, icon=icon, url=reverse(route), system=True)
        assert description in card["text"]
        assert "Publication" not in card["text"]
    assert "Enabled" in rescue["text"] and "Euro RESCUE" in rescue["text"]
    assert "Web" in rescue["text"]
    assert "https://example.test/rescue?locale=de" in rescue["text"]
    assert f'<span class="badge text-bg-{css}">{status}</span>' in mail["html"]
    assert provider in mail["text"] and delivery in mail["text"]
    assert f'<dd class="col-5 text-end">{provider}</dd>' in mail["html"]
    assert f'<dd class="col-5 text-end">{delivery}</dd>' in mail["html"]
    assert 'aria-label="System data modules"' in response.content.decode()
    # Departments inherit the same effective URL, preserving safe external navigation.
    department_page = client.get(reverse("portal-data-hub", args=(department.id,)))
    inherited = HubCards(department_page.content.decode()).cards[-1]
    assert 'href="https://example.test/rescue?locale=de"' in inherited["html"]
    assert 'target="_blank"' in inherited["html"]
    assert 'rel="noopener noreferrer"' in inherited["html"]


@pytest.mark.django_db
def test_data_hub_counts_and_access_remain_department_scoped(data_hub_scope, client):
    admin, department = data_hub_scope
    outsider = User.objects.create_user("outside@example.test", "Outside", "safe-password")
    other = Department.objects.create(name="Other", short_code="OTH", created_by=outsider)
    DepartmentMembership.objects.create(user=outsider, department=other, created_by=outsider)
    PhonebookEntry.objects.create(
        department=department, organization_unit="Local", phone_number="1"
    )
    for index in range(2):
        PhonebookEntry.objects.create(
            department=other, organization_unit="Other", phone_number=str(index)
        )
    response = client.get(reverse("portal-data-hub", args=(department.id,)))
    phonebook = next(m for m in response.context["modules"] if m["id"] == "department_phonebook")
    assert phonebook["count"] == 1
    card = next(
        c
        for c in HubCards(response.content.decode()).cards
        if c["attrs"].get("data-module-id") == "department_phonebook"
    )
    assert '<dd class="col-5 text-end">1</dd>' in card["html"]
    assert client.get(reverse("portal-data-hub", args=(other.id,))).status_code == 403
    assert client.get(reverse("portal-system-data-hub")).status_code == 403
    station = Station.objects.create(department=department, name="Station", short_code="ST")
    StationAdminAssignment.objects.create(user=outsider, station=station, created_by=admin)
    client.force_login(outsider)
    assert client.get(reverse("portal-data-hub", args=(department.id,))).status_code == 403
    assert client.get(reverse("portal-system-data-hub")).status_code == 403
    client.logout()
    assert client.get(reverse("portal-data-hub", args=(department.id,))).status_code == 302
    assert client.get(reverse("portal-system-data-hub")).status_code == 302
