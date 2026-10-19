"""File-library adapter for the standard publication lifecycle."""

import uuid
from typing import Any, cast

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.file_library.models import Content, Library, PublicationContent
from apps.publications.builders import PublicationBuildError
from apps.publications.document_dataset_adapters import FrozenDocument
from apps.publications.models import DatasetScopeState


def scope_kwargs(library):
    return {
        "department": library.department if library.department_id else library.station.department,
        "station": library.station,
        "dataset_type_code": "department_files" if library.department_id else "station_files",
        "scope_type": "DEPARTMENT" if library.department_id else "STATION",
    }


def lock_scope(library):
    from apps.file_library.capacity import department_for, lock_department_capacity

    lock_department_capacity(department_for(library).id, reassess=False)
    scope, _ = DatasetScopeState.objects.get_or_create(
        **scope_kwargs(library), defaults={"source_revision": library.source_revision}
    )
    return DatasetScopeState.objects.select_for_update().get(pk=scope.pk)


@transaction.atomic
def source_payload(*, department, station):
    from apps.file_library.capacity import lock_department_capacity

    lock_department_capacity(department.id, reassess=False)
    library, _ = Library.objects.get_or_create(
        **({"station": station} if station else {"department": department})
    )
    lock_scope(library)
    library.refresh_from_db()
    entries = list(library.entries.select_related("content").order_by("id"))
    return {
        "library": {
            "id": str(library.id),
            "name": library.name,
            "scope": {
                "type": "STATION" if station else "DEPARTMENT",
                "department_id": str(department.id),
                **({"station_id": str(station.id)} if station else {}),
            },
        },
        "source_revision": library.source_revision,
        "folders": [
            {
                "id": str(e.id),
                "name": e.name,
                "parent_id": str(e.parent_id) if e.parent_id else None,
            }
            for e in entries
            if e.is_folder
        ],
        "documents": [
            {
                "id": str(e.id),
                "name": e.name,
                "folder_id": str(e.parent_id) if e.parent_id else None,
                "size_bytes": cast(Content, e.content).size_bytes,
                "sha256": cast(Content, e.content).sha256,
                "content_id": str(e.content_id),
            }
            for e in entries
            if not e.is_folder
        ],
    }


def summary(snapshot, revision):
    return {
        "document_count": len(snapshot["documents"]),
        "total_accepted_bytes": sum(e["size_bytes"] for e in snapshot["documents"]),
        "source_revision": revision,
    }


def build_summary(*, department, station, source_revision):
    return summary(source_payload(department=department, station=station), source_revision)


def pin_source(publication):
    # Called under the scope lock, before BUILDING is committed. Edits acquire
    # that same lock before touching the source; current entries still protect bytes.
    snapshot = publication.source_snapshot
    for entry in snapshot["documents"]:
        content = Content.objects.get(pk=entry["content_id"], library_id=snapshot["library"]["id"])
        PublicationContent.objects.create(
            publication=publication, content=content, document_id=entry["id"]
        )


class FileLibraryAdapter:
    manifest_metadata_key = "file"

    def frozen_documents(self, publication):
        snapshot = publication.source_snapshot
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("documents"), list):
            raise PublicationBuildError("Frozen file library is unavailable.")
        refs = {
            str(ref.document_id): ref.content
            for ref in PublicationContent.objects.filter(publication=publication).select_related(
                "content"
            )
        }
        entries = snapshot["documents"]
        if set(refs) != {e["id"] for e in entries} or len(refs) != len(entries):
            raise PublicationBuildError("Frozen library membership is incomplete.")
        result = []
        for entry in entries:
            content = refs[entry["id"]]
            if (
                str(content.id) != entry["content_id"]
                or content.sha256 != entry["sha256"]
                or content.size_bytes != entry["size_bytes"]
                or str(content.library_id) != snapshot["library"]["id"]
            ):
                raise PublicationBuildError("Frozen library content is invalid.")
            result.append(
                FrozenDocument(
                    uuid.UUID(entry["id"]),
                    content.sha256,
                    {k: v for k, v in entry.items() if k != "content_id"},
                    content.storage_key,
                )
            )
        return tuple(result)


