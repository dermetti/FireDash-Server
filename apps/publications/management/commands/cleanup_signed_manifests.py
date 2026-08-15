from django.conf import settings
from django.core.management.base import BaseCommand

from apps.publications.manifests import cleanup_signed_manifests


class Command(BaseCommand):
    help = "Remove retained terminal signed manifests without credentials."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--batch-size", type=int, default=500)

    def handle(self, *args, **options):
        count = cleanup_signed_manifests(
            retention_days=settings.SIGNED_MANIFEST_RETENTION_DAYS,
            batch_size=options["batch_size"],
            dry_run=options["dry_run"],
        )
        self.stdout.write(
            f"{'Would remove' if options['dry_run'] else 'Removed'} {count} signed manifest(s)."
        )
