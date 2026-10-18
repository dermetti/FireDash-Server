from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError

from apps.file_library.reconciliation import reconcile_orphans


class Command(BaseCommand):
    help = "Reconcile orphaned accepted library PDFs under library locks."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-days", type=int, default=7)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        try:
            count = reconcile_orphans(
                grace=timedelta(days=options["older_than_days"]), dry_run=options["dry_run"]
            )
        except ValueError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(f"Orphan PDFs {'eligible' if options['dry_run'] else 'removed'}: {count}")
