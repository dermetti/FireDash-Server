from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import AuthenticationThrottle


class Command(BaseCommand):
    help = "Remove obsolete authentication counters; preserve effective windows and lockouts."

    def handle(self, *args, **options):
        now = timezone.now()
        deleted, _ = (
            AuthenticationThrottle.objects.filter(
                updated_at__lt=now - timedelta(days=7),
                window_started_at__lte=now
                - timedelta(seconds=settings.AUTH_THROTTLE_WINDOW_SECONDS),
            )
            .filter(Q(locked_until__isnull=True) | Q(locked_until__lte=now))
            .delete()
        )
        self.stdout.write(f"Removed {deleted} obsolete authentication counter(s).")
