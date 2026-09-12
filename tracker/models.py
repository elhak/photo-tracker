import uuid
from zoneinfo import ZoneInfo
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


def current_order_month():
    return timezone.localtime(timezone.now(), ZoneInfo("Asia/Jakarta")).date().replace(day=1)


class User(AbstractUser):
    # Admin setup needs only a username and password, without an email prompt.
    REQUIRED_FIELDS = []
    deleted_at = models.DateTimeField(null=True, blank=True)


class Tracker(models.Model):
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="trackers")
    order_id = models.CharField(max_length=100)
    order_month = models.DateField(default=current_order_month)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "order_id", "order_month"], name="unique_owner_order_month")]
        ordering = ["-updated_at", "-pk"]


class Photo(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tracker = models.ForeignKey(Tracker, on_delete=models.CASCADE, related_name="photos")
    added_by = models.ForeignKey(User, on_delete=models.PROTECT)
    object_key = models.CharField(max_length=200, unique=True)
    byte_size = models.PositiveIntegerField()
    uploaded_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)


class UploadIntent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_id = models.UUIDField()
    actor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="uploads")
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="owned_uploads")
    order_id = models.CharField(max_length=100)
    order_month = models.DateField(default=current_order_month)
    checksum = models.CharField(max_length=44)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    photo = models.ForeignKey(Photo, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["actor", "request_id"], name="unique_upload_request")]

    @property
    def staging_key(self):
        return f"pending/{self.id}.jpg"

    @property
    def final_key(self):
        return f"photos/{self.id}.jpg"


class DeletionJob(models.Model):
    object_key = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveIntegerField(default=0)


class LoginAttempt(models.Model):
    key = models.CharField(max_length=64, unique=True)
    failures = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(default=timezone.now)
