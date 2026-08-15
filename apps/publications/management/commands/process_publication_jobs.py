import time

from django.core.management.base import BaseCommand

from apps.publications.work_cycle import (
    process_build_cycle,
    process_delivery_cycle,
    process_work_cycle,
)


class Command(BaseCommand):
    help = "Process queued publication jobs."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--delivery", action="store_true", help="Process grants and manifests only."
        )
        mode.add_argument("--build", action="store_true", help="Process dataset builds only.")
        parser.add_argument("--forever", action="store_true", help="Keep polling for new jobs.")
        parser.add_argument("--poll-seconds", type=float, default=5.0)

    def handle(self, *args, **options):
        poll_seconds = options["poll_seconds"]
        if poll_seconds <= 0:
            raise ValueError("--poll-seconds must be positive.")
        while True:
            processed = 0
            if options["delivery"]:
                delivery = process_delivery_cycle()
                processed = delivery.processed
                if processed:
                    self.stdout.write(
                        "Publication delivery cycle: "
                        f"grants={delivery.key_grants} manifests={delivery.manifests} "
                        f"elapsed={delivery.elapsed_seconds:.3f}s"
                    )
            elif options["build"]:
                build = process_build_cycle()
                processed = build.processed
                self.stdout.write(
                    "Publication build cycle: "
                    f"builds={build.dataset_builds} recovered={build.recovered} "
                    f"elapsed={build.elapsed_seconds:.3f}s"
                )
            else:
                generic = process_work_cycle()
                processed = generic.dataset_builds + generic.key_grants + generic.manifests
                self.stdout.write(
                    "Publication work cycle: "
                    f"builds={generic.dataset_builds} grants={generic.key_grants} "
                    f"manifests={generic.manifests} recovered={generic.recovered} "
                    f"artifacts_cleaned={generic.artifacts_cleaned} "
                    f"elapsed={generic.elapsed_seconds:.3f}s"
                )
            if not options["forever"]:
                return
            if processed == 0:
                time.sleep(poll_seconds)
