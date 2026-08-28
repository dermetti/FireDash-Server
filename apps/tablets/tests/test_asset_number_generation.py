"""Department-local, concurrent-safe Tablet asset-number allocation tests."""

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, close_old_connections
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership
from apps.authorization.services import set_department_tablet_asset_number_policy
from apps.organizations.models import Department
from apps.tablets import services as tablet_services
from apps.tablets.models import Tablet
from apps.tablets.services import TabletError, create_tablet, retire_tablet


@pytest.fixture
def asset_number_scope(db):
    admin = User.objects.create_user("asset-admin@example.test", "Asset Admin", "password")
    other_admin = User.objects.create_user("asset-other@example.test", "Other Admin", "password")
    department = Department.objects.create(name="Asset Numbers", short_code="AST", created_by=admin)
    other_department = Department.objects.create(
        name="Other Asset Numbers", short_code="OTH", created_by=other_admin
    )
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    DepartmentMembership.objects.create(
        user=other_admin, department=other_department, created_by=other_admin
    )
    return admin, other_admin, department, other_department


def _set_policy(admin, department, *, auto_enabled=True, prefix="", width=1):
    return set_department_tablet_asset_number_policy(
        actor=admin,
        department=department,
        auto_enabled=auto_enabled,
        prefix=prefix,
        width=width,
    )


@pytest.mark.django_db
def test_asset_number_policy_defaults_are_manual_and_sequence_starts_at_zero(asset_number_scope):
    _, _, department, _ = asset_number_scope
    assert department.tablet_asset_number_auto_enabled is False
    assert department.tablet_asset_number_prefix == ""
    assert department.tablet_asset_number_width == 1
    assert department.tablet_asset_number_sequence == 0


@pytest.mark.django_db
def test_manual_asset_number_works_while_automatic_generation_is_disabled(asset_number_scope):
    admin, _, department, _ = asset_number_scope
    tablet = create_tablet(
        actor=admin, department=department, display_name="Manual", asset_number="MANUAL-100"
    )
    department.refresh_from_db()
    assert tablet.asset_number == "MANUAL-100"
    assert department.tablet_asset_number_sequence == 0


@pytest.mark.django_db
def test_policy_is_scoped_validated_and_audited(asset_number_scope):
    admin, other_admin, department, _ = asset_number_scope
    changed = _set_policy(admin, department, prefix=" TABFHH ", width=4)
    changed.refresh_from_db()
    assert changed.tablet_asset_number_prefix == "TABFHH"
    assert changed.tablet_asset_number_width == 4
    assert changed.tablet_asset_number_auto_enabled is True
    assert AuditEvent.objects.filter(
        action="authorization.department_tablet_asset_number_policy_changed",
        department=department,
    ).exists()
    with pytest.raises(PermissionDenied):
        _set_policy(other_admin, department, prefix="X", width=4)
    with pytest.raises(ValueError, match="between 1 and 20"):
        _set_policy(admin, department, width=0)
    with pytest.raises(ValueError, match="between 1 and 20"):
        _set_policy(admin, department, width=21)
    with pytest.raises(ValueError, match="must fit"):
        _set_policy(admin, department, prefix="X" * 128, width=1)


@pytest.mark.django_db
def test_generated_asset_number_formats_prefix_width_and_monotonic_sequence(asset_number_scope):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, prefix="TABFHH", width=4)
    first = create_tablet(
        actor=admin, department=department, display_name="First", generate_asset_number=True
    )
    department.refresh_from_db()
    assert first.asset_number == "TABFHH0001"
    assert department.tablet_asset_number_sequence == 1

    _set_policy(admin, department, prefix="TABFHH", width=6)
    second = create_tablet(
        actor=admin, department=department, display_name="Second", generate_asset_number=True
    )
    department.refresh_from_db()
    assert second.asset_number == "TABFHH000002"
    assert department.tablet_asset_number_sequence == 2


