import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q


class Case(models.Model):
    class ReviewMode(models.TextChoices):
        TEXT = "TEXT", "Written consultation"
        VIDEO = "VIDEO", "Video and written consultation"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Awaiting doctor acceptance"
        IN_REVIEW = "IN_REVIEW", "Doctor reviewing"
        RESPONDED = "RESPONDED", "Consultation ready"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Seen by hospital"
        DECLINED = "DECLINED", "Declined by doctor"
        CANCELLED = "CANCELLED", "Cancelled by hospital"

    class ConsentMethod(models.TextChoices):
        VERBAL = "VERBAL", "Verbal"
        WRITTEN = "WRITTEN", "Written"

    class ConsentParty(models.TextChoices):
        PATIENT = "PATIENT", "Patient"
        REPRESENTATIVE = "REPRESENTATIVE", "Patient representative"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    hospital = models.ForeignKey(
        "accounts.Hospital", on_delete=models.PROTECT, related_name="cases"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_cases"
    )
    doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="assigned_cases"
    )
    patient_name = models.CharField(max_length=160)
    patient_reference = models.CharField(max_length=60, blank=True)
    age_years = models.PositiveSmallIntegerField(validators=[MaxValueValidator(120)])
    presenting_complaint = models.TextField(max_length=3000)
    history = models.TextField(max_length=2000, blank=True)
    allergies = models.TextField(max_length=2000, blank=True)
    medications = models.TextField(max_length=2000, blank=True)
    systolic_bp = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(40), MaxValueValidator(300)]
    )
    diastolic_bp = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(20), MaxValueValidator(200)]
    )
    pulse_bpm = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(20), MaxValueValidator(300)]
    )
    respiratory_rate = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(4), MaxValueValidator(80)]
    )
    temperature_c = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("25.0")), MaxValueValidator(Decimal("45.0"))],
    )
    spo2_percent = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("50.0")), MaxValueValidator(Decimal("100.0"))],
    )
    review_mode = models.CharField(
        max_length=5, choices=ReviewMode.choices, default=ReviewMode.TEXT
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    consent_method = models.CharField(max_length=8, choices=ConsentMethod.choices)
    consent_party = models.CharField(max_length=14, choices=ConsentParty.choices)
    consent_at = models.DateTimeField()
    video_consent_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["hospital", "-created_at"]),
            models.Index(fields=["doctor", "-created_at"]),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if (self.systolic_bp is None) != (self.diastolic_bp is None):
            errors["systolic_bp"] = "Enter both blood pressure values, or leave both blank."
        elif self.systolic_bp is not None and self.systolic_bp <= self.diastolic_bp:
            errors["systolic_bp"] = (
                "Check the blood pressure values: systolic must exceed diastolic."
            )
        if (
            self.age_years is not None
            and self.age_years < 18
            and self.consent_party != self.ConsentParty.REPRESENTATIVE
        ):
            errors["consent_party"] = (
                "Record consent from a patient representative for a patient under 18."
            )
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        # Do not expose patient identifiers in incidental object logging.
        return f"Case {self.pk}"


class Response(models.Model):
    case = models.OneToOneField(Case, on_delete=models.PROTECT, related_name="response")
    doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="consultation_responses"
    )
    assessment = models.TextField(max_length=4000)
    recommendations = models.TextField(max_length=4000)
    follow_up = models.TextField(max_length=2000, blank=True)
    escalation = models.TextField(max_length=2000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class CaseEvent(models.Model):
    class Event(models.TextChoices):
        CREATED = "CREATED", "Created"
        ACCEPTED = "ACCEPTED", "Accepted"
        DECLINED = "DECLINED", "Declined"
        RESPONSE_ADDED = "RESPONSE_ADDED", "Response added"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"
        CANCELLED = "CANCELLED", "Cancelled"
        VIDEO_REQUESTED = "VIDEO_REQUESTED", "Video requested"
        VIDEO_CONSENT = "VIDEO_CONSENT", "Video consent recorded"
        FILE_UPLOADED = "FILE_UPLOADED", "Report uploaded"
        FILE_READ = "FILE_READ", "Report read"
        VIDEO_STARTED = "VIDEO_STARTED", "Video started"
        VIDEO_ENDED = "VIDEO_ENDED", "Video ended"
        READ = "READ", "Case read"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="case_events"
    )
    event = models.CharField(max_length=20, choices=Event.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class Report(models.Model):
    class State(models.TextChoices):
        PENDING = "PENDING", "Awaiting security scan"
        CLEAN = "CLEAN", "Available"
        REJECTED = "REJECTED", "Rejected"
        UNAVAILABLE = "UNAVAILABLE", "Security scan unavailable"

    class DetectedType(models.TextChoices):
        PNG = "PNG", "PNG"
        JPEG = "JPEG", "JPEG"
        PDF = "PDF", "PDF"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="reports")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_reports"
    )
    display_name = models.CharField(max_length=160)
    detected_type = models.CharField(max_length=4, choices=DetectedType.choices, blank=True)
    storage_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    size_bytes = models.PositiveIntegerField()
    digest = models.CharField(max_length=64)
    scan_attempted_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=11, choices=State.choices, default=State.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class VideoSession(models.Model):
    class CleanupState(models.TextChoices):
        PENDING = "PENDING", "Pending"
        DONE = "DONE", "Done"
        ERROR = "ERROR", "Retry needed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="video_sessions")
    room_name = models.CharField(max_length=160, unique=True)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="started_video_sessions"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    cleanup_state = models.CharField(
        max_length=7, choices=CleanupState.choices, default=CleanupState.PENDING
    )
    version = models.PositiveIntegerField(default=1)
    cleanup_attempted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["case"],
                condition=Q(ended_at__isnull=True),
                name="one_unended_video_per_case",
            )
        ]
