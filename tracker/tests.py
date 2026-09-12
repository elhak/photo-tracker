import base64
import hashlib
import io
import json
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch
from botocore.exceptions import ClientError
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from moto import mock_aws
from PIL import Image
from . import services, storage
from .models import DeletionJob, LoginAttempt, Photo, Tracker, UploadIntent, User, current_order_month


def jpeg():
    output = io.BytesIO()
    Image.new("RGB", (48, 64), "#779d81").save(output, format="JPEG")
    return output.getvalue()


def digest(data):
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


TEST_SETTINGS = dict(
    S3_BUCKET="photo-tracker-tests", S3_REGION="us-east-1", S3_ENDPOINT_URL=None,
    SECURE_SSL_REDIRECT=False, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)


@override_settings(**TEST_SETTINGS)
class TrackerTests(TestCase):
    def setUp(self):
        self.aws = mock_aws()
        self.aws.start()
        self.addCleanup(self.aws.stop)
        self.s3 = storage.client()
        self.s3.create_bucket(Bucket=settings.S3_BUCKET)
        self.admin = User.objects.create_user("admin", password="test-password", is_staff=True)
        self.alice = User.objects.create_user("alice", password="test-password")
        self.bob = User.objects.create_user("bob", password="test-password")
        self.client.force_login(self.alice)

    def request_upload(self, order="  001-Ab  ", data=None, tracker_id=None, request_id=None):
        return self.client.post(reverse("upload-intent"), json.dumps({
            "order_id": order, "request_id": str(request_id or uuid.uuid4()),
            "checksum": digest(data if data is not None else jpeg()), "tracker_id": tracker_id,
            "byte_size": len(data if data is not None else jpeg()),
        }), content_type="application/json")

    def staged(self, order="001-Ab", actor=None, owner=None, content=None):
        actor = actor or self.alice
        content = content if content is not None else jpeg()
        intent = UploadIntent.objects.create(actor=actor, owner=owner or actor, order_id=order,
                                             checksum=digest(content), request_id=uuid.uuid4())
        self.s3.put_object(Bucket=settings.S3_BUCKET, Key=intent.staging_key, Body=content, ContentType="image/jpeg")
        return intent

    def published(self, order="001-Ab", actor=None, owner=None):
        intent = self.staged(order, actor, owner)
        return services.complete_upload(intent.pk, actor or self.alice).photo

    def test_order_month_uses_jakarta_calendar_and_year(self):
        for instant, expected in [
            (datetime(2026, 8, 31, 16, 59, tzinfo=datetime_timezone.utc), date(2026, 8, 1)),
            (datetime(2026, 8, 31, 17, 0, tzinfo=datetime_timezone.utc), date(2026, 9, 1)),
            (datetime(2026, 12, 31, 17, 0, tzinfo=datetime_timezone.utc), date(2027, 1, 1)),
        ]:
            with patch("tracker.models.timezone.now", return_value=instant):
                self.assertEqual(current_order_month(), expected)

    def test_same_order_separates_months_and_completion_keeps_intent_month(self):
        august = self.staged("141")
        august.order_month = date(2026, 8, 1)
        august.save(update_fields=["order_month"])
        september = self.staged("141")
        september.order_month = date(2026, 9, 1)
        september.save(update_fields=["order_month"])
        first = services.complete_upload(august.pk, self.alice).photo
        second = services.complete_upload(september.pk, self.alice).photo
        self.assertNotEqual(first.tracker_id, second.tracker_id)
        self.assertEqual(first.tracker.order_month, date(2026, 8, 1))
        self.assertEqual(services.complete_upload(august.pk, self.alice).photo_id, first.pk)
        self.assertEqual(Photo.objects.count(), 2)
        # Adding explicitly to the August tracker in a later month retains August.
        response = self.request_upload("141", tracker_id=first.tracker_id)
        self.assertEqual(response.status_code, 200)
        intent = UploadIntent.objects.get(pk=response.json()["id"])
        self.assertEqual(intent.order_month, date(2026, 8, 1))

    def test_rename_does_not_merge_or_rewrite_pending_in_another_month(self):
        august = Tracker.objects.create(owner=self.alice, order_id="old", order_month=date(2026, 8, 1))
        september = Tracker.objects.create(owner=self.alice, order_id="141", order_month=date(2026, 9, 1))
        pending = self.staged("old")
        pending.order_month = date(2026, 9, 1)
        pending.save(update_fields=["order_month"])
        self.client.force_login(self.admin)
        response = self.client.post(reverse("tracker-edit", args=[august.pk]), {"order_id": "141"})
        self.assertEqual(response.status_code, 302)
        august.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(august.order_id, "141")
        self.assertTrue(Tracker.objects.filter(pk=september.pk).exists())
        self.assertEqual(pending.order_id, "old")

    def test_no_tracker_before_photo_and_policy_is_restricted(self):
        response = self.request_upload()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Tracker.objects.exists())
        intent = UploadIntent.objects.get(pk=response.json()["id"])
        self.assertEqual(intent.order_id, "001-Ab")
        policy = json.loads(base64.b64decode(response.json()["upload"]["fields"]["policy"]))
        self.assertIn(["content-length-range", 1, 500000], policy["conditions"])
        self.assertIn({"key": intent.staging_key}, policy["conditions"])
        self.assertIn({"x-amz-checksum-sha256": intent.checksum}, policy["conditions"])
        self.assertIn({"Content-Type": "image/jpeg"}, policy["conditions"])

    @override_settings(S3_ENDPOINT_URL="s3.us-west-004.backblazeb2.com")
    def test_invalid_storage_endpoint_is_a_configuration_error(self):
        with self.assertRaisesMessage(RuntimeError, "S3_ENDPOINT_URL"):
            storage.client()
        with self.assertLogs("tracker.views", level="WARNING"):
            response = self.request_upload()
        self.assertEqual(response.status_code, 503)

    def test_intent_retry_returns_same_id_and_rejects_changed_content(self):
        request_id = uuid.uuid4()
        first = self.request_upload(request_id=request_id)
        second = self.request_upload(request_id=request_id)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(UploadIntent.objects.count(), 1)
        self.assertEqual(self.request_upload(request_id=request_id, data=b"changed").status_code, 400)

    def test_backblaze_uses_signed_put_with_exact_content_length(self):
        from urllib.parse import parse_qs, urlparse
        with patch("tracker.storage.is_backblaze", return_value=True), patch("tracker.storage.client", return_value=self.s3):
            response = self.request_upload()
        self.assertEqual(response.status_code, 200)
        upload = response.json()["upload"]
        self.assertEqual(upload["method"], "PUT")
        self.assertNotIn("fields", upload)
        query = parse_qs(urlparse(upload["url"]).query)
        self.assertIn("content-length", query["X-Amz-SignedHeaders"][0].split(";"))
        self.assertEqual(upload["headers"]["Content-Type"], "image/jpeg")

    def test_backblaze_rejects_missing_or_oversized_upload_length(self):
        intent = self.staged()
        with patch("tracker.storage.is_backblaze", return_value=True):
            for size in [None, 0, 500001, True]:
                with self.assertRaises(storage.InvalidPhoto):
                    storage.sign_upload(intent, byte_size=size)

    def test_backblaze_copies_verified_version_and_deletes_all_exact_key_versions(self):
        self.s3.put_bucket_versioning(Bucket=settings.S3_BUCKET, VersioningConfiguration={"Status": "Enabled"})
        intent = self.staged()
        self.s3.put_object(Bucket=settings.S3_BUCKET, Key=intent.staging_key, Body=jpeg())
        neighbor = intent.staging_key + "-other"
        self.s3.put_object(Bucket=settings.S3_BUCKET, Key=neighbor, Body=b"keep")
        with patch("tracker.storage.is_backblaze", return_value=True), patch("tracker.storage.client", return_value=self.s3):
            with patch.object(self.s3, "copy_object", wraps=self.s3.copy_object) as copy:
                services.complete_upload(intent.pk, self.alice)
                self.assertIn("VersionId", copy.call_args.kwargs["CopySource"])
            self.s3.delete_object(Bucket=settings.S3_BUCKET, Key=intent.staging_key)
            storage.delete_object(intent.staging_key)
            storage.delete_object(intent.staging_key)  # Retry is harmless.
        versions = self.s3.list_object_versions(Bucket=settings.S3_BUCKET, Prefix=intent.staging_key)
        self.assertFalse(any(v["Key"] == intent.staging_key for v in versions.get("Versions", []) + versions.get("DeleteMarkers", [])))
        self.assertEqual(self.s3.get_object(Bucket=settings.S3_BUCKET, Key=neighbor)["Body"].read(), b"keep")

    def test_cors_setup_preserves_existing_rules(self):
        original = {"ID": "existing", "AllowedOrigins": ["https://example.com"], "AllowedMethods": ["GET"]}
        self.s3.put_bucket_cors(Bucket=settings.S3_BUCKET, CORSConfiguration={"CORSRules": [original]})
        with patch("tracker.storage.is_backblaze", return_value=True), patch("tracker.storage.client", return_value=self.s3):
            call_command("setup_storage_cors", origin=["http://127.0.0.1:8081"], apply=True, stdout=io.StringIO())
        rules = self.s3.get_bucket_cors(Bucket=settings.S3_BUCKET)["CORSRules"]
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0]["ID"], "existing")
        self.assertIn("PUT", rules[1]["AllowedMethods"])

    def test_empty_order_missing_checksum_and_invalid_request_are_rejected(self):
        self.assertEqual(self.request_upload(order="   ").status_code, 400)
        self.assertEqual(self.request_upload(order="a" * 101).status_code, 400)
        for body in ["{}", "not-json", '{"order_id":"x","checksum":"!"}', "[]", "null"]:
            response = self.client.post(reverse("upload-intent"), body, content_type="application/json")
            self.assertEqual(response.status_code, 400, body)
        self.assertFalse(Tracker.objects.exists())

    def test_malformed_field_types_are_rejected_without_server_errors(self):
        base = {"order_id": "x", "request_id": str(uuid.uuid4()), "checksum": digest(jpeg())}
        for field, value in [("request_id", 123), ("order_id", []), ("checksum", {}), ("tracker_id", {}), ("tracker_id", True)]:
            response = self.client.post(reverse("upload-intent"), json.dumps({**base, field: value}), content_type="application/json")
            self.assertEqual(response.status_code, 400, field)

    def test_list_is_ordered_by_latest_activity(self):
        first = self.published(order="first")
        second = self.published(order="second")
        Tracker.objects.filter(pk=first.tracker_id).update(updated_at=timezone.now() + timedelta(seconds=1))
        response = self.client.get(reverse("tracker-list"))
        self.assertEqual([item.pk for item in response.context["page"]], [first.tracker_id, second.tracker_id])

    def test_same_owner_merges_but_case_and_other_owners_remain_separate(self):
        first = self.published()
        second = self.published()
        other_case = self.published(order="001-ab")
        other_owner = self.published(actor=self.bob)
        self.assertEqual(first.tracker_id, second.tracker_id)
        self.assertNotEqual(first.tracker_id, other_case.tracker_id)
        self.assertNotEqual(first.tracker_id, other_owner.tracker_id)

    def test_completion_is_idempotent_and_published_object_is_separate(self):
        intent = self.staged()
        url = reverse("upload-complete", args=[intent.pk])
        first = self.client.post(url)
        second = self.client.post(url)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(Photo.objects.count(), 1)
        photo = Photo.objects.get()
        self.assertEqual(photo.expires_at - photo.uploaded_at, timedelta(days=30))
        self.assertEqual(self.s3.get_object(Bucket=settings.S3_BUCKET, Key=photo.object_key)["Body"].read(), jpeg())
        self.s3.put_object(Bucket=settings.S3_BUCKET, Key=intent.staging_key, Body=b"overwritten")
        self.assertEqual(self.s3.get_object(Bucket=settings.S3_BUCKET, Key=photo.object_key)["Body"].read(), jpeg())

    def test_deleted_photo_cannot_be_resurrected_by_completion_retry(self):
        intent = self.staged()
        result = services.complete_upload(intent.pk, self.alice)
        services.remove_photo(result.photo_id)
        call_command("cleanup_photos", stdout=io.StringIO())
        self.assertEqual(self.client.post(reverse("upload-complete", args=[intent.pk])).status_code, 200)
        self.assertFalse(Photo.objects.exists())

    def test_users_cannot_view_or_append_others_data(self):
        photo = self.published(actor=self.bob)
        self.assertEqual(self.client.get(reverse("tracker-detail", args=[photo.tracker_id])).status_code, 404)
        self.assertEqual(self.client.get(reverse("photo-url", args=[photo.pk])).status_code, 404)
        self.assertEqual(self.request_upload(tracker_id=photo.tracker_id).status_code, 404)
        response = self.client.get(reverse("tracker-list"))
        self.assertNotContains(response, photo.tracker.order_id)
        intent = self.staged(actor=self.bob)
        self.assertEqual(self.client.post(reverse("upload-complete", args=[intent.pk])).status_code, 404)

    def test_admin_can_append_without_changing_owner_and_can_view_all(self):
        original = self.published(actor=self.bob)
        self.client.force_login(self.admin)
        response = self.request_upload(tracker_id=original.tracker_id)
        intent = UploadIntent.objects.get(pk=response.json()["id"])
        self.assertEqual(intent.owner_id, self.bob.pk)
        self.s3.put_object(Bucket=settings.S3_BUCKET, Key=intent.staging_key, Body=jpeg())
        photo = services.complete_upload(intent.pk, self.admin).photo
        self.assertEqual(photo.tracker.owner, self.bob)
        self.assertEqual(photo.added_by, self.admin)
        self.assertEqual(self.client.get(reverse("photo-url", args=[photo.pk])).status_code, 200)

    def test_regular_user_cannot_edit_delete_or_manage_users(self):
        photo = self.published()
        urls = [reverse("tracker-edit", args=[photo.tracker_id]), reverse("photo-delete", args=[photo.pk]),
                reverse("users"), reverse("user-create"), reverse("user-password", args=[self.bob.pk]),
                reverse("user-delete", args=[self.bob.pk])]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url).status_code, 403)

    def test_admin_rename_collision_requires_confirmation_and_merges_pending(self):
        first = self.published(order="old")
        target = self.published(order="new")
        pending = self.staged(order="old")
        self.client.force_login(self.admin)
        url = reverse("tracker-edit", args=[first.tracker_id])
        response = self.client.post(url, {"order_id": "new"})
        self.assertContains(response, "Saya setuju menggabungkan")
        self.assertEqual(Tracker.objects.count(), 2)
        self.assertEqual(self.client.post(url, {"order_id": "new", "confirm": "yes"}).status_code, 302)
        self.assertEqual(Tracker.objects.count(), 1)
        self.assertEqual(Photo.objects.filter(tracker=target.tracker).count(), 2)
        pending.refresh_from_db()
        self.assertEqual(pending.order_id, "new")
        self.assertEqual(services.complete_upload(pending.pk, self.alice).photo.tracker_id, target.tracker_id)

    def test_admin_rename_never_merges_different_owners(self):
        photo = self.published(order="old")
        self.published(order="new", actor=self.bob)
        services.rename_tracker(photo.tracker_id, "new")
        self.assertEqual(Tracker.objects.filter(order_id="new").count(), 2)

    def test_deleted_user_access_and_sessions_are_revoked_but_photos_remain(self):
        photo = self.published()
        services.disable_user(self.alice.pk)
        self.assertEqual(self.client.get(reverse("tracker-list")).status_code, 302)
        self.assertEqual(self.client.get(reverse("photo-url", args=[photo.pk])).status_code, 401)
        self.assertFalse(self.client.login(username="alice", password="test-password"))
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("tracker-detail", args=[photo.tracker_id])).status_code, 200)
        self.assertTrue(Photo.objects.filter(pk=photo.pk).exists())

    def test_last_active_admin_cannot_be_deleted(self):
        with self.assertRaises(services.ActionError):
            services.disable_user(self.admin.pk)
        another = User.objects.create_user("second-admin", is_staff=True)
        services.disable_user(self.admin.pk)
        with self.assertRaises(services.ActionError):
            services.disable_user(another.pk)

    def test_password_reset_invalidates_old_sessions(self):
        other_client = Client()
        other_client.force_login(self.alice)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("user-password", args=[self.alice.pk]), {
            "new_password1": "New-secure-pass-782", "new_password2": "New-secure-pass-782",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(other_client.get(reverse("tracker-list")).status_code, 302)

    def test_admin_creates_accounts_with_selected_role(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("user-create"), {"username": "operator", "first_name": "Operator",
            "role": "user", "password1": "Secure-new-pass-782", "password2": "Secure-new-pass-782"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.get(username="operator").is_staff)

    def test_simple_account_setup_and_password_policy(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("user-create"))
        self.assertNotContains(response, 'name="email"')
        self.assertNotContains(response, 'name="first_name"')
        self.assertEqual(User.REQUIRED_FIELDS, [])
        response = self.client.post(reverse("user-create"), {
            "username": "simple", "role": "user", "password1": "123456", "password2": "123456",
        })
        self.assertEqual(response.status_code, 302)
        account = User.objects.get(username="simple")
        self.assertTrue(account.check_password("123456"))
        response = self.client.post(reverse("user-password", args=[account.pk]), {
            "new_password1": "simple", "new_password2": "simple",
        })
        self.assertEqual(response.status_code, 302)
        account.refresh_from_db()
        self.assertTrue(account.check_password("simple"))
        response = self.client.post(reverse("user-password", args=[account.pk]), {
            "new_password1": "12345", "new_password2": "12345",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("new_password2", response.context["form"].errors)
        account.refresh_from_db()
        self.assertTrue(account.check_password("simple"))

    def test_invalid_oversized_and_checksum_mismatched_images_are_not_published(self):
        for data in [b"not a jpeg", b"x" * 500001]:
            intent = self.staged(content=data)
            response = self.client.post(reverse("upload-complete", args=[intent.pk]))
            self.assertEqual(response.status_code, 400)
        intent = self.staged()
        self.s3.put_object(Bucket=settings.S3_BUCKET, Key=intent.staging_key, Body=b"changed")
        self.assertEqual(self.client.post(reverse("upload-complete", args=[intent.pk])).status_code, 400)
        self.assertFalse(Photo.objects.exists())
        self.assertFalse(Tracker.objects.exists())

    def test_partial_success_and_failed_completion_can_be_retried(self):
        good = self.staged()
        bad = self.staged()
        self.assertEqual(self.client.post(reverse("upload-complete", args=[good.pk])).status_code, 200)
        with patch("tracker.storage.verify_and_copy", side_effect=ClientError({"Error": {"Code": "ServiceUnavailable"}}, "GetObject")):
            self.assertEqual(self.client.post(reverse("upload-complete", args=[bad.pk])).status_code, 503)
        self.assertEqual(Photo.objects.count(), 1)
        self.assertEqual(self.client.post(reverse("upload-complete", args=[bad.pk])).status_code, 200)
        self.assertEqual(Photo.objects.count(), 2)
        self.assertEqual(Tracker.objects.count(), 1)

    def test_deletion_during_verification_cannot_publish(self):
        intent = self.staged()
        original_verify = storage.verify_and_copy
        def delete_then_verify(value):
            result = original_verify(value)
            services.disable_user(self.alice.pk)
            return result
        with patch("tracker.storage.verify_and_copy", side_effect=delete_then_verify):
            with self.assertRaises(services.ActionError):
                services.complete_upload(intent.pk, self.alice)
        self.assertFalse(Photo.objects.exists())
        self.assertTrue(DeletionJob.objects.filter(object_key=intent.final_key).exists())

    def test_expiration_is_independent_and_read_url_is_capped(self):
        old = self.published()
        Photo.objects.filter(pk=old.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        recent = self.published()
        response = self.client.get(reverse("tracker-detail", args=[old.tracker_id]))
        self.assertEqual(len(response.context["page"]), 1)
        self.assertEqual(self.client.get(reverse("photo-url", args=[old.pk])).status_code, 404)
        recent.expires_at = timezone.now() + timedelta(seconds=20)
        recent.save()
        response = self.client.get(reverse("photo-url", args=[recent.pk]))
        from urllib.parse import parse_qs, urlparse
        seconds = int(parse_qs(urlparse(response.json()["url"]).query)["X-Amz-Expires"][0])
        self.assertLessEqual(seconds, 20)
        self.assertEqual(response["Cache-Control"], "private, no-store, max-age=0")

    def test_cleanup_expired_photos_empty_orders_and_abandoned_uploads(self):
        photo = self.published()
        Photo.objects.filter(pk=photo.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        abandoned = self.staged(order="never-published")
        UploadIntent.objects.filter(pk=abandoned.pk).update(created_at=timezone.now() - timedelta(hours=25))
        call_command("cleanup_photos", stdout=io.StringIO())
        self.assertFalse(Photo.objects.exists())
        self.assertFalse(Tracker.objects.exists())
        self.assertFalse(UploadIntent.objects.filter(pk=abandoned.pk).exists())
        with self.assertRaises(ClientError):
            self.s3.head_object(Bucket=settings.S3_BUCKET, Key=abandoned.staging_key)

    def test_failed_cleanup_is_hidden_and_retried(self):
        photo = self.published()
        services.remove_photo(photo.pk)
        with patch("tracker.storage.delete_object", side_effect=RuntimeError("offline")):
            with self.assertRaises(CommandError):
                call_command("cleanup_photos", stdout=io.StringIO())
        self.assertTrue(DeletionJob.objects.filter(object_key=photo.object_key, attempts=1).exists())
        self.assertEqual(self.client.get(reverse("photo-url", args=[photo.pk])).status_code, 404)
        call_command("cleanup_photos", stdout=io.StringIO())
        self.assertFalse(Photo.objects.exists())
        self.assertFalse(Tracker.objects.exists())

    def test_abandoned_completion_is_rejected(self):
        intent = self.staged()
        UploadIntent.objects.filter(pk=intent.pk).update(created_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(self.client.post(reverse("upload-complete", args=[intent.pk])).status_code, 400)

    def test_csrf_and_method_guards(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.alice)
        self.assertEqual(csrf_client.post(reverse("upload-intent"), "{}", content_type="application/json").status_code, 403)
        self.assertEqual(self.client.get(reverse("upload-intent")).status_code, 405)
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)

    def test_login_throttle_and_valid_login(self):
        self.client.logout()
        for _ in range(10):
            response = self.client.post(reverse("login"), {"username": "alice", "password": "wrong"})
            self.assertEqual(response.status_code, 400)
        response = self.client.post(reverse("login"), {"username": "alice", "password": "test-password"})
        self.assertEqual(response.status_code, 429)
        LoginAttempt.objects.update(started_at=timezone.now() - timedelta(minutes=16))
        self.assertEqual(self.client.post(reverse("login"), {"username": "alice", "password": "test-password"}).status_code, 302)

    def test_pages_render_in_indonesian_without_file_inputs(self):
        photo = self.published()
        self.client.force_login(self.admin)
        urls = [reverse("tracker-list"), reverse("capture"), reverse("tracker-detail", args=[photo.tracker_id]),
                reverse("users"), reverse("user-create"), reverse("user-password", args=[self.alice.pk]),
                reverse("tracker-edit", args=[photo.tracker_id]), reverse("photo-delete", args=[photo.pk])]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertContains(response, '<html lang="id">')
            self.assertNotContains(response, 'type="file"')


@override_settings(**TEST_SETTINGS)
class ConcurrentSubmissionTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user("concurrent", password="test")

    def test_concurrent_photos_merge_into_one_tracker(self):
        intents = [UploadIntent.objects.create(actor=self.user, owner=self.user, order_id="0001", checksum=digest(jpeg()), request_id=uuid.uuid4()) for _ in range(4)]
        barrier = threading.Barrier(4)
        def verified(intent):
            barrier.wait(timeout=10)
            return 100, timezone.now()
        def complete(pk):
            close_old_connections()
            try:
                return services.complete_upload(pk, User.objects.get(pk=self.user.pk)).photo.tracker_id
            finally:
                close_old_connections()
        with patch("tracker.storage.verify_and_copy", side_effect=verified):
            with ThreadPoolExecutor(max_workers=4) as pool:
                tracker_ids = list(pool.map(complete, [intent.pk for intent in intents]))
        self.assertEqual(len(set(tracker_ids)), 1)
        self.assertEqual(Tracker.objects.count(), 1)
        self.assertEqual(Photo.objects.count(), 4)

    def test_concurrent_completion_retry_does_not_duplicate_photo(self):
        intent = UploadIntent.objects.create(actor=self.user, owner=self.user, order_id="0001", checksum=digest(jpeg()), request_id=uuid.uuid4())
        barrier = threading.Barrier(2)
        def verified(value):
            barrier.wait(timeout=10)
            return 100, timezone.now()
        def complete(_):
            close_old_connections()
            try:
                return services.complete_upload(intent.pk, User.objects.get(pk=self.user.pk)).photo_id
            finally:
                close_old_connections()
        with patch("tracker.storage.verify_and_copy", side_effect=verified):
            with ThreadPoolExecutor(max_workers=2) as pool:
                photos = list(pool.map(complete, range(2)))
        self.assertEqual(photos[0], photos[1])
        self.assertEqual(Photo.objects.count(), 1)

    def test_backup_has_committed_data_and_valid_integrity(self):
        import sqlite3
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            call_command("backup_database", directory=directory, stdout=io.StringIO())
            backup = next(Path(directory).glob("*.sqlite3"))
            with sqlite3.connect(backup) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("SELECT username FROM tracker_user").fetchone()[0], "concurrent")
