# ruff: noqa: E501
"""Bounded worker lanes for publication builds and tablet delivery."""

from dataclasses import dataclass
from datetime import timedelta
from time import monotonic
from uuid import UUID

from django.conf import settings

from apps.publications.document_v2 import process_next_generation_key_grant
from apps.publications.fire_plan_v2_delivery import process_next_fire_plan_v2_generation_key_grant
from apps.publications.models import SignedManifest
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


@dataclass(frozen=True)
class DeliveryCycleResult:
    key_grants: int
    manifests: int
    elapsed_seconds: float
    deferred_manifests: int = 0

    @property
    def processed(self) -> int:
        return self.key_grants + self.manifests

    @property
    def forward_progress(self) -> int:
        """Items that reached a durable non-deferred outcome this cycle."""
        return self.key_grants + self.manifests - self.deferred_manifests


@dataclass(frozen=True)
class BuildCycleResult:
    dataset_builds: int
    recovered: int
    elapsed_seconds: float

    @property
    def processed(self) -> int:
        return self.dataset_builds


def process_delivery_cycle(*, batch_size: int | None = None) -> DeliveryCycleResult:
    """Process only latency-sensitive grant and manifest work."""
    started = monotonic()
    limit = batch_size or settings.PUBLICATION_WORKER_BATCH_SIZE
    grants = manifests = deferred_manifests = 0
    deferred_manifest_ids: set[UUID] = set()
    for _ in range(limit):
        grant = process_next_dataset_key_grant()
        v2_grant = process_next_fire_plan_v2_generation_key_grant()
        document_grant = process_next_generation_key_grant()
        manifest = process_next_signed_manifest(exclude_ids=deferred_manifest_ids)
        grants += (
            int(grant is not None) + int(v2_grant is not None) + int(document_grant is not None)
        )
        manifests += int(manifest is not None)
        if (
            manifest is not None
            and getattr(manifest, "status", None) == SignedManifest.Status.PENDING
        ):
            deferred_manifests += 1
            deferred_manifest_ids.add(manifest.id)
        if grant is None and v2_grant is None and document_grant is None and manifest is None:
            break
    return DeliveryCycleResult(
        key_grants=grants,
        manifests=manifests,
        elapsed_seconds=monotonic() - started,
        deferred_manifests=deferred_manifests,
    )


def process_build_cycle(*, batch_size: int | None = None) -> BuildCycleResult:
    """Recover and process only dataset publication build jobs."""
    started = monotonic()
    limit = batch_size or settings.PUBLICATION_WORKER_BATCH_SIZE
    recovered = recover_stale_jobs(
        timeout=timedelta(seconds=settings.PUBLICATION_JOB_HEARTBEAT_TIMEOUT_SECONDS),
        max_attempts=settings.PUBLICATION_JOB_MAX_ATTEMPTS,
    )
    builds = 0
    for _ in range(limit):
        job = process_next_job()
        builds += int(job is not None)
        if job is None:
            break
    return BuildCycleResult(
        dataset_builds=builds,
        recovered=recovered,
        elapsed_seconds=monotonic() - started,
    )


def process_work_cycle(*, batch_size: int | None = None) -> WorkCycleResult:
    """Give each work class one fair, bounded opportunity per cycle slot."""
    started = monotonic()
    limit = batch_size or settings.PUBLICATION_WORKER_BATCH_SIZE
    build = process_build_cycle(batch_size=limit)
    delivery = process_delivery_cycle(batch_size=limit)
    return WorkCycleResult(
        dataset_builds=build.dataset_builds,
        key_grants=delivery.key_grants,
        manifests=delivery.manifests,
        recovered=build.recovered,
        artifacts_cleaned=0,
        elapsed_seconds=monotonic() - started,
    )
