"""Small consultation workflow with authorization repeated at every boundary."""

from contextlib import contextmanager

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import Hospital, LocumLink, User

from .models import Case, CaseEvent, Response, VideoSession


class Conflict(Exception):
    """The requested workflow transition is no longer available."""


CASE_FIELDS = (
    "patient_name",
    "patient_reference",
    "age_years",
    "presenting_complaint",
    "history",
    "allergies",
    "medications",
    "systolic_bp",
    "diastolic_bp",
    "pulse_bpm",
    "respiratory_rate",
    "temperature_c",
    "spo2_percent",
    "review_mode",
    "consent_method",
    "consent_party",
)
RESPONSE_FIELDS = ("assessment", "recommendations", "follow_up", "escalation")


def _actor(user_id):
    user = User.objects.select_for_update().filter(pk=user_id).first()
    if not user or not user.is_active or not user.email_verified_at:
        raise PermissionDenied
    if user.role == User.Role.DOCTOR:
        if not user.doctor_verified or user.hospital_id:
            raise PermissionDenied
    elif user.role != User.Role.STAFF or not user.hospital_id:
        raise PermissionDenied
    return user


def _hospital(hospital_id):
    hospital = Hospital.objects.select_for_update().filter(pk=hospital_id).first()
    if not hospital or not hospital.is_approved:
        raise PermissionDenied
    return hospital


def _active_doctor(doctor_id):
    # Other participants are a fresh snapshot; never acquire a second User lock
    # after the hospital lock, which would invert the shared lock order.
    return User.objects.filter(
        pk=doctor_id,
        role=User.Role.DOCTOR,
        hospital__isnull=True,
        is_active=True,
        email_verified_at__isnull=False,
        doctor_verified=True,
    ).first()


def require_case(user_id, case_id, write=False):
    """Return (fresh actor, scoped case, link), using the common lock order.

    Call within ``authorized_case`` for operations that must retain the locks.
    A standalone call is a single atomic authorization/read operation.
    ``write`` is accepted for callers that distinguish read and write intent;
    both use the same strict scope and locks.
    """
    if not connection.in_atomic_block:
        with transaction.atomic():
            return require_case(user_id, case_id, write=write)
    user = _actor(user_id)
    scope = Case.objects.filter(pk=case_id).values("hospital_id", "doctor_id").first()
    if not scope:
        raise PermissionDenied
    if user.role == User.Role.STAFF and user.hospital_id != scope["hospital_id"]:
        raise PermissionDenied
    if user.role == User.Role.DOCTOR and user.pk != scope["doctor_id"]:
        raise PermissionDenied
    hospital = _hospital(scope["hospital_id"])
    link = (
        LocumLink.objects.select_for_update()
        .filter(
            hospital_id=hospital.pk,
            doctor_id=scope["doctor_id"],
        )
        .first()
    )
    if user.role == User.Role.DOCTOR and (
        not link or link.state != LocumLink.State.ACTIVE or not link.accepted_at
    ):
        raise PermissionDenied
    case = (
        Case.objects.select_for_update()
        .filter(
            pk=case_id,
            hospital_id=hospital.pk,
            doctor_id=scope["doctor_id"],
        )
        .first()
    )
    if not case:
        raise PermissionDenied
    # Hospital records remain visible to their own team after a locum link is
    # revoked. The doctor loses access immediately at this boundary.
    case.hospital = hospital
    return user, case, link


@contextmanager
def authorized_case(user_id, case_id):
    with transaction.atomic():
        yield require_case(user_id, case_id, write=True)


@contextmanager
def authorized_list(user_id):
    with transaction.atomic():
        user = _actor(user_id)
        if user.role == User.Role.STAFF:
            hospital = _hospital(user.hospital_id)
            cases = Case.objects.filter(hospital=hospital)
        else:
            cases = Case.objects.filter(
                doctor=user,
                hospital__is_approved=True,
                hospital__locum_links__doctor=user,
                hospital__locum_links__state=LocumLink.State.ACTIVE,
                hospital__locum_links__accepted_at__isnull=False,
            )
        yield user, cases.select_related("doctor", "hospital").order_by("-created_at")


def create_case(user_id, data):
    with transaction.atomic():
        user = _actor(user_id)
        if user.role != User.Role.STAFF:
            raise PermissionDenied
        hospital = _hospital(user.hospital_id)
        selected = data.get("doctor")
        doctor_id = getattr(selected, "pk", selected)
        link = (
            LocumLink.objects.select_for_update()
            .filter(
                hospital=hospital,
                doctor_id=doctor_id,
                state=LocumLink.State.ACTIVE,
                accepted_at__isnull=False,
            )
            .first()
        )
        doctor = _active_doctor(doctor_id)
        if not link or not doctor:
            raise PermissionDenied
        if not data.get("consent_confirmed"):
            raise ValidationError({"consent_confirmed": "Confirm that consent has been recorded."})
        if data.get("review_mode") == Case.ReviewMode.VIDEO and not data.get("video_consent"):
            raise ValidationError(
                {"video_consent": "Confirm consent for video before requesting it."}
            )
        now = timezone.now()
        case = Case(
            hospital=hospital,
            created_by=user,
            doctor=doctor,
            consent_at=now,
            video_consent_at=now if data.get("video_consent") else None,
            **{field: data[field] for field in CASE_FIELDS if field in data},
        )
        case.full_clean()
        case.save()
        CaseEvent.objects.create(case=case, actor=user, event=CaseEvent.Event.CREATED)
        return case


