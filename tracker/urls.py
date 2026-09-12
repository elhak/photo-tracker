from django.urls import path
from . import views

urlpatterns = [
    path("masuk/", views.sign_in, name="login"),
    path("keluar/", views.sign_out, name="logout"),
    path("", views.tracker_list, name="tracker-list"),
    path("tambah/", views.capture, name="capture"),
    path("tracker/<int:pk>/", views.tracker_detail, name="tracker-detail"),
    path("tracker/<int:pk>/edit/", views.tracker_edit, name="tracker-edit"),
    path("foto/<uuid:pk>/hapus/", views.photo_delete, name="photo-delete"),
    path("pengguna/", views.users, name="users"),
    path("pengguna/tambah/", views.user_create, name="user-create"),
    path("pengguna/<int:pk>/sandi/", views.user_password, name="user-password"),
    path("pengguna/<int:pk>/hapus/", views.user_delete, name="user-delete"),
    path("api/uploads/", views.upload_intent, name="upload-intent"),
    path("api/uploads/<uuid:pk>/complete/", views.upload_complete, name="upload-complete"),
    path("api/photos/<uuid:pk>/url/", views.photo_url, name="photo-url"),
    path("health/", views.health, name="health"),
]
