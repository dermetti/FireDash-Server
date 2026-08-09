from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.assignments.models import PersonnelStationAssignment
from apps.assignments.services import end_temporary_assignment


class Command(BaseCommand):
    help = "End temporary personnel assignments whose validity window has expired."

    def handle(self, *args, **options):
        expired_assignments = PersonnelStationAssignment.objects.filter(
            assignment_type=PersonnelStationAssignment.AssignmentType.TEMPORARY,
            valid_until__lte=timezone.now(),
            ended_at__isnull=True,
        ).order_by("valid_until")[: settings.TEMPORARY_ASSIGNMENT_EXPIRY_BATCH_SIZE]
        expired_count = 0
        for assignment in expired_assignments:
            end_temporary_assignment(assignment=assignment)
            expired_count += 1
        self.stdout.write(f"Expired {expired_count} temporary assignment(s).")
