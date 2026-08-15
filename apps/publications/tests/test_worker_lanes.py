"""Regression coverage for the isolated publication worker lanes."""

from unittest.mock import Mock, patch

import pytest

from apps.publications.management.commands.process_publication_jobs import Command
from apps.publications.work_cycle import (
    BuildCycleResult,
    DeliveryCycleResult,
    process_build_cycle,
    process_delivery_cycle,
)


def test_delivery_cycle_processes_grants_and_manifests_but_never_builds():
    with (
        patch(
            "apps.publications.work_cycle.process_next_dataset_key_grant",
            side_effect=[object(), None],
        ) as grants,
        patch(
            "apps.publications.work_cycle.process_next_signed_manifest",
            side_effect=[object(), None],
        ) as manifests,
        patch("apps.publications.work_cycle.process_next_job") as builds,
        patch("apps.publications.work_cycle.recover_stale_jobs") as recovery,
    ):
        result = process_delivery_cycle(batch_size=3)

    assert result.key_grants == 1
    assert result.manifests == 1
    assert grants.call_count == manifests.call_count == 2
    builds.assert_not_called()
    recovery.assert_not_called()


def test_build_cycle_processes_builds_but_never_delivery_work():
    with (
        patch("apps.publications.work_cycle.recover_stale_jobs", return_value=1) as recovery,
        patch(
            "apps.publications.work_cycle.process_next_job", side_effect=[object(), None]
        ) as builds,
        patch("apps.publications.work_cycle.process_next_dataset_key_grant") as grants,
        patch("apps.publications.work_cycle.process_next_signed_manifest") as manifests,
    ):
        result = process_build_cycle(batch_size=3)

    assert result.dataset_builds == 1
    assert result.recovered == 1
    recovery.assert_called_once()
    assert builds.call_count == 2
    grants.assert_not_called()
    manifests.assert_not_called()


def test_delivery_forever_sleeps_only_when_idle_and_continues_when_busy():
    busy = DeliveryCycleResult(key_grants=1, manifests=0, elapsed_seconds=0.01)
    idle = DeliveryCycleResult(key_grants=0, manifests=0, elapsed_seconds=0.01)
    stop = RuntimeError("stop test loop")
    with (
        patch(
            "apps.publications.management.commands.process_publication_jobs.process_delivery_cycle",
            side_effect=[busy, idle],
        ) as cycle,
        patch(
            "apps.publications.management.commands.process_publication_jobs.time.sleep",
            side_effect=stop,
        ) as sleep,
        pytest.raises(RuntimeError, match="stop test loop"),
    ):
        Command().handle(delivery=True, build=False, forever=True, poll_seconds=2.0)

    assert cycle.call_count == 2
    sleep.assert_called_once_with(2.0)


def test_delivery_idle_one_shot_does_not_emit_a_log_line():
    command = Command()
    command.stdout = Mock()
    with patch(
        "apps.publications.management.commands.process_publication_jobs.process_delivery_cycle",
        return_value=DeliveryCycleResult(key_grants=0, manifests=0, elapsed_seconds=0.01),
    ):
        command.handle(delivery=True, build=False, forever=False, poll_seconds=2.0)
    command.stdout.write.assert_not_called()


def test_build_one_shot_never_invokes_delivery_cycle():
    command = Command()
    command.stdout = Mock()
    with (
        patch(
            "apps.publications.management.commands.process_publication_jobs.process_build_cycle",
            return_value=BuildCycleResult(dataset_builds=0, recovered=0, elapsed_seconds=0.01),
        ) as build,
        patch(
            "apps.publications.management.commands.process_publication_jobs.process_delivery_cycle"
        ) as delivery,
    ):
        command.handle(delivery=False, build=True, forever=False, poll_seconds=2.0)
    build.assert_called_once()
    delivery.assert_not_called()


def test_invalid_delivery_poll_interval_is_rejected():
    with pytest.raises(ValueError, match="must be positive"):
        Command().handle(delivery=True, build=False, forever=False, poll_seconds=0)
