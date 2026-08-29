from pathlib import Path

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.reference_data.models import PhonebookEntry
from apps.reference_data.phonebook import find_duplicate_candidates, normalize_phone_number
from apps.reference_data.services import (
    create_phonebook_entry,
    resolve_phonebook_duplicate,
    update_phonebook_entry,
)


@pytest.fixture
def phonebook_scope(db):
    actor = User.objects.create_user("phonebook@example.test", "Phonebook", "password")
    department = Department.objects.create(name="One", short_code="ONE", created_by=actor)
    DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
    station = Station.objects.create(department=department, name="Station One", short_code="S1")
    other = Department.objects.create(name="Two", short_code="TWO", created_by=actor)
    return actor, department, station, other


def make_entry(actor, department, **values):
    values = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "phone_number": "040 428512300",
        **values,
    }
    return create_phonebook_entry(actor=actor, department=department, **values)


@pytest.mark.django_db
def test_canonical_validation_uuid_normalization_and_station_ownership(phonebook_scope):
    actor, department, station, other = phonebook_scope
    entry = make_entry(actor, department, station=station)
    assert entry.id and entry.phone_number == "040 42851 2300"
    assert normalize_phone_number("040 42851 2300") == "040 42851 2300"
    assert normalize_phone_number(" 040 42851 2300 ") == "040 42851 2300"
    assert entry.display_name == "Ada Lovelace" and entry.scope_label == "Station One"
    invalid = PhonebookEntry(department=department, first_name="Only", phone_number="1")
    with pytest.raises(ValidationError):
        invalid.full_clean()
    foreign_station = Station.objects.create(department=other, name="Other", short_code="O")
    with pytest.raises(PermissionDenied):
        make_entry(actor, department, station=foreign_station)


@pytest.mark.django_db
def test_duplicate_threshold_ranking_and_department_isolation(phonebook_scope):
    actor, department, station, other = phonebook_scope
    exact_a = make_entry(actor, department, organization_unit="Ops", function="Chief")
    exact_b = make_entry(actor, department, organization_unit="Ops", function="Chief")
    make_entry(
        actor,
        department,
        first_name="Different",
        last_name="Person",
        organization_unit="Ops",
        function="Other",
    )
    make_entry(
        actor, department, first_name="Phone", last_name="Only", phone_number=exact_a.phone_number
    )
    PhonebookEntry.objects.create(
        department=other,
        first_name="Ada",
        last_name="Lovelace",
        organization_unit="Ops",
        function="Chief",
        phone_number="040 42851 2300",
    )
    candidates = find_duplicate_candidates(department=department)
    assert candidates[0].reasons == (
        "Name",
        "Organization unit",
        "Function",
        "Phone number",
        "Scope",
    )
    assert {
        c.first_id if hasattr(c, "first_id") else c.first.id for c in candidates
    }  # deterministic non-empty scan
    assert all(
        "Phone" not in (candidate.first.display_name, candidate.second.display_name)
        for candidate in candidates
    )
    assert all(candidate.first.department_id == department.id for candidate in candidates)
    assert {exact_a.id, exact_b.id} == {candidates[0].first.id, candidates[0].second.id}


@pytest.mark.django_db
def test_empty_fields_do_not_add_duplicate_signals(phonebook_scope):
    actor, department, _, _ = phonebook_scope
    make_entry(actor, department, first_name="One", last_name="Person", phone_number="111")
    make_entry(actor, department, first_name="Two", last_name="Person", phone_number="222")
    assert not find_duplicate_candidates(department=department)


