import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.authorization.models import (
    VEHICLE_RESCUE_GUIDES_DEFAULT_WEB_URL,
    SystemRole,
    VehicleRescueGuidesConfiguration,
)
from apps.authorization.services import (
    set_vehicle_rescue_guides_web_url,
    vehicle_rescue_guides_capability,
)
from apps.publications.models import DatasetPublication, PublicationJob


def test_vehicle_rescue_guides_singleton_default_and_url_validation(db):
    configuration, _ = VehicleRescueGuidesConfiguration.objects.get_or_create(singleton=True)
    assert vehicle_rescue_guides_capability() == {
        "provider": "euro_rescue",
        "mode": "web",
        "web_url": VEHICLE_RESCUE_GUIDES_DEFAULT_WEB_URL,
    }
    for invalid_url in (
        "http://rescue.euroncap.com/",
        "https:///missing-host",
        "https://user:password@rescue.euroncap.com/",
        " https://rescue.euroncap.com/",
        "https://rescue.euroncap.com/" + "x" * 2048,
    ):
        configuration.web_url = invalid_url
        with pytest.raises(ValidationError):
            configuration.full_clean()
    VehicleRescueGuidesConfiguration.objects.filter(pk=configuration.pk).update(
        web_url="http://invalid.example/"
    )
    with pytest.raises(ValidationError):
        vehicle_rescue_guides_capability()


def test_system_admin_updates_global_url_audits_and_only_invalidates_manifests(db):
    admin = User.objects.create_user("rescue-system@example.test", "System", "safe-password")
    SystemRole.objects.create(user=admin)
    before_publications = DatasetPublication.objects.count()
    before_jobs = PublicationJob.objects.count()
    updated = set_vehicle_rescue_guides_web_url(
        actor=admin, web_url="https://rescue.euroncap.com/guides?lang=de"
    )
    assert updated.web_url == "https://rescue.euroncap.com/guides?lang=de"
    assert DatasetPublication.objects.count() == before_publications
    assert PublicationJob.objects.count() == before_jobs
    event = AuditEvent.objects.get(action="vehicle_rescue_guides_configuration.updated")
    assert event.actor_user == admin
    assert event.metadata == {
        "old_web_url": VEHICLE_RESCUE_GUIDES_DEFAULT_WEB_URL,
        "new_web_url": "https://rescue.euroncap.com/guides?lang=de",
    }


def test_non_system_admin_cannot_update_vehicle_rescue_guides_configuration(db):
    user = User.objects.create_user("rescue-department@example.test", "Department", "safe-password")
    with pytest.raises(PermissionDenied, match="System administrator role is required"):
        set_vehicle_rescue_guides_web_url(actor=user, web_url="https://rescue.euroncap.com/new")
