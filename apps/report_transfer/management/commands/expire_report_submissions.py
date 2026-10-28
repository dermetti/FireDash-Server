from django.core.management.base import BaseCommand, CommandError

from apps.report_transfer.contract import TransferError
from apps.report_transfer.services import cleanup


class Command(BaseCommand):
    help = "Expire report ciphertext and remove orphan files; safe to retry."

    def handle(self, *args, **options):
        try:
            counts = cleanup()
        except (OSError, TransferError):
            raise CommandError("Report storage unavailable; retry cleanup.") from None
        self.stdout.write(" ".join(f"{key}={value}" for key, value in counts.items()))
        if counts["failed"]:
            raise CommandError("Some report files could not be removed; retry cleanup.")
