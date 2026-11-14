import time

from django.core.management.base import BaseCommand

from care.documents import scan_pending
from care.media import cleanup_sessions


class Command(BaseCommand):
    help = "Process private report scans and call cleanup; output counts only."

    def add_arguments(self, parser):
        parser.add_argument("--watch", action="store_true")

    def handle(self, *args, **options):
        try:
            while True:
                self.stdout.write(f"Reports: {scan_pending(10)}; calls: {cleanup_sessions(10)}")
                if not options["watch"]:
                    return
                time.sleep(5)
        except KeyboardInterrupt:
            self.stdout.write("Worker stopped.")