@pytest.mark.django_db
def test_duplicate_resolutions_keep_both_skip_and_stale_safety(phonebook_scope):
    actor, department, _, _ = phonebook_scope
    first = make_entry(actor, department, organization_unit="Ops", function="Chief")
    second = make_entry(actor, department, organization_unit="Ops", function="Chief")
    candidate = find_duplicate_candidates(department=department)[0]
    resolve_phonebook_duplicate(
        actor=actor,
        department=department,
        first_id=candidate.first.id,
        second_id=candidate.second.id,
        first_fingerprint=candidate.first_fingerprint,
        second_fingerprint=candidate.second_fingerprint,
        action="keep_both",
    )
    assert not find_duplicate_candidates(department=department)
    update_phonebook_entry(actor=actor, entry=first, function="Deputy")
    assert find_duplicate_candidates(department=department)  # changed entries may be reviewed again
    stale = find_duplicate_candidates(department=department)[0]
    update_phonebook_entry(actor=actor, entry=second, last_name="Byron")
    with pytest.raises(ValueError):
        resolve_phonebook_duplicate(
            actor=actor,
            department=department,
            first_id=stale.first.id,
            second_id=stale.second.id,
            first_fingerprint=stale.first_fingerprint,
            second_fingerprint=stale.second_fingerprint,
            action="keep_first",
        )
    current = find_duplicate_candidates(department=department)[0]
    resolve_phonebook_duplicate(
        actor=actor,
        department=department,
        first_id=current.first.id,
        second_id=current.second.id,
        first_fingerprint=current.first_fingerprint,
        second_fingerprint=current.second_fingerprint,
        action="keep_first",
    )
    assert PhonebookEntry.objects.filter(pk=current.first.id).exists()
    assert not PhonebookEntry.objects.filter(pk=current.second.id).exists()
    third = make_entry(actor, department, organization_unit="Dispatch", function="Lead")
    fourth = make_entry(actor, department, organization_unit="Dispatch", function="Lead")
    candidate = next(
        item
        for item in find_duplicate_candidates(department=department)
        if {item.first.id, item.second.id} == {third.id, fourth.id}
    )
    resolve_phonebook_duplicate(
        actor=actor,
        department=department,
        first_id=candidate.first.id,
        second_id=candidate.second.id,
        first_fingerprint=candidate.first_fingerprint,
        second_fingerprint=candidate.second_fingerprint,
        action="keep_second",
    )
    assert not PhonebookEntry.objects.filter(pk=candidate.first.id).exists()
    assert PhonebookEntry.objects.filter(pk=candidate.second.id).exists()


@pytest.mark.django_db
def test_phonebook_list_and_crud_presentation(client, phonebook_scope):
    actor, department, station, _ = phonebook_scope
    client.force_login(actor)
    entry = make_entry(actor, department, station=station, organization_unit="Control")
    list_url = reverse("reference-data-phonebook", args=[department.id])
    response = client.get(list_url)
    assert response.status_code == 200
    for label in (
        "Name",
        "Function",
        "Phone number",
        "Scope",
        "Ada Lovelace",
        "Station One",
    ):
        assert label.encode() in response.content
    detail_url = reverse("reference-data-phonebook-detail", args=[entry.id])
    assert client.get(detail_url).status_code == 200
    edit_url = reverse("reference-data-phonebook-edit", args=[entry.id])
    response = client.post(
        edit_url,
        {
            "station": station.id,
            "first_name": "Ada",
            "last_name": "Lovelace",
            "organization_unit": "Control",
            "function": "Watch officer",
            "phone_number": "040 428512300",
        },
    )
    assert response.status_code == 302
    entry.refresh_from_db()
    assert entry.phone_number == "040 42851 2300"
    assert (
        client.post(reverse("reference-data-phonebook-delete", args=[entry.id])).status_code == 302
    )
    assert not PhonebookEntry.objects.filter(pk=entry.id).exists()


@pytest.mark.django_db
def test_phonebook_manual_station_binding_and_list_filters(client, phonebook_scope):
    actor, department, first_station, other_department = phonebook_scope
    second_station = Station.objects.create(
        department=department, name="Second station", short_code="S2"
    )
    foreign_station = Station.objects.create(
        department=other_department, name="Foreign station", short_code="FS"
    )
    client.force_login(actor)
    create_url = reverse("reference-data-phonebook-create", args=[department.id])
    values = {
        "station": str(first_station.id),
        "first_name": "Ada",
        "last_name": "Lovelace",
        "organization_unit": "Control",
        "function": "Duty officer",
        "phone_number": "040 428512300",
    }
    created = client.post(create_url, values)
    assert created.status_code == 302
    assert created.url == reverse("reference-data-phonebook", args=[department.id])
    entry = PhonebookEntry.objects.get(department=department, first_name="Ada")
    assert entry.station_id == first_station.id
    edit_url = reverse("reference-data-phonebook-edit", args=[entry.id])
    values["station"] = str(second_station.id)
    assert client.post(edit_url, values).status_code == 302
    entry.refresh_from_db()
    assert entry.station_id == second_station.id
    values["station"] = ""
    assert client.post(edit_url, values).status_code == 302
    entry.refresh_from_db()
    assert entry.station_id is None
    values["station"] = str(foreign_station.id)
    assert client.post(create_url, values).status_code == 200
    assert (
        PhonebookEntry.objects.filter(department=department, station=foreign_station).count() == 0
    )
    station_entry = make_entry(
        actor,
        department,
        station=first_station,
        first_name="Grace",
        last_name="Hopper",
        organization_unit="Operations",
        function="Dispatch",
        phone_number="555 100",
    )
    PhonebookEntry.objects.create(
        department=other_department,
        first_name="Foreign",
        last_name="Entry",
        phone_number="555 100",
    )
    list_url = reverse("reference-data-phonebook", args=[department.id])
    response = client.get(list_url, {"q": "Grace", "scope": "station", "station": first_station.id})
    assert response.status_code == 200 and list(response.context["entries"]) == [station_entry]
    live_response = client.get(
        list_url,
        {"q": "Grace", "scope": "station", "station": first_station.id},
        HTTP_HX_REQUEST="true",
    )
    assert live_response.status_code == 200
    assert "phonebook-results" in live_response.content.decode()
    assert "Grace Hopper" in live_response.content.decode()
    assert foreign_station.name not in response.content.decode()
    assert first_station.name in response.content.decode()
    assert client.get(list_url, {"scope": "department"}).context["total_count"] == 1
    body = client.get(list_url).content.decode()
    for label in (
        "Status",
        "Actions",
        "Existing Phonebook entries",
        "Add entry",
        "Import CSV",
        "Review duplicates",
        "Name",
        "Function",
        "Phone number",
        "Scope",
    ):
        assert label in body
    assert "Download CSV template" not in body
    assert "hx-trigger" in body
    assert "input changed delay:1s" in body


