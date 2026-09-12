import base64
import hashlib
import json
import logging
import uuid
from datetime import timedelta
from functools import wraps
from botocore.exceptions import BotoCoreError, ClientError
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from .forms import CreateUserForm, OrderForm
from .models import LoginAttempt, Photo, Tracker, UploadIntent, User
from . import services, storage

logger = logging.getLogger(__name__)


def admin_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            return HttpResponseForbidden("Akses hanya untuk admin.")
        return view(request, *args, **kwargs)
    return wrapped


def trackers_for(user):
    qs = Tracker.objects.all()
    return qs if user.is_staff else qs.filter(owner=user)


def live_photos():
    return Photo.objects.filter(deleted_at__isnull=True, expires_at__gt=timezone.now())


@require_http_methods(["GET", "POST"])
def sign_in(request):
    if request.user.is_authenticated:
        return redirect("tracker-list")
    form = AuthenticationForm(request, data=request.POST if request.method == "POST" else None)
    status = 200
    if request.method == "POST":
        username = request.POST.get("username", "")[:150]
        ip = request.META.get("HTTP_X_REAL_IP", request.META.get("REMOTE_ADDR", "")) if not settings.DEBUG else request.META.get("REMOTE_ADDR", "")
        limits = [(hashlib.sha256(f"user:{username.casefold()}".encode()).hexdigest(), 10),
                  (hashlib.sha256(f"ip:{ip}".encode()).hexdigest(), 30)]
        blocked = False
        # Reserve each attempt before authentication to prevent concurrent bypasses.
        with transaction.atomic():
            for key, limit in limits:
                attempt, _ = LoginAttempt.objects.get_or_create(key=key)
                if attempt.started_at <= timezone.now() - timedelta(minutes=15):
                    attempt.failures = 0
                    attempt.started_at = timezone.now()
                if attempt.failures >= limit:
                    blocked = True
                attempt.failures += 1
                attempt.save()
        if blocked:
            form.add_error(None, "Terlalu banyak percobaan. Coba lagi dalam 15 menit.")
            status = 429
        elif form.is_valid():
            login(request, form.get_user())
            LoginAttempt.objects.filter(key=limits[0][0]).delete()
            return redirect("tracker-list")
        else:
            status = 400
    return render(request, "tracker/login.html", {"form": form}, status=status)


@require_POST
def sign_out(request):
    logout(request)
    return redirect("login")


@login_required
@require_GET
def tracker_list(request):
    search = request.GET.get("q", "").strip()[:100]
    valid = Q(photos__deleted_at__isnull=True, photos__expires_at__gt=timezone.now())
    qs = trackers_for(request.user).select_related("owner").annotate(photo_count=Count("photos", filter=valid)).filter(photo_count__gt=0).order_by("-updated_at", "-pk")
    if search:
        qs = qs.filter(order_id__contains=search)
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    return render(request, "tracker/list.html", {"page": page, "search": search})


@login_required
@require_GET
def tracker_detail(request, pk):
    tracker = get_object_or_404(trackers_for(request.user).select_related("owner"), pk=pk)
    photos = live_photos().filter(tracker=tracker).select_related("added_by").order_by("-uploaded_at")
    page = Paginator(photos, 24).get_page(request.GET.get("page"))
    return render(request, "tracker/detail.html", {"tracker": tracker, "page": page})


@login_required
@require_GET
def capture(request):
    tracker = None
    if request.GET.get("tracker"):
        # Appending to someone else's order is an admin-only edit.
        tracker = get_object_or_404(trackers_for(request.user), pk=request.GET["tracker"])
    return render(request, "tracker/capture.html", {"tracker": tracker})


