"""Optional LiveKit rooms with current case authorization at every boundary."""

import asyncio
import hashlib
import hmac
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Exists, F, OuterRef
from django.utils import timezone

from accounts.models import Hospital, LocumLink
from accounts.services import active_link, active_links

from .models import Case, CaseEvent, VideoSession
from .services import authorized_case


class MediaError(Exception):
    """A fixed public error without provider details or credentials."""


def configured():
    if not getattr(settings, "LIVEKIT_EXPLICIT_ROOMS_CONFIRMED", False):
        return False
    if len(getattr(settings, "LIVEKIT_API_SECRET", "")) < 32:
        return False
    if not all(
        getattr(settings, name, "")
        for name in ("LIVEKIT_URL", "LIVEKIT_API_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
    ):
        return False
    for name, scheme in (("LIVEKIT_URL", "wss"), ("LIVEKIT_API_URL", "https")):
        try:
            parsed = urlsplit(getattr(settings, name))
            if (
                parsed.scheme != scheme
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                return False
            _ = parsed.port
        except (ValueError, TypeError):
            return False
    try:
        from livekit import api  # noqa: F401
    except ImportError:
        return False
    return True


def active_session(case):
    return VideoSession.objects.filter(case=case, ended_at__isnull=True).first()


def _require_video(case):
    if (
        case.status != Case.Status.IN_REVIEW
        or case.review_mode != Case.ReviewMode.VIDEO
        or not case.consent_at
        or not case.video_consent_at
    ):
        raise PermissionDenied
    if not active_link(case.hospital_id, case.doctor_id):
        raise PermissionDenied
    if not configured():
        raise MediaError("Video is not configured. Written consultation remains available.")


async def _provider_async(action, room_name):
    import aiohttp
    from livekit import api

    async with api.LiveKitAPI(
        url=settings.LIVEKIT_API_URL,
        api_key=settings.LIVEKIT_API_KEY,
        api_secret=settings.LIVEKIT_API_SECRET,
        timeout=aiohttp.ClientTimeout(total=5),
        failover=False,
    ) as client:
        if action == "create":
            return await client.room.create_room(
                api.CreateRoomRequest(
                    name=room_name,
                    max_participants=2,
                    empty_timeout=120,
                    departure_timeout=20,
                    replay_enabled=False,
                )
            )
        return await client.room.delete_room(api.DeleteRoomRequest(room=room_name))


async def _bounded_provider(action, room_name):
    return await asyncio.wait_for(_provider_async(action, room_name), timeout=5)


def _provider(action, room_name):
    if not configured():
        raise MediaError("Video is not configured.")
    try:
        return async_to_sync(_bounded_provider)(action, room_name)
    except Exception as exc:
        # Not-found means the room is already absent, which completes deletion.
        if action == "delete" and getattr(exc, "code", None) == "not_found":
            return None
        raise MediaError(
            "The video service is unavailable. Please use written consultation."
        ) from exc


def start_session(user_id, case_id):
    failed = False
    with authorized_case(user_id, case_id) as (user, case, _link):
        _require_video(case)
        if user.role != "DOCTOR":
            raise PermissionDenied
        session = (
            VideoSession.objects.select_for_update()
            .filter(case=case, ended_at__isnull=True)
            .first()
        )
        if session:
            return session
        if (
            VideoSession.objects.filter(case=case)
            .exclude(cleanup_state=VideoSession.CleanupState.DONE)
            .exists()
        ):
            raise MediaError("A previous session is awaiting cleanup. Try again after it ends.")
        session = VideoSession.objects.create(
            case=case, started_by=user, room_name=f"lb-{secrets.token_hex(24)}"
        )
        try:
            _provider("create", session.room_name)
        except MediaError:
            # The provider may have accepted a request before timing out. Persist
            # a cleanup target even when no join token was ever issued.
            session.ended_at = timezone.now()
            session.cleanup_state = VideoSession.CleanupState.ERROR
            session.version += 1
            session.save(update_fields=["ended_at", "cleanup_state", "version"])
            failed = True
        else:
            CaseEvent.objects.create(case=case, actor=user, event=CaseEvent.Event.VIDEO_STARTED)
    if failed:
        raise MediaError("The video service is unavailable. Please use written consultation.")
    return session


def issue_token(user_id, case_id):
    with authorized_case(user_id, case_id) as (user, case, _link):
        _require_video(case)
        session = (
            VideoSession.objects.select_for_update()
            .filter(case=case, ended_at__isnull=True)
            .first()
        )
        if not session:
            raise MediaError("The doctor has not started a video session.")
        if session.created_at <= timezone.now() - timedelta(hours=1):
            raise MediaError("This session has expired. End it before starting another.")
        from livekit import api

        # Stable within one call, opaque outside it; no patient or account name.
        identity = (
            "participant-"
            + hmac.new(
                settings.LIVEKIT_API_SECRET.encode(),
                f"{session.pk}:{user.pk}".encode(),
                hashlib.sha256,
            ).hexdigest()[:32]
        )
        token = (
            api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
            .with_identity(identity)
            .with_ttl(timedelta(seconds=60))
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=session.room_name,
                    room_create=False,
                    room_list=False,
                    room_admin=False,
                    room_record=False,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=False,
                    can_publish_sources=["camera", "microphone"],
                    can_update_own_metadata=False,
                    ingress_admin=False,
                    recorder=False,
                    agent=False,
                    can_manage_agent_session=False,
                    can_subscribe_metrics=False,
                )
            )
            .to_jwt()
        )
        return {"url": settings.LIVEKIT_URL, "token": token, "version": session.version}


