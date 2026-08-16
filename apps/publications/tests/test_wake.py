"""One-bit systemd socket wakeup and transactional rebuild regression tests."""

import os
from unittest.mock import patch

import pytest
from django.db import transaction
from django.test import override_settings

from apps.accounts.models import User
from apps.authorization.models import DepartmentMembership
from apps.organizations.models import Department, Station
from apps.publications.models import DatasetScopeState, PublicationJob
from apps.publications.services import bulk_request_rebuilds, request_rebuild
from apps.publications.wake import (
    PUBLICATION_BUILD_WAKE_FD_NAME,
    drain_publication_build_activation_wakes,
    wake_publication_build_worker,
)


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


def test_socket_activation_drain_consumes_all_connect_and_close_wakes(monkeypatch):
    class Client:
        closed = False

        def close(self):
            self.closed = True

    pending_clients = [Client(), Client()]

    class Listener:
        closed = False
        nonblocking = None

        def __init__(self, _family=None, _type=None, *, fileno=None):
            assert fileno == 70

        def setblocking(self, value):
            self.nonblocking = value

        def accept(self):
            if pending_clients:
                return pending_clients.pop(), None
            raise BlockingIOError

        def close(self):
            self.closed = True

    listeners = []

    def socket_factory(_family=None, _type=None, *, fileno=None):
        listener = Listener(fileno=fileno)
        listeners.append(listener)
        return listener

    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setenv("LISTEN_FDNAMES", PUBLICATION_BUILD_WAKE_FD_NAME)
    with (
        patch("apps.publications.wake.os.dup", return_value=70) as duplicate,
        patch("apps.publications.wake.socket.socket", side_effect=socket_factory),
    ):
        assert drain_publication_build_activation_wakes() == 2
    listener = listeners[0]
    duplicate.assert_called_once_with(3)
    assert listener.nonblocking is False
    assert listener.closed is True


def test_activation_drain_preserves_original_fd_for_a_second_drain(monkeypatch):
    class Client:
        def close(self):
            pass

    pending_clients = [Client()]
    listener_fds = []

    class Listener:
        def __init__(self, _family=None, _type=None, *, fileno=None):
            listener_fds.append(fileno)

        def setblocking(self, _value):
            pass

        def accept(self):
            if pending_clients:
                return pending_clients.pop(), None
            raise BlockingIOError

        def close(self):
            pass

    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setenv("LISTEN_FDNAMES", PUBLICATION_BUILD_WAKE_FD_NAME)
    with (
        patch("apps.publications.wake.os.dup", side_effect=[70, 71]) as duplicate,
        patch("apps.publications.wake.socket.socket", side_effect=Listener),
    ):
        assert drain_publication_build_activation_wakes() == 1
        # A wake arriving during the build is consumed by the post-build drain.
        pending_clients.append(Client())
        assert drain_publication_build_activation_wakes() == 1

    assert duplicate.call_args_list[0].args == duplicate.call_args_list[1].args == (3,)
    assert listener_fds == [70, 71]


def test_activation_drain_empty_completion_does_not_warn_or_wrap_original_fd(monkeypatch, caplog):
    class Listener:
        def __init__(self, _family=None, _type=None, *, fileno=None):
            assert fileno == 70

        def setblocking(self, _value):
            pass

        def accept(self):
            raise BlockingIOError

        def close(self):
            pass

    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setenv("LISTEN_FDNAMES", PUBLICATION_BUILD_WAKE_FD_NAME)
    with (
        patch("apps.publications.wake.os.dup", return_value=70) as duplicate,
        patch("apps.publications.wake.socket.socket", side_effect=Listener),
    ):
        assert drain_publication_build_activation_wakes() == 0

    duplicate.assert_called_once_with(3)
    assert "Could not drain the publication build wake socket." not in caplog.text


def test_timer_or_manual_build_has_no_activation_socket(monkeypatch):
    monkeypatch.delenv("LISTEN_FDS", raising=False)
    monkeypatch.delenv("LISTEN_PID", raising=False)
    with patch("apps.publications.wake.socket.socket") as socket_factory:
        assert drain_publication_build_activation_wakes() == 0
    socket_factory.assert_not_called()


def test_activation_drain_ignores_unrecognised_inherited_descriptors(monkeypatch):
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setenv("LISTEN_FDNAMES", "unrelated-service-socket")
    with patch("apps.publications.wake.socket.socket") as socket_factory:
        assert drain_publication_build_activation_wakes() == 0
    socket_factory.assert_not_called()


def test_web_wake_helper_has_no_systemctl_sudo_or_dbus_escalation():
    import inspect

    import apps.publications.wake as wake_module

    source = inspect.getsource(wake_module)
    assert "systemctl" not in source
    assert "sudo" not in source
    assert "dbus" not in source.lower()
