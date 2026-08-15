import time

from django.core.management.base import BaseCommand

from apps.publications.work_cycle import process_work_cycle


class Command(BaseCommand):
    help = "Process queued publication jobs."

    def add_arguments(self, parser):
        parser.add_argument("--forever", action="store_true", help="Keep polling for new jobs.")
        parser.add_argument("--poll-seconds", type=float, default=5.0)

    def handle(self, *args, **options):
        poll_seconds = options["poll_seconds"]
        if poll_seconds <= 0:
            raise ValueError("--poll-seconds must be positive.")
        while True:
            result = process_work_cycle()
            processed = result.dataset_builds + result.key_grants + result.manifests
            self.stdout.write(
                "Publication work cycle: "
                f"builds={result.dataset_builds} grants={result.key_grants} "
                f"manifests={result.manifests} recovered={result.recovered} "
                f"artifacts_cleaned={result.artifacts_cleaned} "
                f"elapsed={result.elapsed_seconds:.3f}s"
            )
            if not options["forever"]:
                return
            if processed == 0:
                time.sleep(poll_seconds)
