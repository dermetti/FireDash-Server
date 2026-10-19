# ruff: noqa: F811
import uuid
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.audit.services import record_event
from apps.authorization.models import DepartmentMembership, SystemRole
from apps.authorization.services import permanently_remove_administrator
from apps.file_library import capacity, services
from apps.file_library.models import CapacityReview, DepartmentQuotaPolicy, Library
from apps.file_library.tests.test_capacity import operation, projection, tablet_for
from apps.file_library.tests.test_libraries import (  # noqa: F401
    library_for,
    pdf,
    pdf_pipeline,
    scope,
)
from apps.file_library.tests.test_publication import crypto, publish  # noqa: F401
from apps.organizations.models import Station
from apps.portal.forms import FileLibraryQuotaForm
from apps.portal.overview import attention_for_request, department_attention
from apps.portal.tests.test_data_hub_publication_state import HubCards
from apps.portal.views import _nav_context
from apps.publications.models import DatasetPublication, DatasetScopeState

pytestmark = pytest.mark.django_db


def reauth(client):
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()


def review_for(actor, library, **changes):
    from apps.file_library.publication import scope_kwargs, source_payload
    from apps.file_library.publication_capacity import candidate_operation

    with transaction.atomic():
        snapshot = source_payload(
            department=capacity.department_for(library), station=library.station
        )
    scope_state = DatasetScopeState.objects.get(**scope_kwargs(library))
    values = dict(
        library=library,
        requested_by=actor,
        operation=candidate_operation(scope_state, snapshot),
        publication_snapshot=snapshot,
        projection={"tablets": []},
        fingerprint="a" * 64,
        expires_at=None,
    )
    values.update(changes)
    return CapacityReview.objects.create(**values)


@pytest.mark.parametrize("candidate_status", ["FAILED", "BUILDING"])
def test_file_card_metrics_current_version_order_and_scope(
    client, scope, pdf_pipeline, crypto, candidate_status
):
    admin, _station_admin, other, department, outside, _station, _second = scope
    library = library_for(scope)
    services.create_folder(actor=admin, library_id=library.id, name="Folder")
    services.upload_pdf(actor=admin, library_id=library.id, upload=pdf())
    services.upload_pdf(actor=admin, library_id=library.id, upload=pdf("second.pdf"))
    other_library = services.get_library(other, department_id=outside.id)
    services.upload_pdf(actor=other, library_id=other_library.id, upload=pdf("outside.pdf"))
    library.refresh_from_db()
    state = DatasetScopeState.objects.get(
        department=department, dataset_type_code="department_files"
    )
    current = publish(admin, library)
    state.refresh_from_db()
    candidate = DatasetPublication.objects.create(
        department=department,
        scope_state=state,
        dataset_type_code="department_files",
        version_number=current.version_number + 1,
        schema_version=2,
        source_revision=library.source_revision,
        source_snapshot=current.source_snapshot,
        status=candidate_status,
    )
    state.latest_built_publication = candidate
    state.current_source_fingerprint = "b" * 64
    state.save(update_fields=("latest_built_publication", "current_source_fingerprint"))
    client.force_login(admin)
    response = client.get(reverse("portal-data-hub", args=(department.id,)))
    card = HubCards(response.content.decode()).cards[0]
    module = response.context["modules"][0]
    assert module["id"] == "file_management" and module["count"] == 2
    assert module["usage_bytes"] == library.usage_bytes
    assert module["publication_version"] == current.version_number
    assert f"v{current.version_number}" in card["text"]
    assert f"v{candidate.version_number}" not in card["text"]
    assert "outside.pdf" not in card["text"]
    assert [tag for tag, _attrs in card["elements"] if tag in {"a", "button"}] == ["a"]
    assert card["attrs"]["href"] == reverse("file-library-index")


