# Boss Laundry

A small Django application for camera-only order documentation. Bahasa Indonesia UI, admin/user permissions, direct private S3 uploads, a strict 500,000-byte JPEG limit, and independent 30-day photo expiration.

Branding uses the supplied Boss Laundry horizontal logo and primary logo, resized for the header and favicon. Original artwork is unchanged; the UI matches its black, cyan (`#14c7e7`), and blue palette.

## Local development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). No Node build is required.

```sh
uv sync --locked
export DJANGO_DEBUG=1
uv run manage.py migrate
uv run manage.py createsuperuser
uv run manage.py runserver 127.0.0.1:8000
```

Open http://127.0.0.1:8000. Desktop localhost supports the camera; phones connecting to a LAN IP require HTTPS. Set the S3 variables from `.env.example` in your shell to enable photo storage. Django does not automatically load `.env` files. `uv run --env-file .env manage.py runserver` is available if you prefer an environment file. Never use development debug mode on a public server.

Without S3 credentials, login and user management work; uploads fail with an Indonesian configuration/storage message and remain retryable in browser memory. There is no fake storage fallback.

```sh
DJANGO_DEBUG=1 uv run manage.py test
DJANGO_DEBUG=1 uv run manage.py makemigrations --check --dry-run
```

For the optional simulated-camera browser suite and screenshots:

```sh
uv run playwright install chromium
DJANGO_DEBUG=1 uv run manage.py test tracker.browser_tests --noinput
```

Screenshots are written to ignored `test-results/`. Do not run the browser and backend suites concurrently: both use the same temporary test database.

Tests use Moto S3 in memory, not a real AWS account. Concurrent-submission tests use a separate file-backed SQLite test database at `data/test.sqlite3`.

## Behavior

- `is_staff` is the app's admin role. `createsuperuser` creates the initial admin. Admins can create both roles and reset passwords; registration and email delivery are absent.
- Account setup asks for a username, role, and password (with confirmation); no email or full name is required. The initial `createsuperuser` command does not ask for email either. Passwords need at least six characters, with no uppercase, symbol, numeric-only, common-password, or username-similarity restrictions. The same rule applies to creation and password resets.
- Users see and append only their own tracker data. Admins see and edit all data. Admin appends preserve the owner and record the actual uploader. Removing a user disables access and invalidates password-bound sessions permanently; the username remains reserved and photos retain normal expiration.
- Order IDs are trimmed, case-sensitive, at most 100 characters, and preserve leading zeros. `(owner, order_id)` is unique. SQLite uses WAL, a 20-second busy timeout, and short immediate transactions for concurrent writes. Storage network requests run outside transactions.
- Deleting an individual photo hides it immediately; background cleanup removes its object. A previously issued read URL may remain usable for at most 60 seconds, even after access revocation. Expiration URLs never outlive photo expiration.
- Camera frames become JPEG blobs, capped at 1920 pixels on the longest side, then progressively reduced to at most 500,000 bytes. Preview/remove lets the user retake a photo. Photos and previews are never intentionally persisted on the device; closing/reloading the page loses unsaved photos. The app cannot control browser/OS memory management or prevent screenshots.
- Each photo has a stable client request UUID. An authenticated intent returns a five-minute signed S3 POST with a fixed key, content type, size range, SHA-256 checksum, and no-store metadata. A completion request validates the S3 bytes in RAM, then conditionally copies them to a separate published key. Reusing an upload policy cannot overwrite published photos. The VPS never writes image files to disk.
- Retry completes an already-uploaded photo without repeating the upload. An upload batch saves each successful photo independently and leaves failed photos available for retry. No tracker exists until a verified photo is saved. Up to 100 incomplete intents per user may exist at once to bound abandoned storage usage.
- Access to expired photos is denied immediately. A five-minute job removes expired/deleted objects and empty trackers. Failed deletions remain queued and cause a nonzero job exit. A bucket lifecycle policy is the fallback; physical deletion is not guaranteed at the exact expiration second if S3 is unavailable.
- Timestamps are UTC in storage and displayed in Asia/Jakarta. Expiration is 30 × 24 hours after S3's server-recorded upload time; client clock values are never trusted for enforcement.

## API