@transaction.atomic
def initialize_library(library):
    from apps.publications.models import PublicationJob
    from apps.publications.services import enqueue_publication_job

    scope = lock_scope(library)
    if (
        scope.current_published_publication_id is None
        and not PublicationJob.objects.filter(scope_state=scope).exists()
    ):
        kwargs = scope_kwargs(library)
        kwargs.pop("scope_type")
        enqueue_publication_job(**kwargs)
    return scope


def publish_now(*, actor, library_id):
    # Commit an expired-grant audit before a denied publication transaction can roll back.
    from apps.file_library.services import active_temporary_access

    active_temporary_access(actor)
    try:
        return _publish_now(actor=actor, library_id=library_id)
    except PermissionDenied:
        active_temporary_access(actor)
        raise


@transaction.atomic
def _publish_now(*, actor, library_id):
    from apps.audit.services import record_event
    from apps.file_library.services import locked_library
    from apps.publications.models import PublicationJob
    from apps.publications.services import enqueue_publication_job
    from apps.publications.wake import wake_publication_build_worker

    library = locked_library(actor, library_id)
    kwargs = scope_kwargs(library)
    kwargs.pop("scope_type")
    job = enqueue_publication_job(
        **kwargs, requested_by=actor, trigger_type=PublicationJob.TriggerType.USER_REQUEST
    )
    transaction.on_commit(wake_publication_build_worker)
    record_event(
        action="file_library.publish_requested",
        actor_user=actor,
        department=kwargs["department"],
        station=kwargs["station"],
        target_type="library",
        target_uuid=library.id,
    )
    if library.department_id:
        from apps.file_library.services import revoke_temporary_access

        revoke_temporary_access(actor=actor, reason="department_mutation")
    return job


def publication_state(library):
    from apps.publications.models import PublicationJob

    scope = DatasetScopeState.objects.filter(**scope_kwargs(library)).first()
    if scope is None:
        return {"label": "Unpublished changes"}
    current = scope.current_published_publication
    job = PublicationJob.objects.filter(scope_state=scope).order_by("-created_at").first()
    label = (
        "Published"
        if current and current.source_revision == library.source_revision
        else "Unpublished changes"
    )
    if job and job.status in {"PENDING", "RUNNING", "FAILED"}:
        label = {"PENDING": "Queued", "RUNNING": "Building", "FAILED": "Failed"}[job.status]
    from apps.file_library.capacity import presentation_summary
    from apps.file_library.models import CapacityReview

    review = (
        CapacityReview.objects.filter(
            library=library,
            publication_snapshot__isnull=False,
            consumed_at__isnull=True,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
        .order_by("-created_at", "id")
        .first()
    )
    return {
        "capacity_review": review,
        "capacity_summary": presentation_summary(review.projection) if review else None,
        "label": label,
        "current": current,
        "job": job,
        "unpublished": not current or current.source_revision != library.source_revision,
    }


def initialize_missing_libraries():
    """Worker discovery also publishes scopes never visited in the portal."""
    from apps.organizations.models import Department, Station

    for model, field, code in (
        (Department, "department", "department_files"),
        (Station, "station", "station_files"),
    ):
        existing = DatasetScopeState.objects.filter(dataset_type_code=code).values_list(
            field + "_id", flat=True
        )
        active: dict[str, Any] = (
            {"status": "ACTIVE"}
            if model == Department
            else {"active": True, "department__status": "ACTIVE"}
        )
        for owner in model.objects.filter(**active).exclude(pk__in=existing):
            library, _ = Library.objects.get_or_create(**cast(dict[str, Any], {field: owner}))
            initialize_library(library)
