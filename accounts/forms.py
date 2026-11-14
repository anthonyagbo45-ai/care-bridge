from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm, UserChangeForm, UserCreationForm
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Hospital, User


class EmailAuthenticationForm(AuthenticationForm):
    username = forms.EmailField(
        label="Email address", widget=forms.EmailInput(attrs={"autofocus": True})
    )

    def clean_username(self):
        return self.cleaned_data["username"].strip().lower()

    def confirm_login_allowed(self, user):
        if not user.is_active or not user.email_verified_at:
            raise self.get_invalid_login_error()


class RegistrationForm(forms.Form):
    full_name = forms.CharField(max_length=150, label="Your full name")
    email = forms.EmailField(label="Email address")
    role = forms.ChoiceField(choices=User.Role.choices, label="I am a")
    hospital_name = forms.CharField(
        max_length=160,
        required=False,
        help_text="Hospital team members: enter your hospital name. Owner approval is required.",
    )
    registration_number = forms.CharField(
        max_length=100,
        required=False,
        label="Medical registration number",
        help_text="Doctors: provide the number for the owner to verify.",
    )
    password1 = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput,
        help_text=password_validation.password_validators_help_text_html(),
    )
    password2 = forms.CharField(label="Confirm password", strip=False, widget=forms.PasswordInput)

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def clean(self):
        data = super().clean()
        role = data.get("role")
        if role == User.Role.STAFF and not data.get("hospital_name"):
            self.add_error("hospital_name", "Enter your hospital name.")
        if role == User.Role.DOCTOR and not data.get("registration_number"):
            self.add_error("registration_number", "Enter your medical registration number.")
        if data.get("password1") and data.get("password2"):
            if data["password1"] != data["password2"]:
                self.add_error("password2", "The passwords do not match.")
            user = User(
                email=data.get("email", ""),
                first_name=data.get("full_name", ""),
                role=role or User.Role.STAFF,
            )
            try:
                password_validation.validate_password(data["password1"], user)
            except ValidationError as exc:
                self.add_error("password1", exc)
        return data

    @transaction.atomic
    def save(self):
        data = self.cleaned_data
        hospital = None
        if data["role"] == User.Role.STAFF:
            hospital = Hospital.objects.create(name=data["hospital_name"])
        return User.objects.create_user(
            email=data["email"],
            password=data["password1"],
            first_name=data["full_name"],
            role=data["role"],
            hospital=hospital,
            registration_number=data["registration_number"]
            if data["role"] == User.Role.DOCTOR
            else "",
        )


class InviteDoctorForm(forms.Form):
    doctor_email = forms.EmailField(
        label="Doctor's exact email address",
        help_text="Use the address of your already hired, verified locum doctor.",
    )


class AdminUserCreationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("email", "first_name", "role", "hospital")


class AdminUserChangeForm(UserChangeForm):
    class Meta:
        model = User
        fields = "__all__"

    def clean(self):
        data = super().clean()
        if data.get("doctor_verified"):
            if not data.get("registration_number", "").strip():
                self.add_error("registration_number", "Record the doctor's registration number.")
            if not data.get("verification_reference", "").strip():
                self.add_error(
                    "verification_reference", "Record your independent verification reference."
                )
        return data