@pytest.mark.parametrize("role", ["department", "station"])
def test_overview_has_no_file_navigation_tile_and_role_scoped_sidebar(client, scope, role):
    client.force_login(scope[0] if role == "department" else scope[1])
    content = client.get(reverse("dashboard")).content.decode()
    if role == "department":
        assert reverse("file-library-index") not in content
        assert reverse("portal-data-hub", args=(scope[3].id,)) in content
    else:
        assert reverse("file-library-index") in content
        assert reverse("station-data-hub", args=(scope[5].id,)) not in content
    assert '<h2 class="h5">File Management' not in content
    assert "Dateiverwaltung" not in content


def test_shared_station_policy_future_inheritance_isolation_and_no_publication(scope):
    admin, station_admin, other, department, outside, station, second = scope
    department_library = library_for(scope)
    first = services.get_library(station_admin, station_id=station.id)
    existing = Library.objects.create(station=second)
    foreign = services.get_library(other, department_id=outside.id)
    original = foreign.quota_bytes
    revisions = {
        lib.id: lib.source_revision for lib in (department_library, first, existing, foreign)
    }
    from apps.publications.models import PublicationJob

    jobs = PublicationJob.objects.count()
    services.set_department_quotas(
        actor=admin,
        department=department,
        department_files_bytes=1234567,
        station_files_bytes=2345678,
    )
    future = Station.objects.create(department=department, name="Future", short_code="FUTURE")
    new_library = Library.objects.create(station=future)
    assert department_library.quota_bytes == 1234567
    assert {lib.quota_bytes for lib in (first, existing, new_library)} == {2345678}
    assert foreign.quota_bytes == original
    assert (
        dict(Library.objects.filter(pk__in=revisions).values_list("id", "source_revision"))
        == revisions
    )
    assert PublicationJob.objects.count() == jobs
    with pytest.raises(PermissionDenied):
        services.set_department_quotas(actor=other, department=department, station_files_bytes=1)
    with pytest.raises(PermissionDenied):
        services.set_department_quotas(
            actor=station_admin, department=department, station_files_bytes=1
        )


@pytest.mark.parametrize(
    "amount,unit,expected",
    [
        ("0.000001", "MB", 1),
        ("1.23456789", "GB", 1234567890),
        ("0", "GB", 0),
        ("0.0000001", "MB", None),
        ("999999999999", "GB", None),
    ],
)
def test_quota_form_exact_decimal_bytes_and_bounds(amount, unit, expected):
    form = FileLibraryQuotaForm(
        {
            "department_amount": amount,
            "department_unit": unit,
            "station_amount": "1",
            "station_unit": "GB",
        }
    )
    assert form.is_valid() == (expected is not None)
    if expected is not None:
        assert form.cleaned_data["department_files_bytes"] == expected
    else:
        assert "department_amount" in form.errors


def test_quota_settings_two_groups_preserve_arbitrary_bytes(client, scope):
    admin, _station_admin, _other, department, _outside, _station, _second = scope
    services.set_department_quotas(
        actor=admin,
        department=department,
        department_files_bytes=1234567,
        station_files_bytes=9876543,
    )
    client.force_login(admin)
    url = reverse("portal-department-settings", args=(department.id,))
    response = client.get(url)
    form = response.context["quota_form"]
    assert str(form.initial["department_amount"]) == "1.234567"
    assert str(form.initial["station_amount"]) == "9.876543"
    content = response.content.decode()
    assert content.count('name="department_amount"') == 1
    assert content.count('name="station_amount"') == 1
    assert 'name="library_id"' not in content
    assert (
        "Save quotas" in content
        and "This limit applies separately to every station library." in content
    )
    reauth(client)
    response = client.post(url, {"action": "file-library-quota", **form.initial})
    assert response.status_code == 302
    policy = DepartmentQuotaPolicy.objects.get(department=department)
    assert (policy.department_files_bytes, policy.station_files_bytes) == (1234567, 9876543)


