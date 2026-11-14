"""Account and employment-link gates, always evaluated against current records."""

from contextlib import contextmanager

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Hospital, LocumLink, User


def _eligible_current(user, hospital=None):
    if not user or not user.is_active or not user.email_verified_at:
        return False
    if user.role == User.Role.DOCTOR:
        return user.doctor_verified and user.hospital_id is None
    if user.role == User.Role.STAFF and user.hospital_id:
        if hospital is None or hospital.pk != user.hospital_id:
            hospital = Hospital.objects.filter(pk=user.hospital_id).first()
        return bool(hospital and hospital.is_approved)
    return False


def eligible(user):
    """Return current eligibility, without trusting a session/model snapshot."""
    user_id = getattr(user, "pk", None)
    if not user_id:
        return False
    current = User.objects.filter(pk=user_id).first()
    return _eligible_current(current)


def active_links():
    """Current relationship queryset, also used by bounded background workers."""
    return LocumLink.objects.filter(
        hospital__is_approved=True,
        doctor__role=User.Role.DOCTOR,
        doctor__hospital__isnull=True,
        doctor__is_active=True,
        doctor__email_verified_at__isnull=False,
        doctor__doctor_verified=True,
        state=LocumLink.State.ACTIVE,
        accepted_at__isnull=False,
    )


def active_link(hospital_id, doctor_id):
    """Read a current accepted link, with both scope approvals rechecked."""
    return active_links().filter(hospital_id=hospital_id, doctor_id=doctor_id).first()


@contextmanager
def locked_actor_scope(user_id, hospital_id=None):
    """Lock the actor then hospital, leaving link locks to the caller.

    Doctor callers supply the hospital scope. No other participant User is
    locked after the hospital, matching the case-service lock order.
    """
    with transaction.atomic():
        actor = User.objects.select_for_update().filter(pk=user_id).first()
        if not actor or not actor.is_active or not actor.email_verified_at:
            raise PermissionDenied
        scope_id = actor.hospital_id if actor.role == User.Role.STAFF else hospital_id
        hospital = Hospital.objects.select_for_update().filter(pk=scope_id).first()
        if not hospital or not hospital.is_approved or not _eligible_current(actor, hospital):
            raise PermissionDenied
        yield actor, hospital


def invite_doctor(user_id, doctor_email):
    email = doctor_email.strip().lower()
    with locked_actor_scope(user_id) as (actor, hospital):
        if actor.role != User.Role.STAFF:
            raise PermissionDenied
        doctor = User.objects.filter(email=email, role=User.Role.DOCTOR).first()
        if not _eligible_current(doctor):
            raise ValidationError("An invitation could not be sent to that account.")
        link = (
            LocumLink.objects.select_for_update().filter(hospital=hospital, doctor=doctor).first()
        )
        if link and link.state in (LocumLink.State.ACTIVE, LocumLink.State.PENDING):
            raise ValidationError("An invitation or active link already exists.")
        if link is None:
            return LocumLink.objects.create(hospital=hospital, doctor=doctor, invited_by=actor)
        link.state = LocumLink.State.PENDING
        link.invited_by = actor
        link.accepted_at = None
        link.save(update_fields=["state", "invited_by", "accepted_at"])
        return link


def change_link(user_id, link_id, action):
    if action not in {"accept", "decline", "revoke"}:
        raise PermissionDenied
    # This read locates the lock scope only; all permissions use the locked rows.
    scope = LocumLink.objects.filter(pk=link_id).values("hospital_id").first()
    if not scope:
        raise PermissionDenied
    with locked_actor_scope(user_id, scope["hospital_id"]) as (actor, hospital):
        link = LocumLink.objects.select_for_update().filter(pk=link_id, hospital=hospital).first()
        if not link:
            raise PermissionDenied
        if action == "revoke":
            if actor.role != User.Role.STAFF or actor.hospital_id != link.hospital_id:
                raise PermissionDenied
            if link.state not in (LocumLink.State.ACTIVE, LocumLink.State.PENDING):
                raise ValidationError("This link is no longer active or pending.")
            link.state = LocumLink.State.REVOKED
        else:
            if actor.role != User.Role.DOCTOR or actor.pk != link.doctor_id:
                raise PermissionDenied
            if link.state != LocumLink.State.PENDING:
                raise ValidationError("This invitation is no longer pending.")
            link.state = LocumLink.State.ACTIVE if action == "accept" else LocumLink.State.DECLINED
            link.accepted_at = timezone.now() if action == "accept" else None
        link.save(update_fields=["state", "accepted_at"])
        return link
