"""One-bit systemd socket wakeup and transactional rebuild regression tests."""

from unittest.mock import patch

import pytest
from django.db import transaction
from django.test import override_settings

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.publications.models import DatasetScopeState, PublicationJob
from apps.publications.services import bulk_request_rebuilds, request_rebuild
from apps.publications.wake import wake_publication_build_worker


@pytest.fixture
def wake_context(db):
    admin = User.objects.create_user("wake@example.test", "Wake Admin", "safe-password")
    department = Department.objects.create(name="Wake Dept", short_code="WAK", created_by=admin)
    DepartmentMembership.objects.create(user=admin, department=department, created_by=admin)
    station = Station.objects.create(department=department, name="Wake Station", short_code="WST")
    return admin, department, station


@pytest.mark.django_db(transaction=True)
def test_rolled_back_manual_rebuild_does_not_wake(wake_context):
    admin, department, station = wake_context
    with patch("apps.publications.services.wake_publication_build_worker") as wake:
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                request_rebuild(
                    actor=admin,
                    department=department,
                    station=station,
                    dataset_type_code="station_personnel",
                )
                raise RuntimeError("rollback")
    wake.assert_not_called()
    assert not PublicationJob.objects.filter(department=department).exists()


@pytest.mark.django_db(transaction=True)
def test_successful_manual_rebuild_commits_one_wake_and_coalesces(wake_context):
    admin, department, station = wake_context
    with patch("apps.publications.services.wake_publication_build_worker") as wake:
        request_rebuild(
            actor=admin,
            department=department,
            station=station,
            dataset_type_code="station_personnel",
        )
        wake.assert_called_once()
        request_rebuild(
            actor=admin,
            department=department,
            station=station,
            dataset_type_code="station_personnel",
        )
    assert PublicationJob.objects.filter(department=department).count() == 1
    assert wake.call_count == 2  # one harmless advisory notification per committed click


@pytest.mark.django_db(transaction=True)
def test_successful_bulk_commit_sends_one_wake(wake_context):
    admin, department, station = wake_context
    DatasetScopeState.objects.create(
        department=department, station=station, dataset_type_code="station_personnel"
    )
    with patch("apps.publications.services.wake_publication_build_worker") as wake:
        result = bulk_request_rebuilds(actor=admin, department=department)
    assert result["created"] == 1
    wake.assert_called_once()


def test_wake_connects_and_closes_without_sending_job_data():
    class Client:
        sent = False
        timeout = None
        address = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, value):
            self.timeout = value

        def connect(self, address):
            self.address = address

    client = Client()
    with (
        override_settings(
            PUBLICATION_BUILD_WAKE_SOCKET_PATH="/run/fire-backend/publication-build.sock",
            PUBLICATION_BUILD_WAKE_TIMEOUT_SECONDS=0.25,
        ),
        patch("apps.publications.wake.socket.socket", return_value=client),
    ):
        assert wake_publication_build_worker() is True
    assert client.address == "/run/fire-backend/publication-build.sock"
    assert client.timeout == 0.25
    assert client.sent is False


def test_unavailable_wake_socket_leaves_failure_nonfatal(caplog):
    with patch("apps.publications.wake.socket.socket", side_effect=OSError("unavailable")):
        assert wake_publication_build_worker() is False
    assert "nightly timer remains fallback" in caplog.text


def test_web_wake_helper_has_no_systemctl_sudo_or_dbus_escalation():
    import inspect

    import apps.publications.wake as wake_module

    source = inspect.getsource(wake_module)
    assert "systemctl" not in source
    assert "sudo" not in source
    assert "dbus" not in source.lower()
