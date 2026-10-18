from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.file_library.models import Content
from apps.file_library.services import collect_content


class Command(BaseCommand):
    help = "Collect old library PDF content only when no database references retain it."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-days", type=int, default=7)

    def handle(self, *args, **options):
        days = options["older_than_days"]
        if days < 1:
            raise CommandError("Retention must be at least one day.")
        ids = Content.objects.filter(
            created_at__lt=timezone.now() - timedelta(days=days), documents__isnull=True
        ).values_list("id", flat=True)
        count = 0
        for content_id in ids.iterator():
            try:
                count += collect_content(content_id)
            except Content.DoesNotExist:
                continue  # Another collector completed the same candidate.
        self.stdout.write(f"Collected {count} unreferenced content objects.")
