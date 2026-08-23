from django.core.management.base import BaseCommand

from apps.tablets.activity import prune_tablet_api_activity


class Command(BaseCommand):
    help = "Prune tablet API activity outside the bounded retention window."

    def handle(self, *args, **options):
        deleted = prune_tablet_api_activity()
        self.stdout.write(f"Pruned {deleted} tablet API activity record(s).")
