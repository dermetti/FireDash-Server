from django.core.management.base import BaseCommand

from apps.tablets.services import mark_stale_installations


class Command(BaseCommand):
    help = "Mark active app installations with expired authorization leases as stale."

    def handle(self, *args, **options):
        count = mark_stale_installations()
        self.stdout.write(f"Marked {count} app installation(s) stale.")
