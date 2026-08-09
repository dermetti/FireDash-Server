import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.utils import timezone

from apps.accounts.models import User
from apps.assignments.models import TabletVehicleAssignment
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station, Vehicle
from apps.publications.hpke import HPKE_CIPHERSUITE, serialize_p256_public_key
from apps.tablets.models import Tablet
from apps.tablets.services import (
    complete_adoption,
    create_adoption_invitation,
    create_adoption_request,
)


def _p256_public_key():
    return serialize_p256_public_key(ec.generate_private_key(ec.SECP256R1()).public_key())


@pytest.fixture
def department_user(db):
    return User.objects.create_user("lifecycle@example.test", "Lifecycle User", "safe-password")


@pytest.fixture
def operational_tablet(db, department_user):
    user = department_user
    department = Department.objects.create(name="Lifecycle Dept", short_code="LCD", created_by=user)
    DepartmentMembership.objects.create(user=user, department=department, created_by=user)
    station = Station.objects.create(department=department, name="Station A", short_code="STA")
    vehicle = Vehicle.objects.create(
        department=department, station=station, display_name="Engine 1"
    )
    tablet = Tablet.objects.create(department=department, display_name="Test Tablet")
    TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=timezone.now(), created_by=user
    )
    return user, tablet


def _adopt(user, tablet, installation_uuid=None):
    _, token = create_adoption_invitation(actor=user, tablet=tablet)
    key = _p256_public_key()
    challenge = create_adoption_request(
        token=token,
        installation_uuid=installation_uuid or uuid.uuid4(),
        app_version="1.0",
        hpke_public_key=key,
        hpke_ciphersuite=HPKE_CIPHERSUITE,
    )
    return complete_adoption(
        request_id=challenge.request.id,
        challenge_response=challenge.request.expected_hmac_digest,
        confirmed=True,
    )
