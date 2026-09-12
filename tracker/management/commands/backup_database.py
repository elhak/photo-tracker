import os
import sqlite3
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Create a consistent SQLite backup (including committed WAL data), keeping seven days."

    def add_arguments(self, parser):
        parser.add_argument("--directory", default="/var/lib/photo-tracker/backups")

    def handle(self, *args, **options):
        directory = Path(options["directory"])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / f"tracker-{timezone.now():%Y%m%d-%H%M%S}.sqlite3"
        temporary = destination.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            with sqlite3.connect(str(settings.DATABASES["default"]["NAME"])) as source:
                with sqlite3.connect(str(temporary)) as target:
                    source.backup(target)
                    result = target.execute("PRAGMA integrity_check").fetchone()[0]
                    if result != "ok":
                        raise RuntimeError("Backup integrity check failed")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        cutoff = (timezone.now() - timedelta(days=7)).timestamp()
        for old in directory.glob("tracker-*.sqlite3"):
            if old.stat().st_mtime < cutoff:
                old.unlink()
        self.stdout.write(f"Backup verified: {destination}")