All application mutations require a Django session cookie and CSRF token. No photo bytes are accepted by application endpoints.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/uploads/` | JSON: `order_id`, `request_id` (UUID), `checksum` (base64 SHA-256), optional `tracker_id`. Server derives owner and validates access. Returns intent `id` and S3 `upload: {url, fields}`, or a completed receipt. |
| `POST /api/uploads/<uuid>/complete/` | Verifies and publishes an intent belonging to the current actor. Returns `completed: true` and `tracker_url`. Repeats do not duplicate photos. |
| `GET /api/photos/<uuid>/url/` | Authorizes the current user and returns a private signed GET URL valid for at most 60 seconds, capped by photo expiry. |
| `GET /health/` | Checks the app and database; returns `ok`. Does not expose credentials or probe S3. |

## VPS deployment (Ubuntu 24.04 / Python 3.12)

Use a new private, **never-versioned** S3 bucket. A previously versioned bucket may retain old versions; use a dedicated new bucket for this app. Enable all four S3 Block Public Access settings, Bucket owner enforced object ownership, and default SSE-S3 encryption. Do not grant public read access.

1. Choose a domain and point its DNS to the VPS. Install Python 3.12, uv, and Caddy using their official installation instructions. Allow inbound 80/443; the application binds only to loopback. Place the source in `/opt/photo-tracker`, readable by the service and Caddy. Keep source and the virtualenv owned by your deployment account, not writable by the web process.
2. Create an unprivileged service account and data directory:

   ```sh
   sudo useradd --system --home /var/lib/photo-tracker --shell /usr/sbin/nologin photo-tracker
   sudo install -d -o photo-tracker -g photo-tracker -m 700 /var/lib/photo-tracker
   cd /opt/photo-tracker
   uv sync --locked --no-dev
   ```

3. Copy `.env.example` to `/etc/photo-tracker.env`. Set the domain, trusted origin, database path, region, bucket, and runtime IAM credentials. Generate `DJANGO_SECRET_KEY` with `.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(64))'`. Keep `DJANGO_DEBUG=0`. Set file ownership to `root:photo-tracker`, permissions `640`. Never commit credentials. An EC2 instance role can replace static AWS keys; omit key variables when using a role.
4. Replace `REPLACE_BUCKET` in `deploy/s3-iam-policy.json` and `deploy/s3-bucket-policy.json`, and the domain in `deploy/s3-cors.json`. Apply the IAM policy only to the app's runtime identity. Apply the bucket policy, CORS, and lifecycle with an infrastructure admin identity (the app cannot change bucket configuration). For a dedicated new bucket:

   ```sh
   aws s3api put-public-access-block --bucket YOUR_BUCKET --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
   aws s3api put-bucket-ownership-controls --bucket YOUR_BUCKET --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
   aws s3api put-bucket-policy --bucket YOUR_BUCKET --policy file://deploy/s3-bucket-policy.json
   aws s3api put-bucket-cors --bucket YOUR_BUCKET --cors-configuration file://deploy/s3-cors.json
   aws s3api put-bucket-lifecycle-configuration --bucket YOUR_BUCKET --lifecycle-configuration file://deploy/s3-lifecycle.json
   aws s3api get-bucket-versioning --bucket YOUR_BUCKET
   aws s3api get-bucket-encryption --bucket YOUR_BUCKET
   ```

   Versioning should have no enabled/suspended status. Verify default AES256 encryption. A custom S3 endpoint must implement signed POST, SHA-256 checksums, and conditional copy; real compatibility must be tested before use.
5. Run migrations and create the initial admin with the environment loaded. If uv is installed only in a personal home directory, use its absolute executable path or install it system-wide before these commands:

   ```sh
   sudo -u photo-tracker uv run --no-sync --env-file /etc/photo-tracker.env manage.py migrate
   sudo -u photo-tracker uv run --no-sync --env-file /etc/photo-tracker.env manage.py createsuperuser
   uv run --no-sync --env-file /etc/photo-tracker.env manage.py collectstatic --noinput
   uv run --no-sync --env-file /etc/photo-tracker.env manage.py check --deploy
   ```

   Run collectstatic as the deployment account with permission to read the environment file. It writes only static code assets, not user photos. Caddy needs read/traverse access to `/opt/photo-tracker/staticfiles`.
6. Replace `tracker.example.com` in `deploy/Caddyfile`, install it as `/etc/caddy/Caddyfile`, and install the service/timer files:

   ```sh
   sudo cp deploy/photo-tracker*.service deploy/photo-tracker*.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now photo-tracker.service photo-tracker-cleanup.timer photo-tracker-backup.timer
   sudo caddy validate --config /etc/caddy/Caddyfile
   sudo systemctl reload caddy
   ```

   Caddy obtains TLS certificates automatically. Do not expose port 8000 or trust client-supplied proxy headers directly. Caddy overwrites `X-Real-IP`, used for login rate limiting. Avoid access logging full signed S3 URLs.

   Django's deployment check intentionally reports HSTS subdomain/preload advisories by default. Enable the two `DJANGO_HSTS_*` flags only when you own that HTTPS policy for the entire domain subtree; normal HTTPS and one-year HSTS on the app host are already enabled.
7. Check `https://YOUR_DOMAIN/health/`, sign in, add a user, capture a photo, and run `sudo systemctl start photo-tracker-cleanup.service`. Inspect `journalctl -u photo-tracker -u photo-tracker-cleanup` and `systemctl list-timers 'photo-tracker-*'`. Alert on failed units and on cleanup not succeeding for more than 15 minutes using your VPS monitoring. `/health/` alone does not verify cleanup or S3.

The daily timer keeps seven verified SQLite backups in `/var/lib/photo-tracker/backups`. Copy these off-server using your existing backup system; backups contain account and order metadata, never image bytes. Restore with the web app and cleanup service stopped: back up the current database, remove its old `-wal`/`-shm` files, place the chosen backup at `DATABASE_PATH`, restore service ownership/mode, run migrations, then run cleanup before reopening access. Expiration checks still hide expired records restored from an older backup.

For updates: back up, stop the app, deploy code, run `uv sync --locked --no-dev`, migrate, collect static assets, run deployment checks, and restart. Keep old code and the pre-migration database backup for rollback. Use PostgreSQL if usage grows beyond the intended small team.

## Real-device acceptance checklist

Before production use, test over the actual HTTPS domain on Android Chrome and iPhone Safari:

- Allow and deny camera permission; retry after granting permission in site settings.
- Rear/front camera selection where available, portrait/landscape orientation, background/foreground transitions.
- Capture several detailed photos; confirm each stored JPEG is ≤500,000 bytes and the device gallery stays unchanged.
- Remove a preview and retake. Saving without a photo must be unavailable.
- Interrupt the network during upload and completion; retry with no duplicate records or lost successful photos.
- Close the capture page with unsaved images; check the warning and camera indicator switching off.
- Confirm user isolation, admin edits, user deletion, independent photo expiry, and empty tracker cleanup.

Browser automation can validate a simulated camera, but cannot certify physical camera behavior or OS gallery behavior. Real AWS policy enforcement and real-device testing need the deployment environment.
