"""Bounded, fair publication-worker orchestration without credential access."""

from dataclasses import dataclass
from datetime import timedelta
from time import monotonic

from django.conf import settings

from apps.publications.artifacts import cleanup_stale_artifacts
from apps.publications.services import process_next_job, recover_stale_jobs
from apps.publications.worker_grants import (
    process_next_dataset_key_grant,
    process_next_signed_manifest,
)


@dataclass(frozen=True)
class WorkCycleResult:
    dataset_builds: int
    key_grants: int
    manifests: int
    recovered: int
    artifacts_cleaned: int
    elapsed_seconds: float


def process_work_cycle(*, batch_size: int | None = None) -> WorkCycleResult:
    """Give each work class one fair, bounded opportunity per cycle slot."""
    started = monotonic()
    limit = batch_size or settings.PUBLICATION_WORKER_BATCH_SIZE
    recovered = recover_stale_jobs(
        timeout=timedelta(seconds=settings.PUBLICATION_JOB_HEARTBEAT_TIMEOUT_SECONDS),
        max_attempts=settings.PUBLICATION_JOB_MAX_ATTEMPTS,
    )
    artifacts_cleaned = cleanup_stale_artifacts()
    builds = grants = manifests = 0
    for _ in range(limit):
        job = process_next_job()
        grant = process_next_dataset_key_grant()
        manifest = process_next_signed_manifest()
        builds += int(job is not None)
        grants += int(grant is not None)
        manifests += int(manifest is not None)
        if job is None and grant is None and manifest is None:
            break
    return WorkCycleResult(
        dataset_builds=builds,
        key_grants=grants,
        manifests=manifests,
        recovered=recovered,
        artifacts_cleaned=artifacts_cleaned,
        elapsed_seconds=monotonic() - started,
    )
