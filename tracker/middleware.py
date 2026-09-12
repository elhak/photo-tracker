from urllib.parse import urlsplit
from django.conf import settings


class PrivateResponseMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["Cache-Control"] = "private, no-store, max-age=0"
        response["Referrer-Policy"] = "same-origin"
        response["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
        s3 = (f"https://{settings.S3_BUCKET}.s3.{settings.S3_REGION}.amazonaws.com "
              f"https://s3.{settings.S3_REGION}.amazonaws.com")
        if not settings.S3_BUCKET:
            s3 = ""
        if settings.S3_ENDPOINT_URL:
            parsed = urlsplit(settings.S3_ENDPOINT_URL)
            s3 = f"{parsed.scheme}://{parsed.netloc}"
        response["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            f"img-src 'self' blob: {s3}; connect-src 'self' {s3}; "
            "media-src 'self' blob:; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        return response