def test_temporary_selector_bounded_search_and_normal_form_errors(client, scope):
    admin, _station_admin, _other, department, outside, station, _second = scope
    library = library_for(scope)
    Station.objects.bulk_create(
        [
            Station(department=department, name=f"Station {index:03}", short_code=f"S{index:03}")
            for index in range(110)
        ]
    )
    foreign = Station.objects.create(department=outside, name="Forbidden", short_code="NO")
    inactive = Station.objects.create(
        department=department, name="Inactive", short_code="OFF", active=False
    )
    client.force_login(admin)
    url = reverse("file-library-station-access-choose", args=(library.id,))
    response = client.get(url)
    content = response.content.decode()
    assert content.count('action="' + reverse("file-library-station-access-select") + '"') == 1
    assert content.count('name="station_id"') == 2
    assert 'id="chosen-station-id"' in content and " disabled" in content
    assert len(response.context["station_access_form"].options) == 25
    assert "Forbidden" not in content and "Inactive" not in content
    assert 'placeholder="Search stations"' in content and 'role="combobox"' in content
    found = client.get(url, {"station_q": "S109"})
    assert len(found.context["station_access_form"].options) == 1
    endpoint = reverse("portal-scoped-selector", args=(department.id, "stations"))
    selected = client.get(
        endpoint, {"station_q": "S109", "station_id": str(station.id)}, HTTP_HX_REQUEST="true"
    )
    assert f'value="{station.id}" selected' in selected.content.decode()
    assert "S109" in selected.content.decode()
    post_url = reverse("file-library-station-access-select")
    for bad in (str(foreign.id), str(inactive.id), str(uuid.uuid4()), "not-a-uuid"):
        response = client.post(
            post_url, {"department_id": department.id, "station_id": bad, "confirm": "yes"}
        )
        assert response.status_code == 200
        assert "station_id" in response.context["station_access_form"].errors
    response = client.post(post_url, {"department_id": department.id, "station_id": station.id})
    assert "confirm" in response.context["station_access_form"].errors
    denied_confirmation = client.post(
        post_url, {"department_id": department.id, "station_id": station.id, "confirm": "no"}
    )
    assert "confirm" in denied_confirmation.context["station_access_form"].errors
    assert str(station.id) in response.content.decode()
    assert client.post(post_url, {"department_id": "invalid"}).status_code == 400
    assert (
        client.post(
            post_url, {"department_id": outside.id, "station_id": foreign.id, "confirm": "yes"}
        ).status_code
        == 403
    )
    response = client.post(
        post_url, {"department_id": department.id, "station_id": station.id, "confirm": "yes"}
    )
    assert response.status_code == 302 and response.url.startswith(
        reverse("accounts-reauthenticate")
    )
    assert str(station.id) in client.session["pending_reauth"]["return_url"]
    reauth(client)
    response = client.post(
        post_url, {"department_id": department.id, "station_id": station.id, "confirm": "yes"}
    )
    assert response.status_code == 302
    assert services.active_temporary_access(admin).station_id == station.id


def test_station_selector_requires_department_admin_and_stale_station_is_rejected(client, scope):
    admin, station_admin, _other, department, _outside, station, _second = scope
    client.force_login(station_admin)
    assert (
        client.post(
            reverse("file-library-station-access-select"),
            {"department_id": department.id, "station_id": station.id, "confirm": "yes"},
        ).status_code
        == 403
    )
    assert (
        client.get(reverse("portal-scoped-selector", args=(department.id, "stations"))).status_code
        == 403
    )
    Station.objects.filter(pk=station.pk).update(active=False)
    with pytest.raises(PermissionDenied):
        services.activate_temporary_access(actor=admin, station=station)


