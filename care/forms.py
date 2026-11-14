from django import forms

from accounts.models import LocumLink, User

from .models import Case, Response
from .services import CASE_FIELDS, RESPONSE_FIELDS


class DoctorChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, user):
        return f"{user.get_full_name() or user.email} · {user.registration_number}"


class CaseForm(forms.ModelForm):
    doctor = DoctorChoiceField(queryset=User.objects.none(), label="Hired locum doctor")
    consent_confirmed = forms.BooleanField(
        label=(
            "I confirm consent for sharing this case and the written consultation "
            "has been recorded."
        )
    )
    video_consent = forms.BooleanField(
        required=False, label="I confirm consent for a video consultation has been recorded."
    )

    class Meta:
        model = Case
        fields = ("doctor",) + CASE_FIELDS
        widgets = {
            "presenting_complaint": forms.Textarea(attrs={"rows": 4}),
            "history": forms.Textarea(attrs={"rows": 3}),
            "allergies": forms.Textarea(attrs={"rows": 2}),
            "medications": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "age_years": "Age in years",
            "patient_reference": "Hospital case reference (optional)",
            "systolic_bp": "Systolic blood pressure (mmHg)",
            "diastolic_bp": "Diastolic blood pressure (mmHg)",
            "pulse_bpm": "Pulse (beats/min)",
            "respiratory_rate": "Respiratory rate (breaths/min)",
            "temperature_c": "Temperature (°C)",
            "spo2_percent": "Oxygen saturation (%)",
        }

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        if user.role == User.Role.STAFF and user.hospital_id:
            self.fields["doctor"].queryset = User.objects.filter(
                role=User.Role.DOCTOR,
                is_active=True,
                doctor_verified=True,
                email_verified_at__isnull=False,
                hospital__isnull=True,
                locum_links__hospital_id=user.hospital_id,
                locum_links__hospital__is_approved=True,
                locum_links__state=LocumLink.State.ACTIVE,
                locum_links__accepted_at__isnull=False,
            ).order_by("first_name", "email")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("review_mode") == Case.ReviewMode.VIDEO and not cleaned.get("video_consent"):
            self.add_error("video_consent", "Confirm recorded consent before requesting video.")
        return cleaned


class ResponseForm(forms.ModelForm):
    attestation = forms.BooleanField(
        label=(
            "I confirm this is my final written consultation, based on the "
            "information available and its limitations."
        )
    )
    current_password = forms.CharField(
        label="Confirm your current password",
        widget=forms.PasswordInput(render_value=False),
        strip=False,
    )

    class Meta:
        model = Response
        fields = RESPONSE_FIELDS
        widgets = {
            field: forms.Textarea(
                attrs={"rows": 4 if field in ("assessment", "recommendations") else 2}
            )
            for field in RESPONSE_FIELDS
        }


class ReportUploadForm(forms.Form):
    report = forms.FileField(label="PDF, JPEG or PNG report (up to 5 MiB)")
