from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import User


class OrderForm(forms.Form):
    order_id = forms.CharField(label="ID Order", max_length=100, strip=True)


class CreateUserForm(UserCreationForm):
    role = forms.ChoiceField(label="Peran", choices=[("user", "Pengguna"), ("admin", "Admin")])

    class Meta:
        model = User
        fields = ("username", "role", "password1", "password2")
        labels = {"username": "Nama pengguna"}

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_staff = self.cleaned_data["role"] == "admin"
        if commit:
            user.save()
        return user