@pytest.mark.django_db
def test_policy_changes_never_reset_or_change_the_numeric_sequence(asset_number_scope):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, prefix="TAB", width=4)
    create_tablet(
        actor=admin, department=department, display_name="First", generate_asset_number=True
    )
    _set_policy(admin, department, auto_enabled=False, prefix="NEW", width=6)
    department.refresh_from_db()
    assert department.tablet_asset_number_sequence == 1
    _set_policy(admin, department, auto_enabled=True, prefix="NEW", width=6)
    assert (
        create_tablet(
            actor=admin, department=department, display_name="Second", generate_asset_number=True
        ).asset_number
        == "NEW000002"
    )


@pytest.mark.django_db
def test_width_is_minimum_not_a_sequence_ceiling(asset_number_scope):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, width=4)
    department.tablet_asset_number_sequence = 9999
    department.save(update_fields=("tablet_asset_number_sequence",))
    tablet = create_tablet(
        actor=admin, department=department, display_name="Five digits", generate_asset_number=True
    )
    assert tablet.asset_number == "10000"


@pytest.mark.django_db
def test_width_twenty_is_accepted_and_pads_the_numeric_part(asset_number_scope):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, width=20)
    tablet = create_tablet(
        actor=admin, department=department, display_name="Twenty digits", generate_asset_number=True
    )
    assert tablet.asset_number == "00000000000000000001"


@pytest.mark.django_db
def test_automatic_policy_ignores_manual_value_and_uses_department_allocator(asset_number_scope):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, prefix="TAB", width=4)
    tablet = create_tablet(
        actor=admin,
        department=department,
        display_name="Generated",
        asset_number="MANUAL-OVERRIDE",
    )
    department.refresh_from_db()
    assert tablet.asset_number == "TAB0001"
    assert department.tablet_asset_number_sequence == 1


@pytest.mark.django_db
def test_generated_identifiers_skip_manual_collisions_monotonically(asset_number_scope):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, width=4)
    # Existing/manual rows may pre-date the automatic policy.  Create the
    # collision directly rather than passing a manual number to an automatic
    # registration, which intentionally ignores that request value.
    Tablet.objects.create(
        department=department, display_name="Manual 42", asset_number="0042", created_by=admin
    )
    Tablet.objects.create(
        department=department, display_name="Manual 43", asset_number="0043", created_by=admin
    )
    department.tablet_asset_number_sequence = 41
    department.save(update_fields=("tablet_asset_number_sequence",))

    generated = create_tablet(
        actor=admin, department=department, display_name="Generated", generate_asset_number=True
    )
    department.refresh_from_db()
    assert generated.asset_number == "0044"
    assert department.tablet_asset_number_sequence == 44


@pytest.mark.django_db
def test_prefix_change_collision_is_skipped_without_resetting_sequence(asset_number_scope):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, prefix="OLD", width=4)
    create_tablet(
        actor=admin, department=department, display_name="Old", generate_asset_number=True
    )
    _set_policy(admin, department, prefix="NEW", width=4)
    create_tablet(actor=admin, department=department, display_name="Manual", asset_number="NEW0002")

    generated = create_tablet(
        actor=admin, department=department, display_name="New", generate_asset_number=True
    )
    department.refresh_from_db()
    assert generated.asset_number == "NEW0003"
    assert department.tablet_asset_number_sequence == 3


@pytest.mark.django_db
def test_allocation_exhaustion_rolls_back_the_counter(asset_number_scope):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, width=1)
    Tablet.objects.create(
        department=department, display_name="Manual 1", asset_number="1", created_by=admin
    )
    Tablet.objects.create(
        department=department, display_name="Manual 2", asset_number="2", created_by=admin
    )
    with patch.object(tablet_services, "MAX_ASSET_NUMBER_ALLOCATION_ATTEMPTS", 2):
        with pytest.raises(TabletError, match="after 2 attempts"):
            create_tablet(
                actor=admin,
                department=department,
                display_name="Cannot allocate",
                generate_asset_number=True,
            )
    department.refresh_from_db()
    assert department.tablet_asset_number_sequence == 0
    assert not Tablet.objects.filter(department=department, display_name="Cannot allocate").exists()


