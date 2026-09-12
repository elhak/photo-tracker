import base64
import hashlib
import io
import uuid
from datetime import timedelta
from urllib.request import Request, urlopen
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from PIL import Image
from tracker import storage
from tracker.models import UploadIntent, Photo


class Command(BaseCommand):
    help = "Check B2 upload, verification, download and deletion using a tiny synthetic JPEG; no tracker records are created."

    def add_arguments(self, parser):
        parser.add_argument("--write-test", action="store_true", help="Upload and remove a generated test image.")

    def handle(self, *args, **options):
        if not options["write_test"]:
            self.stdout.write("Use --write-test to upload, verify, download, and remove a generated test image.")
            return
        if not storage.is_backblaze():
            raise CommandError("This direct PUT check is for a configured Backblaze endpoint.")
        output = io.BytesIO()
        Image.new("RGB", (16, 16), "#14c7e7").save(output, format="JPEG")
        content = output.getvalue()
        intent = UploadIntent(id=uuid.uuid4(), created_at=timezone.now(), checksum=base64.b64encode(hashlib.sha256(content).digest()).decode())
        failure = None
        try:
            upload = storage.sign_upload(intent, len(content))
            with urlopen(Request(upload["url"], data=content, headers=upload["headers"], method="PUT"), timeout=30) as response:
                if response.status not in (200, 201, 204):
                    raise RuntimeError("Upload rejected")
            self.stdout.write("Direct signed PUT: OK")
            size, uploaded_at = storage.verify_and_copy(intent)
            self.stdout.write("Image verification and version-specific copy: OK")
            photo = Photo(object_key=intent.final_key, expires_at=uploaded_at + timedelta(days=30))
            with urlopen(storage.read_url(photo), timeout=30) as response:
                if response.read() != content:
                    raise RuntimeError("Downloaded photo differs")
            self.stdout.write(f"Private signed download: OK ({size} bytes)")
        except Exception as exc:
            # Never print exceptions containing signed URLs or credentials.
            code = getattr(exc, "response", {}).get("Error", {}).get("Code") or getattr(exc, "code", type(exc).__name__)
            failure = f"Storage check failed ({code})."
        finally:
            for key in [intent.staging_key, intent.final_key]:
                try:
                    storage.delete_object(key)
                except Exception:
                    self.stderr.write(f"Could not remove generated test key: {key}")
                    failure = "Test cleanup failed. The key needs listFiles and deleteFiles permissions."
            if not failure:
                self.stdout.write("Permanent deletion of test objects: OK")
        if failure:
            raise CommandError(failure)
