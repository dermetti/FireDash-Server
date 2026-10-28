"""Publication artifact cleanup, including retry of terminal lifecycle cleanup.

Pre-fix builds promoted artifacts to a flat ``<publication-id>.bin`` path under
``PUBLICATION_ARTIFACT_ROOT`` while the database guard enforces the nested
``<department-id>/<publication-id>/artifact.bin`` layout. Those flat files are
orphans (never referenced by any database row) and can be removed safely.

Only top-level ``*.bin`` files directly under the artifact root are considered
by the legacy-orphan pass; nested canonical artifacts remain protected by the
database reference check. Terminal publication artifacts are removed first by
the lifecycle-aware cleanup helper, so a post-commit filesystem failure can be
retried by the existing daily publication-maintenance service.
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.publications.artifacts import ArtifactError, cleanup_stale_artifacts, remove_artifact_path
from apps.publications.cleanup_results import CleanupResult
from apps.publications.models import DatasetPublication, DocumentArtifactCleanup
from apps.publications.retention import run_publication_retention


class Command(BaseCommand):
    help = "Run bounded publication retention and retry terminal artifact cleanup."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List files without deleting.")
        parser.add_argument(
            "--batch-size",
            type=int,
            help="Maximum publication-retention candidates to process.",
        )

    def handle(self, *args, **options):
        root = settings.PUBLICATION_ARTIFACT_ROOT
        dry_run = options["dry_run"]
        cleanup = None if dry_run else CleanupResult()
        retention = run_publication_retention(
            batch_size=options["batch_size"], dry_run=dry_run, cleanup_result=cleanup
        )
        if cleanup is not None:
            cleanup.merge(cleanup_stale_artifacts())
        referenced = set(
            DatasetPublication.objects.exclude(artifact_path="").values_list(
                "artifact_path", flat=True
            )
        )
        removed = 0
        if root.exists():
            for path in root.iterdir():
                if not path.is_file() or not path.name.endswith(".bin"):
                    continue
                if path.name in referenced:
                    self.stderr.write(f"Keeping referenced top-level file {path} (unexpected).")
                    continue
                if dry_run:
                    removed += 1
                    self.stdout.write(f"Would remove orphan {path}")
                    continue
                try:
                    outcome = remove_artifact_path(path.name)
                except (ArtifactError, OSError) as error:
                    cleanup.cleanup_failed += 1
                    cleanup.cleanup_deferred += 1
                    self.stderr.write(f"Orphan cleanup deferred for {path.name}: {error}")
                    continue
                cleanup.record_file(outcome)
                if outcome == "deleted":
                    removed += 1
                    self.stdout.write(f"Removed orphan {path}")
        verb = "Would remove" if dry_run else "Removed"
        self.stdout.write(f"{verb} {removed} orphan artifact(s).")
        if cleanup is not None:
            self.stdout.write("Artifact cleanup (publication and document files):")
            for name, value in vars(cleanup).items():
                self.stdout.write(f"  {name}: {value}")
            self.stdout.write(
                f"  document_cleanup_pending_records: {DocumentArtifactCleanup.objects.count()}"
            )
        self.stdout.write(
            "Publication retention: "
            f"{retention['obsoleted']} obsoleted, "
            f"{retention['snapshots_purged']} snapshots purged, "
            f"{retention['skipped']} skipped "
            f"({retention['considered']} considered)."
        )