@pytest.mark.django_db
def test_phonebook_modal_create_uses_fragment_success_and_non_htmx_fallback(
    client, phonebook_scope
):
    actor, department, station, _ = phonebook_scope
    client.force_login(actor)
    create_url = reverse("reference-data-phonebook-create", args=[department.id])
    values = {
        "station": station.id,
        "first_name": "Grace",
        "last_name": "Hopper",
        "organization_unit": "Operations",
        "function": "Dispatch",
        "phone_number": "555 100",
    }
    response = client.post(create_url, values, HTTP_HX_REQUEST="true")
    body = response.content.decode()
    assert response.status_code == 200
    assert "HX-Redirect" not in response
    assert response["HX-Trigger"] == '{"phonebook-modal-close": {}}'
    assert "phonebook-feedback" in body
    assert 'id="phonebook-status"' in body
    assert 'id="phonebook-results"' in body
    assert body.count('hx-swap-oob="true"') == 3
    assert PhonebookEntry.objects.filter(department=department, first_name="Grace").exists()

    fallback = client.post(
        create_url,
        values
        | {
            "first_name": "Katherine",
            "last_name": "Johnson",
            "organization_unit": "Analytics",
            "function": "Lead",
            "phone_number": "555 101",
        },
    )
    assert fallback.status_code == 302
    assert fallback.url == reverse("reference-data-phonebook", args=[department.id])


