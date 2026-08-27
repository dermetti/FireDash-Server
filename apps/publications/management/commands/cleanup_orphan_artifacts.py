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

from apps.publications.artifacts import cleanup_stale_artifacts
from apps.publications.models import DatasetPublication
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
        retention = run_publication_retention(batch_size=options["batch_size"], dry_run=dry_run)
        terminal_removed = 0 if dry_run else cleanup_stale_artifacts()
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
                removed += 1
                if dry_run:
                    self.stdout.write(f"Would remove orphan {path}")
                    continue
                path.unlink(missing_ok=True)
                self.stdout.write(f"Removed orphan {path}")
        verb = "Would remove" if dry_run else "Removed"
        self.stdout.write(f"{verb} {removed} orphan artifact(s).")
        if not dry_run:
            self.stdout.write(f"Removed {terminal_removed} terminal publication artifact(s).")
        self.stdout.write(
            "Publication retention: "
            f"{retention['obsoleted']} obsoleted, "
            f"{retention['snapshots_purged']} snapshots purged, "
            f"{retention['skipped']} skipped "
            f"({retention['considered']} considered)."
        )
