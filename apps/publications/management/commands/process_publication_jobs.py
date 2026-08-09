import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.publications.services import process_next_job, recover_stale_jobs


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
            recovered = recover_stale_jobs(
                timeout=timedelta(seconds=settings.PUBLICATION_JOB_HEARTBEAT_TIMEOUT_SECONDS),
                max_attempts=settings.PUBLICATION_JOB_MAX_ATTEMPTS,
            )
            if recovered:
                self.stdout.write(f"Recovered {recovered} stale publication job(s).")
            processed = 0
            for _ in range(settings.PUBLICATION_WORKER_BATCH_SIZE):
                job = process_next_job()
                if job is None:
                    break
                processed += 1
                self.stdout.write(f"Processed publication job {job.id}: {job.status}.")
            if not options["forever"]:
                return
            if processed == 0:
                time.sleep(poll_seconds)
