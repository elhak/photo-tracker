import logging
from datetime import timedelta
from django.core.management.base import BaseCommand, CommandError
from django.contrib.sessions.models import Session
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from tracker import storage
from tracker.models import DeletionJob, LoginAttempt, Photo, Tracker, UploadIntent

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Delete expired photos, abandoned uploads, and empty trackers. Safe to retry."

    def handle(self, *args, **options):
        now = timezone.now()
        with transaction.atomic():
            expired = Photo.objects.filter(Q(expires_at__lte=now) | Q(deleted_at__isnull=False))
            for key in expired.values_list("object_key", flat=True).iterator():
                DeletionJob.objects.get_or_create(object_key=key)
            abandoned = UploadIntent.objects.filter(created_at__lte=now - timedelta(hours=24), completed_at__isnull=True)
            for intent in abandoned.iterator():
                DeletionJob.objects.get_or_create(object_key=intent.staging_key)
                DeletionJob.objects.get_or_create(object_key=intent.final_key)
            abandoned.delete()
            # Completed records are idempotency receipts. Keep them for 31 days.
            UploadIntent.objects.filter(completed_at__lte=now - timedelta(days=31)).delete()
            LoginAttempt.objects.filter(started_at__lte=now - timedelta(days=1)).delete()
            Session.objects.filter(expire_date__lte=now).delete()

        failed = removed = 0
        for job in DeletionJob.objects.order_by("created_at").iterator():
            # Let the last issued POST policy expire before deleting completed staging keys.
            if job.object_key.startswith("pending/") and job.created_at > now - timedelta(minutes=5):
                if UploadIntent.objects.filter(id=job.object_key[8:-4], completed_at__isnull=False).exists():
                    continue
            # A retry may have successfully published this key after an earlier failure.
            if Photo.objects.filter(object_key=job.object_key, deleted_at__isnull=True, expires_at__gt=timezone.now()).exists():
                DeletionJob.objects.filter(pk=job.pk).delete()
                continue
            try:
                storage.delete_object(job.object_key)
            except Exception:
                failed += 1
                DeletionJob.objects.filter(pk=job.pk).update(attempts=F("attempts") + 1)
                logger.warning("Storage deletion failed; retained job %s for retry", job.pk, exc_info=True)
                continue
            with transaction.atomic():
                Photo.objects.filter(object_key=job.object_key).filter(Q(expires_at__lte=timezone.now()) | Q(deleted_at__isnull=False)).delete()
                DeletionJob.objects.filter(pk=job.pk).delete()
            removed += 1
        with transaction.atomic():
            # Rows with a failed object deletion stay hidden until physical cleanup succeeds.
            Tracker.objects.filter(photos__isnull=True).delete()
        self.stdout.write(f"Deleted {removed} objects; {failed} deletions pending retry.")
        if failed:
            raise CommandError("Some S3 deletions failed. Jobs retained for the next run.")
