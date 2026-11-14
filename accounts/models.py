import uuid

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower

from .managers import UserManager


class Hospital(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=160)
    is_approved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class User(AbstractUser):
    class Role(models.TextChoices):
        STAFF = "STAFF", "Hospital team member"
        DOCTOR = "DOCTOR", "Locum doctor"

    STAFF = Role.STAFF
    DOCTOR = Role.DOCTOR
    username = None
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.STAFF)
    hospital = models.ForeignKey(
        Hospital, on_delete=models.PROTECT, null=True, blank=True, related_name="members"
    )
    email_verified_at = models.DateTimeField(null=True, blank=True)
    email_token_version = models.PositiveIntegerField(default=0, editable=False)
    registration_number = models.CharField(max_length=100, blank=True)
    doctor_verified = models.BooleanField(default=False)
    verification_reference = models.CharField(max_length=200, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("email"), name="accounts_unique_email_lower"),
            models.CheckConstraint(
                condition=models.Q(hospital__isnull=True) | models.Q(role="STAFF"),
                name="accounts_hospital_staff_only",
            ),
        ]

    def clean(self):
        super().clean()
        self.email = self.email.strip().lower()
        if self.hospital_id and self.role != self.Role.STAFF:
            raise ValidationError({"hospital": "Only hospital team members have a hospital."})
        if self.doctor_verified and self.role != self.Role.DOCTOR:
            raise ValidationError({"doctor_verified": "Only a doctor can be verified as a doctor."})

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email


class LocumLink(models.Model):
    class State(models.TextChoices):
        PENDING = "PENDING", "Awaiting doctor acceptance"
        ACTIVE = "ACTIVE", "Active"
        DECLINED = "DECLINED", "Declined"
        REVOKED = "REVOKED", "Revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    hospital = models.ForeignKey(Hospital, on_delete=models.PROTECT, related_name="locum_links")
    doctor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="locum_links")
    invited_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="sent_locum_links")
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["hospital", "doctor"], name="accounts_unique_locum_link"
            ),
        ]

    def clean(self):
        super().clean()
        if self.doctor_id and self.doctor.role != User.Role.DOCTOR:
            raise ValidationError({"doctor": "The invited account must be a doctor."})
        if self.invited_by_id and (
            self.invited_by.role != User.Role.STAFF
            or self.invited_by.hospital_id != self.hospital_id
        ):
            raise ValidationError({"invited_by": "The inviter must belong to this hospital."})

    def __str__(self):
        return f"{self.hospital} — {self.doctor} ({self.get_state_display()})"