def test_department_pending_reviews_counts_pagination_and_station_isolation(client, scope):
    admin, station_admin, other, department, outside, station, second = scope
    library = library_for(scope)
    station_library = services.get_library(station_admin, station_id=station.id)
    second_library = Library.objects.create(station=second)
    foreign_library = services.get_library(other, department_id=outside.id)
    first = review_for(admin, library)
    station_review = review_for(station_admin, station_library)
    review_for(admin, second_library)
    for _ in range(25):
        review_for(admin, library)
    review_for(admin, library, expires_at=timezone.now() - timedelta(seconds=1))
    review_for(other, foreign_library)
    for decision in ("approved", "rejected"):
        review_for(admin, library, decision=decision, decided_by=admin, decided_at=timezone.now())
    review_for(
        admin,
        library,
        decision="approved",
        decided_by=admin,
        decided_at=timezone.now(),
        consumed_at=timezone.now(),
    )
    items = department_attention(department)
    requests = [item for item in items if "file capacity" in item.text]
    assert len(requests) == 1 and requests[0].count == 28
    assert requests[0].url == reverse("file-library-capacity-review", args=(first.id,))
    request = RequestFactory().get(reverse("dashboard"))
    request.user = admin
    context = _nav_context(request)
    assert attention_for_request(request, department=department) is context["nav_attention"]
    assert context["nav_attention_total"] == sum(item.count for item in items)
    client.force_login(admin)
    response = client.get(reverse("file-library", args=(library.id,)))
    assert response.context["pending_review_page"].paginator.count == 28
    assert len(response.context["pending_review_page"]) == 25
    content = response.content.decode()
    assert "Capacity reviews requiring approval" in content
    assert "Publish Now" not in content
    assert reverse("file-library-capacity-review", args=(station_review.id,)) in content
    page2 = client.get(reverse("file-library", args=(library.id,)), {"review_page": 2})
    assert len(page2.context["pending_review_page"]) == 3
    client.force_login(station_admin)
    station_page = client.get(reverse("file-library", args=(station_library.id,)))
    assert station_page.context["pending_review_page"] is None
    assert "Capacity reviews requiring approval" not in station_page.content.decode()
    assert client.get(reverse("file-library-capacity-review", args=(first.id,))).status_code == 403


def test_capacity_summary_lowest_bytes_distinct_percentage_unknown_and_scope(client, scope):
    library = library_for(scope)
    small, _ = tablet_for(scope, free=300, total=500)
    large, _ = tablet_for(scope, free=1000, total=10000)
    unknown, installation = tablet_for(scope)
    installation.storage_observed_at = timezone.now() - timedelta(days=2)
    installation.save(update_fields=("storage_observed_at",))
    unrelated, _ = tablet_for(
        scope,
        station=Station.objects.create(department=scope[4], name="Outside", short_code="OUT"),
        free=1,
        total=100,
    )
    result = projection(library, operation())
    original_fingerprint = capacity.fingerprint(result)
    summary = capacity.presentation_summary(result)
    assert summary["lowest_free"]["tablet_id"] == str(small.id)
    assert summary["lowest_free"]["projected_free_bytes"] == 184
    assert summary["lowest_free"]["threshold_breached"] is False
    assert summary["worst_percentage"]["tablet_id"] == str(large.id)
    assert summary["worst_percentage"]["threshold_breached"] is True
    assert summary["different_worst_percentage"] is True and summary["unknown_count"] == 1
    assert str(unrelated.id) not in {row["tablet_id"] for row in summary["tablets"]}
    assert capacity.fingerprint(result) == original_fingerprint
    review = review_for(scope[0], library, projection=result, fingerprint=original_fingerprint)
    client.force_login(scope[0])
    content = client.get(
        reverse("file-library-capacity-review", args=(review.id,))
    ).content.decode()
    assert "Lowest projected remaining free storage" in content and small.display_name in content
    assert "Lowest remaining percentage" in content and large.display_name in content
    assert "Capacity unknown" in content and unknown.display_name in content
    assert "36.80%" in content and "8.84%" in content
    assert len(summary["tablets"]) == 3


