"""Opt-in browser tests: uv run playwright install chromium; manage.py test tracker.browser_tests.

The browser uses Chromium's simulated camera. S3 requests are routed to Moto;
this exercises the full app and compression, but not AWS's policy enforcement.
"""
import email.policy
from unittest.mock import patch
from email.parser import BytesParser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlparse
from django.conf import settings
from django.db import close_old_connections
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from moto import mock_aws
from playwright.sync_api import expect, sync_playwright
from .models import Photo, Tracker, User
from . import storage


@override_settings(
    S3_BUCKET="photo-tracker-browser", S3_REGION="us-east-1", S3_ENDPOINT_URL=None,
    SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class CameraBrowserTests(StaticLiveServerTestCase):
    def setUp(self):
        self.aws = mock_aws()
        self.aws.start()
        self.addCleanup(self.aws.stop)
        self.s3 = storage.client()
        self.s3.create_bucket(Bucket=settings.S3_BUCKET)
        User.objects.create_user("browser-admin", password="Browser-test-294!", is_staff=True, first_name="Admin")
        for username in ("browser-alice", "browser-bob"):
            User.objects.create_user(username, password="Browser-test-294!")
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        self.browser = self.pw.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000}, permissions=["camera"])
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.posts = 0
        self.fail_second_post = False
        self.context.route("https://*.amazonaws.com/**", self.s3_route)
        self.output = Path(settings.BASE_DIR) / "test-results"
        self.output.mkdir(exist_ok=True)

    def s3_route(self, route):
        request = route.request
        if request.method in ("POST", "PUT"):
            self.posts += 1
            if self.fail_second_post and self.posts == 2:
                route.abort("internetdisconnected")
                return
            if request.method == "PUT":
                data = request.post_data_buffer
                key = unquote(urlparse(request.url).path.lstrip("/"))
            else:
                mime = f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode() + request.post_data_buffer
                message = BytesParser(policy=email.policy.default).parsebytes(mime)
                fields = {}
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    fields[name] = part.get_payload(decode=True)
                data = fields["file"]
                key = fields["key"].decode()
            self.assertLessEqual(len(data), 500000)
            self.assertGreater(len(data), 0)
            self.s3.put_object(Bucket=settings.S3_BUCKET, Key=key, Body=data,
                               ContentType="image/jpeg", CacheControl="private, no-store, max-age=0")
            route.fulfill(status=204, headers={"Access-Control-Allow-Origin": self.live_server_url})
        else:
            key = unquote(urlparse(request.url).path.lstrip("/"))
            data = self.s3.get_object(Bucket=settings.S3_BUCKET, Key=key)["Body"].read()
            route.fulfill(status=200, content_type="image/jpeg", body=data, headers={"Cache-Control": "no-store"})

    def login(self, username="browser-admin"):
        self.page.goto(self.live_server_url + "/masuk/")
        self.page.get_by_label("Nama pengguna").fill(username)
        self.page.get_by_label("Kata sandi", exact=True).fill("Browser-test-294!")
        self.page.get_by_role("button", name="Masuk").click()
        expect(self.page.get_by_role("heading", name="Daftar Pesanan.")).to_be_visible()

    def database_summary(self):
        # Playwright's sync facade runs an event loop; query Django in a worker.
        def read():
            close_old_connections()
            try:
                return Photo.objects.count(), Tracker.objects.count(), list(Photo.objects.values_list("byte_size", flat=True))
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(read).result()

    def test_capture_compression_partial_failure_retry_and_gallery(self):
        self.login()
        self.page.screenshot(path=str(self.output / "desktop-empty.png"), full_page=True)
        self.page.get_by_role("link", name="Tambah Pesanan").first.click()
        expect(self.page.locator("#save-photos")).to_be_disabled()
        self.page.locator("#order-id").fill("00124")
        self.page.get_by_role("button", name="Buka kamera").click()
        expect(self.page.locator("#camera")).to_be_visible()
        expect(self.page.locator("#camera")).to_have_js_property("readyState", 4)
        for count in [1, 2]:
            self.page.locator("#take-photo").click()
            expect(self.page.locator("#photo-count")).to_have_text(str(count))
        self.page.screenshot(path=str(self.output / "desktop-capture.png"), full_page=True)
        self.fail_second_post = True
        self.page.locator("#save-photos").click()
        expect(self.page.locator("#save-photos")).to_have_text("Coba simpan lagi", timeout=20000)
        expect(self.page.locator(".queued-photo.saved")).to_have_count(1)
        self.assertEqual(self.database_summary()[0], 1)
        self.page.locator("#save-photos").click()
        expect(self.page.locator(".queued-photo.saved")).to_have_count(2, timeout=20000)
        photos, trackers, sizes = self.database_summary()
        self.assertEqual(photos, 2)
        self.assertEqual(trackers, 1)
        self.assertTrue(all(size <= 500000 for size in sizes))
        self.page.locator("#saved-link").click()
        expect(self.page.locator(".photo-preview img").first).to_be_visible()
        self.page.locator(".photo-preview").first.click()
        expect(self.page.locator("#full-photo")).to_be_visible()
        self.page.locator("#close-dialog").click()
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.screenshot(path=str(self.output / "mobile-gallery.png"), full_page=True)
        self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))
        self.page.get_by_role("link", name="Daftar Pesanan", exact=True).click()
        self.page.screenshot(path=str(self.output / "mobile-list.png"), full_page=True)
        self.page.get_by_role("link", name="Tambah Pesanan", exact=False).first.click()
        self.page.screenshot(path=str(self.output / "mobile-capture.png"), full_page=True)
        self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))
        self.assertEqual(self.errors, [])


    def test_shared_order_uploader_labels_and_responsive_views(self):
        for username in ("browser-alice", "browser-bob"):
            self.login(username)
            self.page.get_by_role("link", name="Tambah Pesanan").first.click()
            self.page.locator("#order-id").fill("141")
            self.page.locator("#open-camera").click()
            expect(self.page.locator("#camera")).to_have_js_property("readyState", 4)
            self.page.locator("#take-photo").click()
            expect(self.page.locator("#photo-count")).to_have_text("1")
            self.page.locator("#save-photos").click()
            expect(self.page.locator(".queued-photo.saved")).to_have_count(1, timeout=20000)
            self.page.locator("#saved-link").click()
            expect(self.page.get_by_text(f"Diunggah oleh {username}", exact=True)).to_be_visible()
            if username == "browser-alice":
                self.page.get_by_role("button", name="Keluar").click()
        self.assertEqual(self.database_summary()[:2], (2, 1))
        expect(self.page.get_by_text("Diunggah oleh browser-alice", exact=True)).to_be_visible()
        expect(self.page.get_by_role("link", name="Edit ID order")).to_have_count(0)
        expect(self.page.get_by_role("link", name="Hapus", exact=True)).to_have_count(0)
        detail_url = self.page.url
        for name, width, height in [("desktop", 1440, 1000), ("mobile", 390, 844)]:
            self.page.set_viewport_size({"width": width, "height": height})
            self.page.goto(detail_url)
            expect(self.page.locator(".photo-preview img").first).to_be_visible()
            self.page.screenshot(path=str(self.output / f"shared-{name}-gallery.png"), full_page=True)
            self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))
            self.page.get_by_role("link", name="Daftar Pesanan", exact=True).click()
            expect(self.page.locator(".tracker-row")).to_have_count(1)
            self.page.screenshot(path=str(self.output / f"shared-{name}-list.png"), full_page=True)
            self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))
        self.assertEqual(self.errors, [])

    def test_permission_denied_retaking_and_no_persistent_image_storage(self):
        self.login()
        self.page.goto(self.live_server_url + "/tambah/")
        self.page.evaluate("() => { navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('denied', 'NotAllowedError'); }; }")
        self.page.locator("#open-camera").click()
        expect(self.page.locator("#camera-status")).to_contain_text("Izin kamera ditolak")
        self.page.reload()
        self.page.locator("#open-camera").click()
        expect(self.page.locator("#camera")).to_have_js_property("readyState", 4)
        self.page.locator("#take-photo").click()
        expect(self.page.locator("#photo-count")).to_have_text("1")
        self.page.get_by_role("button", name="Buang foto 1 untuk ambil ulang").click()
        expect(self.page.locator("#photo-count")).to_have_text("0")
        expect(self.page.locator("#save-photos")).to_be_disabled()
        self.assertEqual(self.page.locator("input[type=file]").count(), 0)
        self.assertEqual(self.page.evaluate("localStorage.length + sessionStorage.length"), 0)
        self.page.locator("#stop-camera").click()
        self.assertTrue(self.page.evaluate("document.querySelector('#camera').srcObject === null"))
        self.assertEqual(self.errors, [])


class BackblazeCameraBrowserTests(CameraBrowserTests):
    def setUp(self):
        super().setUp()
        self.s3.put_bucket_versioning(Bucket=settings.S3_BUCKET, VersioningConfiguration={"Status": "Enabled"})
        client_patch = patch("tracker.storage.client", return_value=self.s3)
        b2_patch = patch("tracker.storage.is_backblaze", return_value=True)
        client_patch.start()
        b2_patch.start()
        self.addCleanup(client_patch.stop)
        self.addCleanup(b2_patch.stop)

