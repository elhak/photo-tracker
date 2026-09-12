from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import base64
import json
import os
from botocore.exceptions import ClientError
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from tracker import storage


class Command(BaseCommand):
    help = "Add browser upload CORS origins to the configured bucket, preserving existing S3 CORS rules."

    def add_arguments(self, parser):
        parser.add_argument("--origin", action="append", required=True)
        parser.add_argument("--apply", action="store_true", help="Save the rule; otherwise only check.")

    def handle(self, *args, **options):
        origins = list(dict.fromkeys(options["origin"]))
        for origin in origins:
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise CommandError("Use exact origins such as http://127.0.0.1:8081, without paths or trailing slashes.")
        s3 = storage.client()
        try:
            rules = s3.get_bucket_cors(Bucket=settings.S3_BUCKET).get("CORSRules", [])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "NoSuchCORSConfiguration":
                rules = []
            else:
                raise CommandError("Cannot read CORS rules. Use a key with readBuckets and writeBuckets for setup.") from None
        rule_id = "boss-laundry-browser"
        managed = next((rule for rule in rules if rule.get("ID") == rule_id), None)
        if managed:
            origins = list(dict.fromkeys(managed.get("AllowedOrigins", []) + origins))
        methods = ["PUT", "GET", "HEAD"] if storage.is_backblaze() else ["POST", "GET", "HEAD"]
        rule = {"ID": rule_id, "AllowedOrigins": origins, "AllowedMethods": methods,
                "AllowedHeaders": ["*"], "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 300}
        new_rules = [old for old in rules if old.get("ID") != rule_id] + [rule]
        if len(new_rules) > 100:
            raise CommandError("Bucket already has the maximum number of CORS rules.")
        if options["apply"]:
            try:
                s3.put_bucket_cors(Bucket=settings.S3_BUCKET, CORSConfiguration={"CORSRules": new_rules})
                saved = s3.get_bucket_cors(Bucket=settings.S3_BUCKET).get("CORSRules", [])
                if not any(all(origin in entry.get("AllowedOrigins", []) for origin in origins)
                           and all(method in entry.get("AllowedMethods", []) for method in methods) for entry in saved):
                    raise CommandError("CORS update could not be verified.")
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "unknown")
                if storage.is_backblaze() and code == "InvalidRequest" and "B2 Native CORS" in exc.response.get("Error", {}).get("Message", ""):
                    self.update_native_cors(origins)
                    return
                raise CommandError(f"Cannot update CORS ({code}). Use a separate setup key with writeBuckets permission.") from None
            self.stdout.write(self.style.SUCCESS("Browser upload CORS saved and verified."))
        else:
            self.stdout.write("Existing S3 CORS rules: " + str(len(rules)))
            self.stdout.write("Proposed methods: " + ", ".join(methods))
            self.stdout.write("Use --apply to add the requested origins while preserving existing rules.")

    def update_native_cors(self, origins):
        def request_json(url, authorization, data=None):
            payload = json.dumps(data).encode() if data is not None else None
            try:
                with urlopen(Request(url, data=payload, headers={"Authorization": authorization, "Content-Type": "application/json"}), timeout=30) as response:
                    return json.load(response)
            except HTTPError as exc:
                raise CommandError(f"B2 CORS setup failed (HTTP {exc.code}). Check setup-key permissions and retry.") from None

        key_id, key = os.environ.get("AWS_ACCESS_KEY_ID", ""), os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        basic = base64.b64encode(f"{key_id}:{key}".encode()).decode()
        auth = request_json("https://api.backblazeb2.com/b2api/v4/b2_authorize_account", "Basic " + basic)
        api = auth["apiInfo"]["storageApi"]
        if "writeBuckets" not in api["allowed"]["capabilities"]:
            raise CommandError("The setup key needs writeBuckets permission to update native B2 CORS rules.")
        api_url = api["apiUrl"].rstrip("/")
        parsed = urlsplit(api_url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".backblazeb2.com"):
            raise CommandError("B2 returned an unexpected API endpoint.")
        def call(operation, data):
            return request_json(f"{api_url}/b2api/v4/{operation}", auth["authorizationToken"], data)
        query = {"accountId": auth["accountId"], "bucketName": settings.S3_BUCKET}
        buckets = call("b2_list_buckets", query)["buckets"]
        bucket = next((item for item in buckets if item["bucketName"] == settings.S3_BUCKET), None)
        if not bucket:
            raise CommandError("Configured bucket not found.")
        name = "boss-laundry-browser"
        old_rules = bucket.get("corsRules", [])
        managed = next((rule for rule in old_rules if rule["corsRuleName"] == name), {})
        origins = list(dict.fromkeys(managed.get("allowedOrigins", []) + origins))
        rule = {"corsRuleName": name, "allowedOrigins": origins, "allowedOperations": ["s3_put", "s3_get", "s3_head"],
                "allowedHeaders": ["*"], "exposeHeaders": ["ETag"], "maxAgeSeconds": 300}
        rules = [rule] + [old for old in old_rules if old["corsRuleName"] != name]
        if len(rules) > 100:
            raise CommandError("Bucket already has the maximum number of CORS rules.")
        # Preserve privacy, lifecycle, encryption and all unrelated bucket settings.
        call("b2_update_bucket", {"accountId": auth["accountId"], "bucketId": bucket["bucketId"],
                                  "ifRevisionIs": bucket["revision"], "corsRules": rules})
        saved = call("b2_list_buckets", query)["buckets"][0]
        if rule not in saved.get("corsRules", []) or saved["bucketType"] != bucket["bucketType"]:
            raise CommandError("B2 CORS update could not be verified.")
        self.stdout.write(self.style.SUCCESS("Native B2 browser upload CORS saved and verified; other bucket settings preserved."))
