import base64
import hashlib
import io
import math
import warnings
from urllib.parse import urlsplit
from datetime import timedelta
import boto3
from botocore.config import Config
from django.conf import settings
from django.utils import timezone
from PIL import Image, UnidentifiedImageError


class InvalidPhoto(ValueError):
    pass


def is_backblaze():
    return (urlsplit(settings.S3_ENDPOINT_URL or "").hostname or "").endswith(".backblazeb2.com")


def client():
    if not settings.S3_BUCKET:
        raise RuntimeError("S3_BUCKET belum dikonfigurasi.")
    if settings.S3_ENDPOINT_URL:
        endpoint = urlsplit(settings.S3_ENDPOINT_URL)
        if endpoint.scheme not in ("https", "http") or not endpoint.hostname:
            raise RuntimeError("S3_ENDPOINT_URL harus berupa URL lengkap, diawali https://.")
    region = urlsplit(settings.S3_ENDPOINT_URL).hostname.split(".")[1] if is_backblaze() else settings.S3_REGION
    return boto3.client(
        "s3", region_name=region, endpoint_url=settings.S3_ENDPOINT_URL,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path" if settings.S3_ENDPOINT_URL else "virtual", "us_east_1_regional_endpoint": "regional"},
                      request_checksum_calculation="when_required", response_checksum_validation="when_required",
                      connect_timeout=5, read_timeout=15, retries={"max_attempts": 2}),
    )


def sign_upload(intent, byte_size=None):
    lifetime = math.floor((intent.created_at + timedelta(hours=24) - timezone.now()).total_seconds())
    if lifetime <= 0:
        raise InvalidPhoto("Sesi unggah sudah berakhir. Ambil foto kembali.")
    if is_backblaze():
        if type(byte_size) is not int or not 1 <= byte_size <= settings.MAX_PHOTO_BYTES:
            raise InvalidPhoto("Ukuran foto tidak valid. Muat ulang halaman dan ambil foto maksimal 500 KB.")
        headers = {"Content-Type": "image/jpeg", "Cache-Control": "private, no-store, max-age=0"}
        url = client().generate_presigned_url("put_object", Params={
            "Bucket": settings.S3_BUCKET, "Key": intent.staging_key,
            "ContentLength": byte_size, "ContentType": headers["Content-Type"],
            "CacheControl": headers["Cache-Control"],
        }, ExpiresIn=min(300, lifetime), HttpMethod="PUT")
        # The browser sets Content-Length automatically for the raw JPEG Blob.
        return {"method": "PUT", "url": url, "headers": headers}
    fields = {
        "Content-Type": "image/jpeg", "Cache-Control": "private, no-store, max-age=0",
        "x-amz-checksum-sha256": intent.checksum,
    }
    return client().generate_presigned_post(
        settings.S3_BUCKET, intent.staging_key, Fields=fields,
        Conditions=[{key: value} for key, value in fields.items()] + [
            ["content-length-range", 1, settings.MAX_PHOTO_BYTES],
        ], ExpiresIn=min(300, lifetime),
    )


def verify_and_copy(intent):
    s3 = client()
    response = s3.get_object(Bucket=settings.S3_BUCKET, Key=intent.staging_key)
    stream = response["Body"]
    try:
        if not 0 < response["ContentLength"] <= settings.MAX_PHOTO_BYTES:
            raise InvalidPhoto("Ukuran foto harus maksimal 500 KB.")
        data = stream.read(settings.MAX_PHOTO_BYTES + 1)
    finally:
        stream.close()
    if len(data) != response["ContentLength"] or len(data) > settings.MAX_PHOTO_BYTES:
        raise InvalidPhoto("Ukuran foto tidak valid.")
    if base64.b64encode(hashlib.sha256(data).digest()).decode() != intent.checksum:
        raise InvalidPhoto("Foto tidak cocok dengan permintaan unggah.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as picture:
                if picture.format != "JPEG" or picture.width * picture.height > 16_000_000:
                    raise InvalidPhoto("Foto harus berupa JPEG dengan resolusi yang sesuai.")
                picture.verify()
            with Image.open(io.BytesIO(data)) as picture:
                picture.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InvalidPhoto("Foto rusak atau bukan gambar JPEG.") from exc
    # Published keys are never writable with a browser's upload policy.
    # Conditional copy binds publication to the exact bytes just verified.
    source = {"Bucket": settings.S3_BUCKET, "Key": intent.staging_key}
    conditions = {"CopySourceIfMatch": response["ETag"]}
    if is_backblaze():
        if not response.get("VersionId"):
            raise RuntimeError("Backblaze tidak mengembalikan versi foto untuk verifikasi.")
        source["VersionId"] = response["VersionId"]
        conditions = {}  # Copy the exact verified version, even if another upload replaced the key.
    s3.copy_object(
        Bucket=settings.S3_BUCKET, Key=intent.final_key,
        CopySource=source, **conditions, MetadataDirective="REPLACE",
        ContentType="image/jpeg", CacheControl="private, no-store, max-age=0",
    )
    return len(data), response["LastModified"]


def read_url(photo):
    remaining = math.floor((photo.expires_at - timezone.now()).total_seconds())
    if remaining <= 0:
        raise InvalidPhoto("Foto sudah kedaluwarsa.")
    return client().generate_presigned_url(
        "get_object", Params={
            "Bucket": settings.S3_BUCKET, "Key": photo.object_key,
            "ResponseContentType": "image/jpeg", "ResponseCacheControl": "private, no-store, max-age=0",
        }, ExpiresIn=min(60, remaining),
    )


def delete_object(key):
    s3 = client()
    if not is_backblaze():
        s3.delete_object(Bucket=settings.S3_BUCKET, Key=key)
        return
    # B2 is always versioned. A plain delete only hides the object.
    versions = []
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=settings.S3_BUCKET, Prefix=key):
        for version in page.get("Versions", []) + page.get("DeleteMarkers", []):
            if version["Key"] == key:
                versions.append(version["VersionId"])
    for version_id in versions:
        s3.delete_object(Bucket=settings.S3_BUCKET, Key=key, VersionId=version_id)