@pytest.mark.django_db
def test_future_growth_that_no_longer_fits_asset_number_fails_without_advancing(asset_number_scope):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, prefix="X" * 127, width=1)
    department.tablet_asset_number_sequence = 9
    department.save(update_fields=("tablet_asset_number_sequence",))
    with pytest.raises(TabletError, match="does not fit"):
        create_tablet(
            actor=admin,
            department=department,
            display_name="Too long",
            generate_asset_number=True,
        )
    department.refresh_from_db()
    assert department.tablet_asset_number_sequence == 9


@pytest.mark.django_db
def test_generation_is_department_local_and_disabled_policy_uses_manual_path(asset_number_scope):
    admin, other_admin, department, other_department = asset_number_scope
    _set_policy(admin, department, prefix="A", width=2)
    _set_policy(other_admin, other_department, prefix="B", width=2)
    assert (
        create_tablet(
            actor=admin, department=department, display_name="A", generate_asset_number=True
        ).asset_number
        == "A01"
    )
    assert (
        create_tablet(
            actor=other_admin,
            department=other_department,
            display_name="B",
            generate_asset_number=True,
        ).asset_number
        == "B01"
    )
    _set_policy(admin, department, auto_enabled=False, width=2)
    assert (
        create_tablet(
            actor=admin,
            department=department,
            display_name="Manual after disabling",
            asset_number="MANUAL-1",
            generate_asset_number=True,
        ).asset_number
        == "MANUAL-1"
    )


@pytest.mark.django_db
def test_failed_generated_registration_rolls_back_counter(asset_number_scope):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, width=4)
    with patch("apps.tablets.services.Tablet.objects.create", side_effect=IntegrityError):
        with pytest.raises(TabletError):
            create_tablet(
                actor=admin,
                department=department,
                display_name="Will fail",
                generate_asset_number=True,
            )
    department.refresh_from_db()
    assert department.tablet_asset_number_sequence == 0


@pytest.mark.django_db
def test_integrity_error_collision_retries_inside_savepoint_without_breaking_transaction(
    asset_number_scope,
):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, width=4)
    create_tablet(actor=admin, department=department, display_name="Manual", asset_number="0001")
    original_filter = Tablet.objects.filter
    hide_first_precheck = True

    def filter_with_stale_first_precheck(*args, **kwargs):
        nonlocal hide_first_precheck
        queryset = original_filter(*args, **kwargs)
        if kwargs.get("asset_number") == "0001" and hide_first_precheck:
            # Simulate another committed writer claiming the candidate after the
            # initial availability check but before this transaction's insert.
            hide_first_precheck = False
            return queryset.none()
        return queryset

    with patch(
        "apps.tablets.services.Tablet.objects.filter", side_effect=filter_with_stale_first_precheck
    ):
        generated = create_tablet(
            actor=admin,
            department=department,
            display_name="Generated after collision",
            generate_asset_number=True,
        )

    department.refresh_from_db()
    assert generated.asset_number == "0002"
    assert department.tablet_asset_number_sequence == 2
    assert Tablet.objects.filter(department=department, asset_number="0001").count() == 1
    assert Tablet.objects.filter(department=department, asset_number="0002").count() == 1


@pytest.mark.django_db
def test_retired_tablets_do_not_reuse_an_allocated_asset_number(asset_number_scope):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, width=4)
    first = create_tablet(
        actor=admin, department=department, display_name="First", generate_asset_number=True
    )
    retire_tablet(actor=admin, tablet=first)
    second = create_tablet(
        actor=admin, department=department, display_name="Second", generate_asset_number=True
    )
    assert first.asset_number == "0001"
    assert second.asset_number == "0002"


@pytest.mark.django_db
def test_registration_ui_uses_department_policy_without_consuming_preview(
    client, asset_number_scope
):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, width=4)
    client.force_login(admin)
    url = reverse("tablet-create", args=(department.id,))
    response = client.get(url)
    assert response.status_code == 200
    content = response.content.decode()
    assert "Generate automatically" not in content
    assert 'value="0001"' in content
    assert "readonly" in content
    department.refresh_from_db()
    assert department.tablet_asset_number_sequence == 0
    response = client.post(
        url,
        {"display_name": "Created through UI", "asset_number": "MANUAL-OVERRIDE"},
    )
    assert response.status_code == 302
    assert (
        Tablet.objects.get(department=department, display_name="Created through UI").asset_number
        == "0001"
    )


