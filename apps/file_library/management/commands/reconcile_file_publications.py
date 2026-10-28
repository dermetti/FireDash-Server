from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from apps.file_library.locking import FILE_DATASETS
from apps.file_library.publication_reconciliation import reconcile_file_scope
from apps.organizations.models import Department
from apps.publications.models import DatasetScopeState


class Command(BaseCommand):
    help = "Reconcile logical file-library reverts. Read-only dry run unless --apply is given."

    def add_arguments(self, parser):
        parser.add_argument("--department", required=True, type=UUID, help="Exact department UUID")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        department_id = options["department"]
        if not Department.objects.filter(pk=department_id).exists():
            raise CommandError("Department does not exist.")
        self.stdout.write("APPLY" if options["apply"] else "DRY RUN (no writes)")
        scopes = DatasetScopeState.objects.filter(
            department_id=department_id, dataset_type_code__in=FILE_DATASETS
        ).order_by("id")
        for scope_id in scopes.values_list("id", flat=True).iterator():
            result = reconcile_file_scope(
                scope_id=scope_id, department_id=department_id, apply=options["apply"]
            )
            self.stdout.write(f"{scope_id}: {result}")