def perform_action(user_id, case_id, action, *, video_consent=False):
    with authorized_case(user_id, case_id) as (user, case, _link):
        doctor = user.role == User.Role.DOCTOR
        staff = user.role == User.Role.STAFF
        now = timezone.now()
        if action == "accept":
            if not doctor:
                raise PermissionDenied
            if case.status != Case.Status.PENDING:
                raise Conflict("This case is no longer awaiting acceptance.")
            case.status, case.accepted_at = Case.Status.IN_REVIEW, now
            event = CaseEvent.Event.ACCEPTED
        elif action == "decline":
            if not doctor:
                raise PermissionDenied
            if case.status != Case.Status.PENDING:
                raise Conflict("This case is no longer awaiting acceptance.")
            case.status = Case.Status.DECLINED
            event = CaseEvent.Event.DECLINED
        elif action == "cancel":
            if not staff:
                raise PermissionDenied
            if case.status not in (Case.Status.PENDING, Case.Status.IN_REVIEW):
                raise Conflict("This case can no longer be cancelled.")
            if VideoSession.objects.filter(case=case, ended_at__isnull=True).exists():
                raise Conflict("End the video call before cancelling this case.")
            case.status = Case.Status.CANCELLED
            event = CaseEvent.Event.CANCELLED
        elif action == "acknowledge":
            if not staff:
                raise PermissionDenied
            if (
                case.status != Case.Status.RESPONDED
                or not Response.objects.filter(case=case).exists()
            ):
                raise Conflict("There is no new consultation to acknowledge.")
            case.status, case.acknowledged_at = Case.Status.ACKNOWLEDGED, now
            event = CaseEvent.Event.ACKNOWLEDGED
        elif action == "request-video":
            if not doctor:
                raise PermissionDenied
            if case.status != Case.Status.IN_REVIEW:
                raise Conflict("Accept the case before requesting video.")
            if case.review_mode == Case.ReviewMode.VIDEO:
                raise Conflict("Video has already been requested.")
            case.review_mode = Case.ReviewMode.VIDEO
            event = CaseEvent.Event.VIDEO_REQUESTED
        elif action == "video-consent":
            if not staff:
                raise PermissionDenied
            if case.status != Case.Status.IN_REVIEW or case.review_mode != Case.ReviewMode.VIDEO:
                raise Conflict("Video consent is not currently requested.")
            if not video_consent:
                raise ValidationError("Confirm that consent for video has been recorded.")
            if case.video_consent_at:
                raise Conflict("Video consent is already recorded.")
            case.video_consent_at = now
            event = CaseEvent.Event.VIDEO_CONSENT
        else:
            raise PermissionDenied
        case.save()
        CaseEvent.objects.create(case=case, actor=user, event=event)
        return case


def finalize_response(user_id, case_id, data):
    with authorized_case(user_id, case_id) as (user, case, _link):
        if user.role != User.Role.DOCTOR:
            raise PermissionDenied
        if case.status != Case.Status.IN_REVIEW or Response.objects.filter(case=case).exists():
            raise Conflict("A final response cannot be added in the current case state.")
        if not case.consent_at or (
            case.age_years < 18 and case.consent_party != Case.ConsentParty.REPRESENTATIVE
        ):
            raise Conflict("Recorded consent is required before responding.")
        if not data.get("attestation"):
            raise ValidationError(
                {"attestation": "Confirm that this is your final written consultation."}
            )
        if not user.check_password(data.get("current_password", "")):
            raise ValidationError({"current_password": "The current password was not accepted."})
        if (
            VideoSession.objects.filter(case=case)
            .filter(Q(ended_at__isnull=True) | ~Q(cleanup_state=VideoSession.CleanupState.DONE))
            .exists()
        ):
            raise Conflict("End the active video call before confirming the written consultation.")
        response = Response(
            case=case, doctor=user, **{field: data.get(field, "") for field in RESPONSE_FIELDS}
        )
        response.full_clean()
        response.save()
        case.status = Case.Status.RESPONDED
        case.save(update_fields=["status", "updated_at"])
        CaseEvent.objects.create(case=case, actor=user, event=CaseEvent.Event.RESPONSE_ADDED)
        return response
