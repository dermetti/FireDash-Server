from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import time

import pytest
from django.db import IntegrityError, connection, connections
from django.test import Client
from django.urls import reverse

from apps.accounts import services as account_services
from apps.accounts.models import AccountSetupToken, User
from apps.audit.models import AuditEvent
from apps.authorization.models import DepartmentMembership, StationAdminAssignment, SystemRole
from apps.organizations.models import Department, Station


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("station_scope", [False, True])
def test_concurrent_provisioning_duplicate_returns_field_error(monkeypatch, station_scope):
    clients_and_urls = []
    for index in range(2):
        actor = User.objects.create_user(f"actor{index}@example.test", "Actor", "password")
        department = Department.objects.create(
            name=f"Department {index}", short_code=f"D{index}", created_by=actor
        )
        DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
        station = Station.objects.create(department=department, name="One", short_code="ONE")
        url = (
            reverse("portal-station-administrator-provision", args=(department.id, station.id))
            if station_scope
            else reverse("portal-administrator-provision", args=(department.id,))
        )
        client = Client()
        client.force_login(actor)
        session = client.session
        session["recent_reauthentication_at"] = time()
        session.save()
        clients_and_urls.append((client, url, department))

    # Each form and service validates before either insert. Separate departments
    # avoid the department row lock serializing the attempts before this race.
    barrier = Barrier(2)
    validate = account_services.validate_new_account_email

    def validate_before_competing_insert(email):
        email = validate(email)
        barrier.wait(timeout=10)
        return email

    monkeypatch.setattr(
        account_services, "validate_new_account_email", validate_before_competing_insert
    )

    def provision(client, url, department):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '15s'")
                cursor.execute("SET lock_timeout = '15s'")
            response = client.post(
                url,
                {"email": "new@example.test", "display_name": "New"},
                HTTP_HX_REQUEST="true",
            )
            assert response.status_code == 200
            return response, department
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(provision, *args) for args in clients_and_urls]
        results = [future.result(timeout=30) for future in futures]

    successes = [
        (response, dept)
        for response, dept in results
        if b"single-use setup link" in response.content
    ]
    failures = [
        (response, dept) for response, dept in results if b"already exists" in response.content
    ]
    assert len(successes) == len(failures) == 1
    assert list(failures[0][0].context["form"].errors) == ["email"]
    user = User.objects.get(email="new@example.test")
    assert not user.is_active and not user.has_usable_password()
    assert AccountSetupToken.objects.filter(user=user).count() == 1
    authorities = (
        StationAdminAssignment.objects.filter(user=user).values_list(
            "station__department_id", flat=True
        )
        if station_scope
        else DepartmentMembership.objects.filter(user=user).values_list("department_id", flat=True)
    )
    assert list(authorities) == [successes[0][1].id]
    assert AuditEvent.objects.filter(target_uuid=user.id).count() == 1
    assert not AuditEvent.objects.filter(
        department=failures[0][1], action__endswith="admin_provisioned"
    ).exists()


@pytest.mark.django_db
def test_setup_creation_does_not_mask_other_integrity_errors(monkeypatch):
    actor = User.objects.create_user("actor@example.test", "Actor", "password")

    def fail_token_creation(**kwargs):
        raise IntegrityError("unrelated constraint")

    monkeypatch.setattr(account_services, "create_setup_token_for_user", fail_token_creation)
    with pytest.raises(IntegrityError, match="unrelated constraint"):
        account_services.create_setup_token(
            actor=actor, email="new@example.test", display_name="New"
        )
    assert not User.objects.filter(email="new@example.test").exists()
    assert not AccountSetupToken.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("station_scope", [False, True])
@pytest.mark.parametrize("after_form_validation", [False, True])
def test_existing_provisioning_pages_handle_duplicate_email(
    client, monkeypatch, station_scope, after_form_validation
):
    from apps.portal import views

    actor = User.objects.create_user("actor@example.test", "Actor", "password")
    department = Department.objects.create(name="One", short_code="ONE", created_by=actor)
    station = Station.objects.create(department=department, name="One", short_code="ONE")
    if station_scope:
        DepartmentMembership.objects.create(user=actor, department=department, created_by=actor)
        url = reverse("portal-station-manage", args=(station.id,))
        service_name = "provision_station_admin"
    else:
        SystemRole.objects.create(user=actor)
        url = reverse("portal-system-department", args=(department.id,))
        service_name = "provision_department_admin"
    if after_form_validation:
        # The real PostgreSQL insert race is covered above; ensure the other
        # callers also render the service's conflict as an email field error.
        def competing_email(**kwargs):
            raise account_services.DuplicateEmailError

        monkeypatch.setattr(views, service_name, competing_email)
    else:
        User.objects.create_user("existing@example.test", "Existing", "password")
    client.force_login(actor)
    session = client.session
    session["recent_reauthentication_at"] = time()
    session.save()
    response = client.post(
        url,
        {"action": "provision", "email": "existing@example.test", "display_name": "Duplicate"},
    )
    assert response.status_code == 200
    assert list(response.context["admin_form"].errors) == ["email"]
    assert not AccountSetupToken.objects.exists()