@pytest.mark.django_db
def test_phonebook_invalid_htmx_create_stays_in_bound_modal(client, phonebook_scope):
    actor, department, _, _ = phonebook_scope
    client.force_login(actor)
    response = client.post(
        reverse("reference-data-phonebook-create", args=[department.id]),
        {"first_name": "Only", "last_name": "", "organization_unit": "", "phone_number": ""},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert "modal" in response.content.decode()
    assert "This field is required" in response.content.decode()
    assert "Only" in response.content.decode()


@pytest.mark.django_db
def test_manual_phonebook_duplicate_create_transitions_to_dedicated_review_and_updates(
    client, phonebook_scope
):
    actor, department, station, _ = phonebook_scope
    client.force_login(actor)
    existing = make_entry(
        actor, department, station=station, organization_unit="Operations", function="Chief"
    )
    original_id = existing.id
    create_url = reverse("reference-data-phonebook-create", args=[department.id])
    review_url = reverse("reference-data-phonebook-create-review", args=[department.id])
    values = {
        "station": station.id,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "organization_unit": "Operations",
        "function": "Deputy",
        "phone_number": "040 428512300",
    }
    response = client.post(create_url, values, HTTP_HX_REQUEST="true")
    assert response.status_code == 204 and response["HX-Redirect"] == review_url
    assert PhonebookEntry.objects.filter(department=department).count() == 1
    review = client.get(review_url)
    candidate = review.context["candidate"]
    assert review.status_code == 200 and review.context["candidate_index"] == 0
    assert "Proposed entry" in review.content.decode()
    response = client.post(
        review_url,
        {
            "action": "update",
            "candidate_id": str(candidate.second.id),
            "candidate_fingerprint": candidate.second_fingerprint,
        },
    )
    assert response.status_code == 302
    existing.refresh_from_db()
    assert existing.id == original_id and existing.function == "Deputy"


@pytest.mark.django_db
def test_manual_phonebook_review_next_create_cancel_and_stale_safety(client, phonebook_scope):
    actor, department, station, other_department = phonebook_scope
    client.force_login(actor)
    strongest = make_entry(
        actor, department, station=station, organization_unit="Operations", function="Chief"
    )
    weaker = make_entry(
        actor, department, station=station, organization_unit="Dispatch", function=""
    )
    create_url = reverse("reference-data-phonebook-create", args=[department.id])
    review_url = reverse("reference-data-phonebook-create-review", args=[department.id])
    values = {
        "station": station.id,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "organization_unit": "Operations",
        "function": "Chief",
        "phone_number": "040 428512300",
    }
    client.post(create_url, values)
    first = client.get(review_url).context["candidate"]
    assert first.second.id == strongest.id
    response = client.post(
        review_url,
        {
            "action": "next",
            "candidate_id": str(first.second.id),
            "candidate_fingerprint": first.second_fingerprint,
        },
    )
    assert response.status_code == 302
    second = client.get(review_url).context["candidate"]
    assert second.second.id == weaker.id
    response = client.post(
        review_url,
        {
            "action": "create",
            "candidate_id": str(second.second.id),
            "candidate_fingerprint": second.second_fingerprint,
        },
    )
    assert response.status_code == 302
    assert PhonebookEntry.objects.filter(department=department).count() == 3

    client.post(create_url, values)
    stale = client.get(review_url).context["candidate"]
    update_phonebook_entry(actor=actor, entry=stale.second, function="Changed")
    response = client.post(
        review_url,
        {
            "action": "create",
            "candidate_id": str(stale.second.id),
            "candidate_fingerprint": stale.second_fingerprint,
        },
    )
    assert response.status_code == 409
    foreign = PhonebookEntry.objects.create(
        department=other_department,
        first_name="Ada",
        last_name="Lovelace",
        phone_number="040 428512300",
    )
    response = client.post(
        review_url,
        {"action": "update", "candidate_id": str(foreign.id), "candidate_fingerprint": "forged"},
    )
    assert response.status_code == 409
    assert PhonebookEntry.objects.filter(department=other_department).count() == 1

    client.post(create_url, values)
    candidate = client.get(review_url).context["candidate"]
    response = client.post(
        review_url,
        {
            "action": "cancel",
            "candidate_id": str(candidate.second.id),
            "candidate_fingerprint": candidate.second_fingerprint,
        },
    )
    assert response.status_code == 302
    assert PhonebookEntry.objects.filter(department=department).count() == 3


@pytest.mark.django_db
def test_phonebook_pagination_preserves_filters_and_resets_for_live_filtering(
    client, phonebook_scope
):
    actor, department, station, _ = phonebook_scope
    client.force_login(actor)
    PhonebookEntry.objects.bulk_create(
        [
            PhonebookEntry(
                department=department,
                station=station,
                organization_unit="Dispatch",
                phone_number=f"555 {number:03}",
            )
            for number in range(101)
        ]
    )
    list_url = reverse("reference-data-phonebook", args=[department.id])
    response = client.get(
        list_url,
        {"q": "Dispatch", "scope": "station", "station": station.id, "page": 2},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert response.context["page"].number == 2
    assert response.context["page_query"] == (f"q=Dispatch&scope=station&station={station.id}")
    assert 'hx-target="#phonebook-results"' in response.content.decode()
    reset_response = client.get(
        list_url,
        {"q": "Dispatch", "scope": "station", "station": station.id},
        HTTP_HX_REQUEST="true",
    )
    assert reset_response.context["page"].number == 1
    ids = [entry.id for entry in reset_response.context["entries"]]
    assert ids == sorted(ids, key=str)


@pytest.mark.parametrize(
    "template",
    (
        "templates/personnel/list.html",
        "templates/reference_data/hydrants.html",
        "templates/reference_data/fire_plans.html",
        "templates/reference_data/klgv_plans.html",
        "templates/reference_data/phonebook.html",
        "templates/portal/stations.html",
        "templates/tablets/list.html",
    ),
)
def test_data_hub_text_searches_use_the_personnel_live_search_trigger(template):
    content = (Path.cwd() / template).read_text(encoding="utf-8")
    assert "input changed delay:1s" in content