def session_status(user_id, case_id):
    with authorized_case(user_id, case_id) as (_user, case, _link):
        _require_video(case)
        session = (
            VideoSession.objects.select_for_update()
            .filter(case=case, ended_at__isnull=True)
            .first()
        )
        active = bool(session and session.created_at > timezone.now() - timedelta(hours=1))
        return {"active": active, "version": session.version if active else None}


def end_session(user_id, case_id):
    with authorized_case(user_id, case_id) as (user, case, _link):
        _require_video(case)
        session = (
            VideoSession.objects.select_for_update()
            .filter(case=case, ended_at__isnull=True)
            .first()
        )
        if not session:
            session = (
                VideoSession.objects.select_for_update()
                .filter(case=case)
                .exclude(cleanup_state=VideoSession.CleanupState.DONE)
                .order_by("-created_at")
                .first()
            )
            if not session:
                return {"ended": True, "cleanup": "DONE"}
        else:
            session.ended_at = timezone.now()
            session.cleanup_state = VideoSession.CleanupState.PENDING
            session.version += 1
            session.save(update_fields=["ended_at", "cleanup_state", "version"])
            CaseEvent.objects.create(case=case, actor=user, event=CaseEvent.Event.VIDEO_ENDED)
    # Durable end is committed before provider I/O. Status polling now removes
    # local tracks even if provider deletion needs a worker retry.
    state = _cleanup(session.pk)
    return {"ended": True, "cleanup": state}


def _cleanup(session_id):
    with transaction.atomic():
        session = (
            VideoSession.objects.select_for_update()
            .filter(pk=session_id, ended_at__isnull=False)
            .first()
        )
        if not session:
            return VideoSession.CleanupState.PENDING
        if session.cleanup_state == VideoSession.CleanupState.DONE:
            return VideoSession.CleanupState.DONE
        # Persist the attempt before provider I/O, so failed requests rotate
        # behind other cleanup work on the next worker batch.
        attempted_at = timezone.now()
        session.cleanup_attempted_at = attempted_at
        session.save(update_fields=["cleanup_attempted_at"])
    try:
        _provider("delete", session.room_name)
        state = VideoSession.CleanupState.DONE
    except MediaError:
        state = VideoSession.CleanupState.ERROR
    VideoSession.objects.filter(
        pk=session.pk,
        ended_at__isnull=False,
        version=session.version,
        cleanup_attempted_at=attempted_at,
    ).update(cleanup_state=state)
    return state


def cleanup_sessions(limit=10):
    """End revoked/expired cases and retry provider deletion; return counts only."""
    counts = {"checked": 0, "ended": 0, "done": 0, "error": 0}
    deadline = timezone.now() - timedelta(hours=1)
    current_link = active_links().filter(
        hospital_id=OuterRef("case__hospital_id"), doctor_id=OuterRef("case__doctor_id")
    )
    candidates = list(
        VideoSession.objects.annotate(has_current_link=Exists(current_link))
        .exclude(ended_at__isnull=False, cleanup_state=VideoSession.CleanupState.DONE)
        # Healthy active calls need no provider cleanup and must not consume
        # the bounded worker batch ahead of revoked or expired sessions.
        .exclude(
            ended_at__isnull=True,
            has_current_link=True,
            case__status=Case.Status.IN_REVIEW,
            case__review_mode=Case.ReviewMode.VIDEO,
            case__consent_at__isnull=False,
            case__video_consent_at__isnull=False,
            created_at__gt=deadline,
        )
        .order_by(F("cleanup_attempted_at").asc(nulls_first=True), "created_at", "pk")
        .values("pk", "case_id", "case__hospital_id", "case__doctor_id")[
            : max(0, min(int(limit), 100))
        ]
    )
    for candidate in candidates:
        cleanup_id = None
        with transaction.atomic():
            # System maintenance has no acting User; take the remaining shared
            # scope locks in the same hospital -> link -> case -> video order.
            hospital = Hospital.objects.select_for_update().get(pk=candidate["case__hospital_id"])
            list(
                LocumLink.objects.select_for_update().filter(
                    hospital=hospital, doctor_id=candidate["case__doctor_id"]
                )
            )
            case = Case.objects.select_for_update().get(pk=candidate["case_id"])
            session = VideoSession.objects.select_for_update().get(pk=candidate["pk"])
            counts["checked"] += 1
            valid = (
                hospital.is_approved
                and case.status == Case.Status.IN_REVIEW
                and case.review_mode == Case.ReviewMode.VIDEO
                and case.consent_at
                and case.video_consent_at
                and active_link(case.hospital_id, case.doctor_id)
                and session.created_at > timezone.now() - timedelta(hours=1)
            )
            if session.ended_at is None and not valid:
                session.ended_at = timezone.now()
                session.version += 1
                session.cleanup_state = VideoSession.CleanupState.PENDING
                session.save(update_fields=["ended_at", "version", "cleanup_state"])
                counts["ended"] += 1
            if session.ended_at is not None:
                cleanup_id = session.pk
        if cleanup_id:
            state = _cleanup(cleanup_id)
            counts["done" if state == VideoSession.CleanupState.DONE else "error"] += 1
    return counts
