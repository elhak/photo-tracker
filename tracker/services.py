from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .models import DeletionJob, Photo, Tracker, UploadIntent, User
from . import storage


class ActionError(ValueError):
    pass


def check_intent_access(intent, actor):
    # Re-read the account so deletion while an S3 request is running cannot publish a photo.
    actor = User.objects.get(pk=actor.pk)
    if not actor.is_active or actor.pk != intent.actor_id:
        raise ActionError("Anda tidak memiliki akses ke unggahan ini.")
    if actor.pk != intent.owner_id and not actor.is_staff:
        raise ActionError("Anda tidak memiliki akses ke tracker ini.")
    if intent.created_at <= timezone.now() - timedelta(hours=24):
        raise ActionError("Sesi unggah sudah berakhir. Ambil foto kembali.")


def complete_upload(intent_id, actor):
    intent = UploadIntent.objects.get(pk=intent_id)
    check_intent_access(intent, actor)
    if intent.completed_at:
        return intent
    size, uploaded_at = storage.verify_and_copy(intent)
    try:
        with transaction.atomic():
            intent = UploadIntent.objects.get(pk=intent_id)
            check_intent_access(intent, actor)
            if intent.completed_at:
                return intent
            tracker, _ = Tracker.objects.get_or_create(owner_id=intent.owner_id, order_id=intent.order_id)
            photo = Photo.objects.create(
                id=intent.id, tracker=tracker, added_by=actor, object_key=intent.final_key,
                byte_size=size, uploaded_at=uploaded_at, expires_at=uploaded_at + timedelta(days=30),
            )
            tracker.updated_at = timezone.now()
            tracker.save(update_fields=["updated_at"])
            intent.photo = photo
            intent.completed_at = timezone.now()
            intent.save(update_fields=["photo", "completed_at"])
            DeletionJob.objects.get_or_create(object_key=intent.staging_key)
    except (ActionError, UploadIntent.DoesNotExist):
        # Cleanup or account deletion may have raced with the conditional S3 copy.
        DeletionJob.objects.get_or_create(object_key=intent.final_key)
        DeletionJob.objects.get_or_create(object_key=intent.staging_key)
        raise ActionError("Sesi unggah telah berakhir atau akses Anda telah dihapus.")
    return intent


@transaction.atomic
def rename_tracker(tracker_id, order_id, confirmed=False):
    tracker = Tracker.objects.get(pk=tracker_id)
    target = Tracker.objects.filter(owner=tracker.owner, order_id=order_id).exclude(pk=tracker.pk).first()
    if target and not confirmed:
        raise ActionError("ID order sudah ada. Konfirmasi untuk menggabungkan foto.")
    UploadIntent.objects.filter(owner=tracker.owner, order_id=tracker.order_id, completed_at__isnull=True).update(order_id=order_id)
    if target:
        Photo.objects.filter(tracker=tracker).update(tracker=target)
        target.updated_at = timezone.now()
        target.save(update_fields=["updated_at"])
        tracker.delete()
        return target
    tracker.order_id = order_id
    tracker.updated_at = timezone.now()
    tracker.save(update_fields=["order_id", "updated_at"])
    return tracker


@transaction.atomic
def remove_photo(photo_id):
    photo = Photo.objects.get(pk=photo_id)
    photo.deleted_at = timezone.now()
    photo.save(update_fields=["deleted_at"])
    DeletionJob.objects.get_or_create(object_key=photo.object_key)


@transaction.atomic
def disable_user(user_id):
    user = User.objects.get(pk=user_id)
    if user.is_staff and user.is_active and User.objects.filter(is_staff=True, is_active=True).count() <= 1:
        raise ActionError("Admin aktif terakhir tidak dapat dihapus.")
    user.is_active = False
    user.deleted_at = timezone.now()
    user.set_unusable_password()  # Changes the session authentication hash, permanently invalidating old sessions.
    user.save(update_fields=["is_active", "deleted_at", "password"])
