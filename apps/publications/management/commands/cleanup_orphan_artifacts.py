"""Reference-aware cleanup of orphaned flat publication artifacts.

Pre-fix builds promoted artifacts to a flat ``<publication-id>.bin`` path under
``PUBLICATION_ARTIFACT_ROOT`` while the database guard enforces the nested
``<department-id>/<publication-id>/artifact.bin`` layout. Those flat files are
orphans (never referenced by any database row) and can be removed safely.

Only top-level ``*.bin`` files directly under the artifact root are considered;
nested canonical artifacts are never touched. Files are still cross-checked
against referenced ``DatasetPublication.artifact_path`` values before removal.
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.publications.models import DatasetPublication


class Command(BaseCommand):
    help = "Remove orphaned flat publication artifacts not referenced by the database."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List files without deleting.")

    def handle(self, *args, **options):
        root = settings.PUBLICATION_ARTIFACT_ROOT
        dry_run = options["dry_run"]
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
