from django.core.management.base import BaseCommand

from apps.ingestion.maintenance import cleanup_expired_staging


class Command(BaseCommand):
    help = "Remove expired private canonical-import staging files."

    def handle(self, *args, **options):
        self.stdout.write(f"import_batches_cleaned={cleanup_expired_staging()}")