def api_view(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Sesi berakhir. Silakan masuk kembali."}, status=401)
        try:
            return view(request, *args, **kwargs)
        except (ValueError, KeyError, TypeError, forms.ValidationError) as exc:
            return JsonResponse({"error": str(exc) if isinstance(exc, (services.ActionError, storage.InvalidPhoto)) else "Data permintaan tidak valid."}, status=400)
        except (BotoCoreError, ClientError, RuntimeError):
            logger.warning("Photo storage request failed", exc_info=True)
            return JsonResponse({"error": "Penyimpanan foto belum tersedia. Silakan coba lagi."}, status=503)
    return wrapped


@require_POST
@api_view
def upload_intent(request):
    data = json.loads(request.body)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    if not isinstance(data.get("order_id"), str) or not isinstance(data.get("request_id"), str):
        raise ValueError("Invalid field type")
    if data.get("tracker_id") is not None and (
        isinstance(data["tracker_id"], bool)
        or not str(data["tracker_id"]).isdigit()
        or len(str(data["tracker_id"])) > 18
    ):
        raise ValueError("Invalid tracker ID")
    form = OrderForm(data)
    if not form.is_valid():
        return JsonResponse({"error": "Isi ID order (maksimal 100 karakter)."}, status=400)
    checksum = data["checksum"]
    if not isinstance(checksum, str) or len(base64.b64decode(checksum, validate=True)) != 32:
        raise ValueError("checksum")
    request_id = uuid.UUID(data["request_id"])
    owner = request.user
    order_id = form.cleaned_data["order_id"]
    with transaction.atomic():
        if data.get("tracker_id"):
            tracker = get_object_or_404(trackers_for(request.user), pk=data["tracker_id"])
            owner, order_id = tracker.owner, tracker.order_id
        intent = UploadIntent.objects.filter(actor=request.user, request_id=request_id).first()
        if intent:
            if intent.checksum != checksum:
                raise ValueError("request_id already used")
            services.check_intent_access(intent, request.user)
        else:
            if UploadIntent.objects.filter(actor=request.user, completed_at__isnull=True, created_at__gt=timezone.now() - timedelta(hours=24)).count() >= 100:
                return JsonResponse({"error": "Terlalu banyak unggahan tertunda. Selesaikan foto sebelumnya atau coba besok."}, status=429)
            intent = UploadIntent.objects.create(actor=request.user, owner=owner, order_id=order_id, checksum=checksum, request_id=request_id)
    if intent.completed_at:
        return JsonResponse(completed_payload(intent))
    return JsonResponse({"id": str(intent.id), "upload": storage.sign_upload(intent, byte_size=data.get("byte_size"))})


def completed_payload(intent):
    photo = intent.photo
    return {"id": str(intent.id), "completed": True,
            "tracker_url": f"/tracker/{photo.tracker_id}/" if photo else "/"}


@require_POST
@api_view
def upload_complete(request, pk):
    get_object_or_404(UploadIntent, pk=pk, actor=request.user)
    intent = services.complete_upload(pk, request.user)
    return JsonResponse(completed_payload(intent))


@require_GET
@api_view
def photo_url(request, pk):
    photo = get_object_or_404(live_photos().filter(tracker__in=trackers_for(request.user)), pk=pk)
    return JsonResponse({"url": storage.read_url(photo)})


@admin_required
@require_http_methods(["GET", "POST"])
def tracker_edit(request, pk):
    tracker = get_object_or_404(Tracker, pk=pk)
    form = OrderForm(request.POST if request.method == "POST" else None, initial={"order_id": tracker.order_id})
    collision = None
    if request.method == "POST" and form.is_valid():
        order_id = form.cleaned_data["order_id"]
        collision = Tracker.objects.filter(owner=tracker.owner, order_id=order_id).exclude(pk=tracker.pk).first()
        if not collision or request.POST.get("confirm") == "yes":
            try:
                target = services.rename_tracker(pk, order_id, confirmed=request.POST.get("confirm") == "yes")
            except services.ActionError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "ID order berhasil diperbarui.")
                return redirect("tracker-detail", pk=target.pk)
    return render(request, "tracker/edit.html", {"tracker": tracker, "form": form, "collision": collision})


@admin_required
@require_http_methods(["GET", "POST"])
def photo_delete(request, pk):
    photo = get_object_or_404(live_photos(), pk=pk)
    if request.method == "POST":
        services.remove_photo(photo.pk)
        messages.success(request, "Foto dihapus. Penghapusan penyimpanan akan diproses otomatis.")
        return redirect("tracker-list")
    return render(request, "tracker/confirm.html", {"title": "Hapus foto?", "description": f"Foto pada order {photo.tracker.order_id} akan dihapus permanen.", "cancel_url": f"/tracker/{photo.tracker_id}/"})


@admin_required
@require_GET
def users(request):
    page = Paginator(User.objects.order_by("-is_active", "username"), 30).get_page(request.GET.get("page"))
    return render(request, "tracker/users.html", {"page": page})


@admin_required
@require_http_methods(["GET", "POST"])
def user_create(request):
    form = CreateUserForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Pengguna berhasil ditambahkan.")
        return redirect("users")
    return render(request, "tracker/form.html", {"form": form, "title": "Tambah pengguna", "button": "Simpan pengguna"})


@admin_required
@require_http_methods(["GET", "POST"])
def user_password(request, pk):
    user = get_object_or_404(User, pk=pk, is_active=True)
    form = SetPasswordForm(user, request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Kata sandi diperbarui. Sesi login lama telah berakhir.")
        return redirect("users")
    return render(request, "tracker/form.html", {"form": form, "title": f"Atur kata sandi · {user.username}", "button": "Simpan kata sandi"})


@admin_required
@require_http_methods(["GET", "POST"])
def user_delete(request, pk):
    user = get_object_or_404(User, pk=pk, is_active=True)
    if request.method == "POST":
        try:
            services.disable_user(user.pk)
        except services.ActionError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Akses pengguna dihapus. Foto tetap tersedia sampai kedaluwarsa.")
        return redirect("users")
    return render(request, "tracker/confirm.html", {"title": f"Hapus pengguna {user.username}?", "description": "Pengguna tidak dapat masuk lagi. Foto tetap tersedia untuk admin sampai kedaluwarsa.", "cancel_url": "/pengguna/"})


@require_GET
def health(request):
    User.objects.exists()
    return HttpResponse("ok", content_type="text/plain")
