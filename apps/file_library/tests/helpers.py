"""Shared tablet fixtures and source-lock concurrency assertions."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from time import monotonic

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from apps.assignments.models import TabletVehicleAssignment
from apps.organizations.models import Vehicle
from apps.tablets.models import Tablet
from apps.tablets.tests.test_management_ui import _adopted_installation


def tablet_for(scope, *, station=None, free=1_000_000, total=1_000_000):
    station = station or scope[5]
    tablet = Tablet.objects.create(
        department=station.department, status="ACTIVE", display_name=str(uuid.uuid4())
    )
    vehicle = Vehicle.objects.create(
        department=station.department, station=station, display_name=str(uuid.uuid4())
    )
    TabletVehicleAssignment.objects.create(
        tablet=tablet, vehicle=vehicle, valid_from=timezone.now(), created_by=scope[0]
    )
    installation = _adopted_installation(tablet)
    installation.free_storage_bytes = free
    installation.total_storage_bytes = total
    installation.storage_observed_at = timezone.now()
    installation.save(
        update_fields=(
            "free_storage_bytes",
            "total_storage_bytes",
            "storage_observed_at",
        )
    )
    return tablet, installation


def serialized_operations(first, second, *, while_blocked=None):
    """Hold the first commit and prove the second waits on its advisory lock.

    Each worker owns a real connection. Observing pg_locks (rather than merely
    starting a thread) establishes that the competing operation reached the DB.
    Timeouts bound failures, including a regression to row-lock inversion.
    """
    assert connection.vendor == "postgresql"
    held, release = Event(), Event()
    second_pid: Queue[int] = Queue()

    def run(action, *, hold=False):
        close_old_connections()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout = '15s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                if not hold:
                    second_pid.put(pid)
                result = action()
                if hold:
                    held.set()
                    assert release.wait(10), "First transaction was never released"
                return result
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        holding = pool.submit(run, first, hold=True)
        waiting = None
        try:
            assert held.wait(10), "First operation did not complete"
            waiting = pool.submit(run, second)
            pid = second_pid.get(timeout=10)
            deadline = monotonic() + 8
            while True:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE pid = %s AND locktype = 'advisory' AND NOT granted)",
                        [pid],
                    )
                    blocked = cursor.fetchone()[0]
                if blocked:
                    break
                if waiting.done():
                    waiting.result()  # Surface errors before reporting missing synchronization.
                    pytest.fail("Competing operation bypassed the department source lock")
                assert monotonic() < deadline, "No advisory lock wait observed"
                release.wait(0.01)
            if while_blocked:
                while_blocked()
        finally:
            release.set()
        return holding.result(timeout=20), waiting.result(timeout=20)
