"""Independent read-only policy inventory; never calls retention or cleanup."""

import json

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef

from apps.publications.models import DatasetPublication, DatasetScopeState, PublicationJob
from apps.publications.restore import iter_predecessor_assessments


def retention_audit():
    limit = settings.PUBLICATION_RETAINED_ROLLBACK_PREDECESSORS
    jobs = PublicationJob.objects.filter(
        scope_state_id=OuterRef("pk"), status__in=("PENDING", "RUNNING")
    )
    attempts = DatasetPublication.objects.filter(
        scope_state_id=OuterRef("pk"), status__in=("STAGED", "BUILDING", "READY_FOR_REVIEW")
    )
    scopes = (
        DatasetScopeState.objects.select_related("current_published_publication")
        .only(
            "id",
            "dataset_type_code",
            "scope_type",
            "department_id",
            "station_id",
            "current_published_publication__id",
            "current_published_publication__version_number",
        )
        .annotate(active_job=Exists(jobs), active_attempt=Exists(attempts))
        .order_by("dataset_type_code", "pk")
    )
    for scope in scopes.iterator(chunk_size=100):
        current = scope.current_published_publication
        candidates = []
        ready, restorable = [], []
        for publication, reason in iter_predecessor_assessments(scope=scope):
            if (
                publication.artifact_status == "READY"
                and publication.artifact_ready
                and len(ready) < limit
            ):
                ready.append(publication.version_number)
            if not reason and len(restorable) < limit:
                restorable.append(publication.version_number)
            candidates.append(
                {
                    "publication_id": str(publication.id),
                    "version": publication.version_number,
                    "restorable": not bool(reason),
                    "reason": reason,
                    "is_current": bool(current and current.id == publication.id),
                }
            )
        current_version = current.version_number if current else None
        newly_unprotected = [v for v in ready if v not in restorable and v != current_version]
        proposed_retire = [
            c["version"]
            for c in candidates
            if c["version"] not in restorable and not c["is_current"]
        ]
        yield {
            "dataset_type": scope.dataset_type_code,
            "scope_id": str(scope.id),
            "scope_type": scope.scope_type,
            "department_id": str(scope.department_id) if scope.department_id else None,
            "station_id": str(scope.station_id) if scope.station_id else None,
            "current_version": current_version,
            "current_policy_protected": ready,
            "proposed_policy_protected": restorable,
            "newly_unprotected": newly_unprotected,
            "proposed_potential_retirements": proposed_retire,
            "protected_count": len(restorable),
            "potential_retirement_count": len(proposed_retire),
            "cleanup_blocked_by_active_job": scope.active_job,
            "active_attempt_protected": scope.active_attempt,
            "current_publication_protected": current is not None,
            "policies_disagree": ready != restorable,
            "candidates": candidates,
        }


class Command(BaseCommand):
    help = "Read-only comparison of current and proposed retention protection; no changes executed."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", dest="json_output")

    def handle(self, *args, **options):
        if options["json_output"]:
            self.stdout.write(
                json.dumps(
                    {
                        "dry_run": True,
                        "restore_aware_enabled": (
                            settings.PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED
                        ),
                        "predecessor_limit": settings.PUBLICATION_RETAINED_ROLLBACK_PREDECESSORS,
                        "scopes": list(retention_audit()),
                    },
                    sort_keys=True,
                )
            )
            return
        self.stdout.write(
            "Restore-aware flag enabled: "
            f"{settings.PUBLICATION_RETENTION_RESTORE_AWARE_ENABLED}; "
            f"predecessor limit: {settings.PUBLICATION_RETAINED_ROLLBACK_PREDECESSORS}"
        )
        count = differences = 0
        for row in retention_audit():
            count += 1
            differences += row["policies_disagree"]
            self.stdout.write(
                f"Dataset: {row['dataset_type']} | {row['scope_type']} {row['scope_id']}"
            )
            self.stdout.write(f"Current: {row['current_version']}")
            self.stdout.write(f"CURRENT POLICY: {row['current_policy_protected']}")
            self.stdout.write(f"PROPOSED POLICY: {row['proposed_policy_protected']}")
            for candidate in row["candidates"]:
                state = (
                    "RESTORABLE"
                    if candidate["restorable"]
                    else "NOT_RESTORABLE ? " + candidate["reason"]
                )
                self.stdout.write(f"  v{candidate['version']} {state}")
            self.stdout.write(
                f"PROPOSED CHANGES ? NOT EXECUTED: lose protection {row['newly_unprotected']}"
            )
            self.stdout.write(
                f"  Protected: {row['protected_count']}; "
                f"potentially retired: {row['potential_retirement_count']}"
            )
            self.stdout.write(
                f"  Current protected: {row['current_publication_protected']}; "
                f"cleanup blocked by active job: {row['cleanup_blocked_by_active_job']}; "
                f"active attempts protected: {row['active_attempt_protected']}"
            )
        self.stdout.write(
            f"{count} scopes; {differences} policy differences. DRY RUN ? No changes executed."
        )