@pytest.mark.django_db
def test_registration_ui_manual_field_when_automatic_numbering_is_disabled(
    client, asset_number_scope
):
    admin, _, department, _ = asset_number_scope
    client.force_login(admin)
    url = reverse("tablet-create", args=(department.id,))
    response = client.get(url)
    content = response.content.decode()
    assert "Generate automatically" not in content
    assert "readonly" not in content
    response = client.post(url, {"display_name": "Manual UI", "asset_number": "MANUAL-10"})
    assert response.status_code == 302
    assert (
        Tablet.objects.get(department=department, display_name="Manual UI").asset_number
        == "MANUAL-10"
    )


@pytest.mark.django_db
def test_preview_changes_with_policy_but_does_not_reserve_the_candidate(client, asset_number_scope):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, prefix="TAB", width=4)
    client.force_login(admin)
    url = reverse("tablet-create", args=(department.id,))
    assert 'value="TAB0001"' in client.get(url).content.decode()
    _set_policy(admin, department, prefix="NEW", width=6)
    assert 'value="NEW000001"' in client.get(url).content.decode()
    department.refresh_from_db()
    assert department.tablet_asset_number_sequence == 0


@pytest.mark.django_db
def test_post_after_preview_allocates_current_candidate_not_the_stale_preview(
    client, asset_number_scope
):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, width=4)
    client.force_login(admin)
    url = reverse("tablet-create", args=(department.id,))
    assert 'value="0001"' in client.get(url).content.decode()
    create_tablet(
        actor=admin, department=department, display_name="Concurrent", asset_number="ignored"
    )
    response = client.post(url, {"display_name": "After preview", "asset_number": "0001"})
    assert response.status_code == 302
    assert (
        Tablet.objects.get(department=department, display_name="After preview").asset_number
        == "0002"
    )


@pytest.mark.django_db
def test_department_settings_ui_uses_a_scoped_audited_policy_form(client, asset_number_scope):
    admin, other_admin, department, _ = asset_number_scope
    client.force_login(admin)
    session = client.session
    session["recent_reauthentication_at"] = timezone.now().timestamp()
    session.save()
    url = reverse("portal-department-settings", args=(department.id,))
    response = client.post(
        url,
        {"action": "asset-numbering", "auto_enabled": "on", "prefix": "TAB", "width": "6"},
    )
    assert response.status_code == 302
    department.refresh_from_db()
    assert (department.tablet_asset_number_auto_enabled, department.tablet_asset_number_prefix) == (
        True,
        "TAB",
    )
    assert department.tablet_asset_number_width == 6
    client.force_login(other_admin)
    assert (
        client.post(
            url,
            {"action": "asset-numbering", "auto_enabled": "on", "prefix": "BAD", "width": "4"},
        ).status_code
        == 403
    )


def _concurrent_registration(*, user_id, department_id, display_name, barrier):
    close_old_connections()
    try:
        user = User.objects.get(pk=user_id)
        department = Department.objects.get(pk=department_id)
        barrier.wait(timeout=10)
        tablet = create_tablet(
            actor=user,
            department=department,
            display_name=display_name,
            generate_asset_number=True,
        )
        return tablet.asset_number
    finally:
        close_old_connections()


@pytest.mark.django_db(transaction=True)
def test_concurrent_automatic_registrations_are_serialized_by_department_row(asset_number_scope):
    admin, _, department, _ = asset_number_scope
    _set_policy(admin, department, width=4)
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _concurrent_registration,
                user_id=admin.id,
                department_id=department.id,
                display_name=name,
                barrier=barrier,
            )
            for name in ("Concurrent one", "Concurrent two")
        ]
        results = [future.result(timeout=15) for future in futures]
    department.refresh_from_db()
    assert set(results) == {"0001", "0002"}
    assert department.tablet_asset_number_sequence == 2