@pytest.mark.parametrize("authority", ["department", "station"])
def test_permanent_removal_audit_safe_context_and_boundaries(client, scope, authority):
    admin, station_admin, other, department, outside, station, _second = scope
    if authority == "department":
        target = User.objects.create_user("removed@example.test", "Removed identity", "password")
        DepartmentMembership.objects.create(user=target, department=department, created_by=admin)
    else:
        target = station_admin
        target.display_name = "Distinct removed identity"
        target.save(update_fields=("display_name",))
    email, name = target.email, target.display_name
    permanently_remove_administrator(actor=admin, user=target, department=department)
    event = AuditEvent.objects.get(action="authorization.administrator_permanently_removed")
    assert set(event.metadata) == {
        "department_authority_count",
        "station_authority_count",
        "station_id",
    }
    assert email not in str(event.metadata) and name not in str(event.metadata)
    assert event.metadata["department_authority_count"] == (1 if authority == "department" else 0)
    assert event.metadata["station_authority_count"] == (1 if authority == "station" else 0)
    client.force_login(admin)
    url = reverse("portal-department-audit-detail", args=(department.id, event.id))
    content = client.get(url).content.decode()
    for text in (
        "Administrator permanently removed",
        admin.display_name,
        str(target.id),
        department.name,
        "Authorization was revoked and the user account anonymized.",
    ):
        assert text in content
    assert email not in content and name not in content and "Safe event context" not in content
    if authority == "station":
        assert str(station.id) in content
    client.force_login(other)
    assert client.get(url).status_code == 403
    assert (
        client.get(
            reverse("portal-department-audit-detail", args=(outside.id, event.id))
        ).status_code
        == 404
    )
    SystemRole.objects.create(user=other, created_by=admin)
    system_detail = client.get(
        reverse("portal-system-audit-detail", args=(event.id,))
    ).content.decode()
    assert "Administrator permanently removed" in system_detail
    for route, args in (("portal-system-audit", ()), ("portal-department-audit", (department.id,))):
        client.force_login(other if not args else admin)
        listed = client.get(reverse(route, args=args)).content.decode()
        assert "Administrator permanently removed" in listed
        assert "\u00e2\u20ac\u201d" not in listed and email not in listed


def test_historical_audit_context_allowlist_never_renders_raw_metadata(client, scope):
    admin, _station_admin, _other, department, _outside, _station, _second = scope
    event = record_event(
        action="authorization.administrator_permanently_removed",
        actor_user=admin,
        department=department,
        target_type="user",
        target_uuid=uuid.uuid4(),
        metadata={
            "email": "secret@example.test",
            "token": "sensitive-token",
            "station_id": "not-an-id",
            "department_authority_count": "<script>secret</script>",
        },
    )
    client.force_login(admin)
    response = client.get(reverse("portal-department-audit-detail", args=(department.id, event.id)))
    content = response.content.decode()
    assert "Authorization was revoked" in content
    assert "secret@example.test" not in content and "sensitive-token" not in content
    assert "not-an-id" not in content and "&lt;script&gt;secret" not in content
    event.refresh_from_db()
    assert event.metadata["token"] == "sensitive-token"


@pytest.mark.django_db(transaction=True)
def test_quota_backfill_preserves_department_uses_minimum_and_reports(scope, capsys):
    admin, _station_admin, _other, department, _outside, station, second = scope
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    old_target = [("file_library", "0007_storageimpact_capacityreview")]
    try:
        executor.migrate(old_target)
        old_apps = executor.loader.project_state(old_target).apps
        OldLibrary = old_apps.get_model("file_library", "Library")
        OldLibrary.objects.create(department_id=department.id, quota_bytes=1234567)
        OldLibrary.objects.create(station_id=station.id, quota_bytes=9876543)
        OldLibrary.objects.create(station_id=second.id, quota_bytes=2345678)
        MigrationExecutor(connection).migrate(latest)
        policy = DepartmentQuotaPolicy.objects.get(department=department)
        assert (policy.department_files_bytes, policy.station_files_bytes) == (1234567, 2345678)
        assert set(
            Library.objects.filter(station__department=department).values_list(
                "source_revision", flat=True
            )
        ) == {0}
        output = capsys.readouterr().out
        assert str(department.id) in output and "2345678" in output and "9876543" in output
    finally:
        MigrationExecutor(connection).migrate(latest)
