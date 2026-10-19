"""Conservative PDF ciphertext estimates and publication administrative reviews.

No installation acknowledgments or cache receipts are inferred. Published-at-
measurement content is an estimated baseline; new ciphertext is reserved at full
size, with no credit for source deletions or replacement reclamation.
"""

import hashlib
import json
from datetime import datetime, timedelta
from fractions import Fraction
from typing import cast

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.audit.services import record_event
from apps.authorization.services import require_department_admin
from apps.file_library.models import CapacityReview, Content, Entry, Library, StorageImpact
from apps.publications.models import DatasetPublication, DocumentArtifact
from apps.tablets.models import AppInstallation, Tablet

GCM_TAG_BYTES = 16


def pending_department_reviews(department):
    """Read model for authorized department views, including all station libraries."""
    return (
        CapacityReview.objects.filter(
            Q(library__department=department) | Q(library__station__department=department),
            decision="pending",
            publication_snapshot__isnull=False,
            superseded_at__isnull=True,
            consumed_at__isnull=True,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
        .select_related("library__station", "requested_by")
        .order_by("created_at", "id")
    )


def department_for(library):
    return library.department if library.department_id else library.station.department


def lock_capacity(library):
    """Serialize source reservations and eligibility mutations across both scopes."""
    lock_department_capacity(department_for(library).id)


def lock_department_capacity(department_id, *, reassess=True):
    """Acquire inside atomic(), before any conflicting row locks.

    Source operations take this lock before scope/library/review/installation
    locks. Eligibility mutations take it before Vehicle -> Tablet (ascending PK)
    -> assignment -> installation locks, skipping rows they do not need.
    Resolve the department from persisted state before entering this boundary;
    department ownership is immutable in these services.
    """
    key = int.from_bytes(
        hashlib.sha256(f"library-capacity:{department_id}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])
    if reassess:
        from apps.file_library.publication_capacity import schedule_reassessment

        schedule_reassessment(department_id)


def _identity(library_id, document_id, sha256):
    return f"{library_id}:{document_id}:{sha256}"


def project(library, operation, *, candidate_snapshot=None):
    """Called within a transaction under the department capacity/source lock.

    Lock current installations to keep telemetry/replacement stable until this
    source operation or review decision commits. Eligibility and assignment
    mutations share the department lock, keeping this population authoritative
    through confirmation and consumption. Assignment identities are also bound
    into confirmations to detect changes between transactions.
    Queries are batched; no sanitizer or publication builder runs here.
    """
    now = timezone.now()
    department = department_for(library)
    assignments = {
        "vehicle_assignments__valid_from__lte": now,
        "vehicle_assignments__ended_at__isnull": True,
        "vehicle_assignments__vehicle__active": True,
        "vehicle_assignments__vehicle__station__active": True,
        "vehicle_assignments__vehicle__department_id": department.id,
    }
    if library.station_id:
        assignments["vehicle_assignments__vehicle__station_id"] = library.station_id
    tablets = list(
        Tablet.objects.filter(
            Q(vehicle_assignments__valid_until__isnull=True)
            | Q(vehicle_assignments__valid_until__gt=now),
            department=department,
            active=True,
            status="ACTIVE",
            **assignments,
        )
        .order_by("id")
        .values(
            "id",
            "display_name",
            "vehicle_assignments__id",
            "vehicle_assignments__valid_from",
            "vehicle_assignments__vehicle__station_id",
        )
    )
    installations = {
        row.tablet_id: row
        for row in AppInstallation.objects.select_for_update()
        .only(
            "id",
            "tablet_id",
            "status",
            "authorization_valid_until",
            "free_storage_bytes",
            "total_storage_bytes",
            "storage_observed_at",
        )
        .filter(tablet_id__in=[t["id"] for t in tablets], status__in=("ACTIVE", "STALE"))
        .order_by("id")
    }
    station_ids = {t["vehicle_assignments__vehicle__station_id"] for t in tablets}
    libraries = list(
        Library.objects.filter(Q(department=department) | Q(station_id__in=station_ids)).order_by(
            "id"
        )
    )
    by_id = {str(item.id): item for item in libraries}
    desired = {}
    for entry in Entry.objects.filter(library__in=libraries, is_folder=False).select_related(
        "content"
    ):
        desired[_identity(entry.library_id, entry.id, cast(Content, entry.content).sha256)] = (
            str(entry.library_id),
            str(entry.id),
            cast(Content, entry.content).sha256,
            cast(Content, entry.content).size_bytes,
        )
    if operation["kind"] == "publication":
        # Replace this library's live set with the exact candidate, never add a
        # synthetic upload to a source set which already contains that upload.
        desired = {key: value for key, value in desired.items() if value[0] != str(library.id)}
        if candidate_snapshot is None:
            raise ValueError("A frozen publication snapshot is required for capacity assessment.")
        for doc in candidate_snapshot["documents"]:
            value = (str(library.id), doc["id"], doc["sha256"], doc["size_bytes"])
            desired[_identity(*value[:3])] = value
    else:
        # A replacement never takes credit for reclaiming the previous ciphertext.
        for key in list(desired):
            if desired[key][:2] == (str(library.id), operation["document_id"]):
                del desired[key]
        candidate = (
            str(library.id),
            operation["document_id"],
            operation["sha256"],
            operation["size_bytes"],
        )
        desired[_identity(*candidate[:3])] = candidate
    cutoff = now - timedelta(seconds=max(1, settings.TABLET_STORAGE_TELEMETRY_MAX_AGE_SECONDS))
    impacts = list(StorageImpact.objects.filter(library__in=libraries, created_at__gte=cutoff))
    publications = list(
        DatasetPublication.objects.filter(
            department=department,
            dataset_type_code__in=("department_files", "station_files"),
            published_at__isnull=False,
        )
        .filter(Q(station_id__isnull=True) | Q(station_id__in=station_ids))
        .filter(Q(source_snapshot__isnull=False) | Q(published_at__gte=cutoff))
        .only("id", "station_id", "source_snapshot", "published_at")
        .order_by("published_at", "id")
    )
    identities = dict(desired)
    for impact in impacts:
        value = (str(impact.library_id), str(impact.document_id), impact.sha256, impact.size_bytes)
        identities[_identity(*value[:3])] = value
    snapshots = {}
    for publication in publications:
        snapshot = publication.source_snapshot
        if not snapshot:
            continue
        library_id = snapshot["library"]["id"]
        entries = {}
        for doc in snapshot["documents"]:
            value = (library_id, doc["id"], doc["sha256"], doc["size_bytes"])
            key = _identity(*value[:3])
            entries[key] = identities[key] = value
        snapshots[publication.id] = entries
    artifacts = {
        (row.dataset_type_code, str(row.canonical_document_id), row.sanitized_pdf_sha256): row
        for row in DocumentArtifact.objects.filter(
            dataset_type_code__in=("department_files", "station_files"),
            canonical_document_id__in={value[1] for value in identities.values()},
        ).only(
            "id",
            "dataset_type_code",
            "canonical_document_id",
            "sanitized_pdf_sha256",
            "ciphertext_size",
        )
    }

    def encrypted_identity(value):
        lib_id, doc_id, digest, size = value
        owner = by_id.get(lib_id)
        if owner is None:
            return None
        code = "department_files" if owner.department_id else "station_files"
        artifact = artifacts.get((code, doc_id, digest))
        return (
            (str(artifact.id), artifact.ciphertext_size)
            if artifact
            else ("pending:" + _identity(lib_id, doc_id, digest), size + GCM_TAG_BYTES)
        )

    results = []
    for tablet in tablets:
        installation = installations.get(tablet["id"])
        relevant = {
            str(owner.id)
            for owner in libraries
            if owner.department_id
            or owner.station_id == tablet["vehicle_assignments__vehicle__station_id"]
        }
        reason = ""
        free = installation.free_storage_bytes if installation else None
        total = installation.total_storage_bytes if installation else None
        observed = installation.storage_observed_at if installation else None
        if not installation:
            reason = "missing_installation"
        elif installation.status != "ACTIVE" or installation.authorization_valid_until <= now:
            reason = "stale_installation"
        elif observed is None or observed < cutoff or observed > now:
            reason = "missing_or_outdated_telemetry"
        elif free is None or total is None or not 0 <= free <= total or total <= 0:
            reason = "invalid_telemetry"
        elif tablet["vehicle_assignments__valid_from"] > observed:
            reason = "assignment_changed_since_telemetry"
        needed = {key: value for key, value in desired.items() if value[0] in relevant}
        baseline = {}
        if not reason:
            for owner_id in relevant:
                eligible = [
                    p
                    for p in publications
                    if p.source_snapshot
                    and p.source_snapshot["library"]["id"] == owner_id
                    and cast(datetime, p.published_at) <= cast(datetime, observed)
                ]
                if eligible:
                    baseline.update(snapshots[eligible[-1].id])
            for impact in impacts:
                if str(impact.library_id) in relevant and impact.created_at > cast(
                    datetime, observed
                ):
                    value = (
                        str(impact.library_id),
                        str(impact.document_id),
                        impact.sha256,
                        impact.size_bytes,
                    )
                    needed[_identity(*value[:3])] = value
            for publication in publications:
                scope_matches = (
                    publication.station_id is None
                    or publication.station_id == tablet["vehicle_assignments__vehicle__station_id"]
                )
                if scope_matches and cast(datetime, publication.published_at) > cast(
                    datetime, observed
                ):
                    if publication.id not in snapshots:
                        reason = "publication_history_unavailable"
                    else:
                        needed.update(snapshots[publication.id])
        cached = {
            encrypted_identity(value)[0] for value in baseline.values() if encrypted_identity(value)
        }
        ciphertexts = dict(
            encrypted_identity(value) for value in needed.values() if encrypted_identity(value)
        )
        incremental = sum(size for identity, size in ciphertexts.items() if identity not in cached)
        projected = None if reason else cast(int, free) - incremental
        if not reason and cast(int, projected) * 5 < cast(int, total):
            reason = "projected_low_storage"
        results.append(
            {
                "tablet_id": str(tablet["id"]),
                "tablet_name": tablet["display_name"],
                "assignment_id": str(tablet["vehicle_assignments__id"]),
                "assignment_valid_from": tablet["vehicle_assignments__valid_from"].isoformat(),
                "installation_id": str(installation.id) if installation else None,
                "installation_status": installation.status if installation else None,
                "lease_until": installation.authorization_valid_until.isoformat()
                if installation
                else None,
                "free_storage_bytes": free,
                "total_storage_bytes": total,
                "observed_at": observed.isoformat() if observed else None,
                "incremental_ciphertext_bytes": incremental,
                "projected_free_bytes": projected,
                "reason": reason,
                "state": "unknown" if projected is None else ("warning" if reason else "ok"),
            }
        )
    return {
        "library_id": str(library.id),
        "department_id": str(department.id),
        "station_id": str(library.station_id) if library.station_id else None,
        "source_revisions": {
            str(owner.id): owner.source_revision
            for owner in libraries
            if owner.id == library.id or owner.source_revision or owner.usage_bytes
        },
        "operation": operation,
        "tablets": results,
    }


def presentation_summary(projection):
    """Summarize stored estimates without changing their fingerprint or risk rules."""
    rows = []
    measured = []
    for original in projection.get("tablets", []):
        row = dict(original)
        free, total = row.get("projected_free_bytes"), row.get("total_storage_bytes")
        if (
            row.get("state") != "unknown"
            and isinstance(free, int)
            and isinstance(total, int)
            and total > 0
        ):
            row["remaining_percentage"] = float(Fraction(free * 100, total))
            row["threshold_breached"] = free * 5 < total
            measured.append(row)
        else:
            row["remaining_percentage"] = None
        rows.append(row)
    lowest = min(
        measured, key=lambda row: (row["projected_free_bytes"], row["tablet_id"]), default=None
    )
    worst = min(
        measured,
        key=lambda row: (
            Fraction(row["projected_free_bytes"], row["total_storage_bytes"]),
            row["tablet_id"],
        ),
        default=None,
    )
    return {
        "tablets": rows,
        "lowest_free": lowest,
        "worst_percentage": worst,
        "low_count": sum(t["state"] == "warning" for t in projection["tablets"]),
        "unknown_count": len(rows) - len(measured),
        "different_worst_percentage": bool(
            lowest and worst and lowest["tablet_id"] != worst["tablet_id"]
        ),
    }


def fingerprint(projection):
    return hashlib.sha256(
        json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def event(review, actor, decision):
    record_event(
        action="file_library.capacity_override_" + decision,
        actor_user=actor,
        department=department_for(review.library),
        station=review.library.station,
        target_type="capacity_review",
        target_uuid=review.id,
        metadata={
            "decision": decision,
            "requested_by": str(review.requested_by_id) if review.requested_by_id else None,
            "decided_by": str(review.decided_by_id) if review.decided_by_id else None,
            "decided_at": review.decided_at.isoformat() if review.decided_at else None,
            "review_id": str(review.id),
            "publication_id": str(review.publication_id) if review.publication_id else None,
            "superseded_at": review.superseded_at.isoformat() if review.superseded_at else None,
            "projection": review.projection,
            "fingerprint": review.fingerprint,
            "expires_at": review.expires_at.isoformat() if review.expires_at else None,
        },
    )


def decide(*, actor, **kwargs):
    from apps.file_library.services import active_temporary_access

    active_temporary_access(actor)
    try:
        return _decide(actor=actor, **kwargs)
    except PermissionDenied:
        active_temporary_access(actor)
        raise


@transaction.atomic
def _decide(*, actor, review_id, confirm, reauthenticated_at, approve=True, expected_fingerprint):
    from apps.file_library.services import LibraryError, active_temporary_access

    review = CapacityReview.objects.select_related(
        "library__station__department", "library__department"
    ).get(pk=review_id)
    library = review.library
    require_department_admin(actor, department_for(library))
    now = timezone.now()
    if (
        confirm is not True
        or not reauthenticated_at
        or reauthenticated_at > now
        or now - reauthenticated_at > timedelta(seconds=settings.RECENT_REAUTH_MAX_AGE_SECONDS)
    ):
        raise PermissionDenied("Explicit confirmation and recent reauthentication are required.")
    lock_capacity(library)
    if active_temporary_access(actor):
        raise PermissionDenied("Release temporary station access before reviewing overrides.")
    # Same source lock as ingestion; never acquire a review row ahead of its library.
    from apps.file_library.publication import lock_scope

    lock_scope(library)
    library = Library.objects.select_for_update().get(pk=library.pk)
    review = CapacityReview.objects.select_for_update().get(pk=review.pk)
    require_department_admin(actor, department_for(library))
    now = timezone.now()
    if now - reauthenticated_at > timedelta(seconds=settings.RECENT_REAUTH_MAX_AGE_SECONDS):
        raise PermissionDenied("Reauthenticate before reviewing this capacity request.")
    if review.publication_snapshot is None:
        raise LibraryError("Historical upload reviews cannot authorize publication.")
    if review.decision != "pending" or review.superseded_at or review.consumed_at:
        raise LibraryError("This capacity review is no longer pending.")
    if expected_fingerprint != review.fingerprint:
        raise LibraryError("The capacity assessment changed. Reload this review.")
    from apps.file_library.publication import source_payload
    from apps.file_library.publication_capacity import assess

    scope = lock_scope(library)
    snapshot = source_payload(department=department_for(library), station=library.station)
    _admitted, current = assess(scope=scope, snapshot=snapshot, actor=review.requested_by)
    review.refresh_from_db()
    if current is None or current.pk != review.pk or review.superseded_at:
        # Return the superseded record so the transaction commits the fresh
        # assessment. Raising here would roll back the invalidation itself.
        return review
    review.decision = "approved" if approve else "rejected"
    review.decided_at, review.decided_by = now, actor
    review.save(update_fields=("decision", "decided_at", "decided_by"))
    event(review, actor, review.decision)
    # The capacity lock registered a post-commit reassessment, which resumes
    # this candidate through the existing worker after a successful approval.
    return review
