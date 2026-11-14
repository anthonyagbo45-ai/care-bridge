"""Synthetic media authorization and provider doubles; no real call is claimed."""

import secrets
from datetime import timedelta
from unittest.mock import call, patch

import jwt
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Hospital, LocumLink, User
from care import media
from care.models import Case, CaseEvent, VideoSession
from care.services import Conflict, finalize_response


class MediaTests(TestCase):
    def test_optional_bad_video_configuration_does_not_break_written_pages(self):
        self.client.force_login(self.doctor)
        with self.settings(LIVEKIT_URL="wss://[invalid"):
            self.assertFalse(media.configured())
            response = self.client.get(reverse("care:case-detail", args=[self.case.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Video is not configured")
        with self.settings(LIVEKIT_API_SECRET="short"):
            self.assertFalse(media.configured())

    def setUp(self):
        self.secret = secrets.token_urlsafe(48)
        self.enterContext(
            self.settings(
                LIVEKIT_URL="wss://media.example.invalid",
                LIVEKIT_API_URL="https://media.example.invalid",
                LIVEKIT_API_KEY="synthetic-media-key",
                LIVEKIT_API_SECRET=self.secret,
                LIVEKIT_EXPLICIT_ROOMS_CONFIRMED=True,
            )
        )
        self.hospital = Hospital.objects.create(name="Fictional media clinic", is_approved=True)
        self.staff = User.objects.create_user(
            "media-staff@example.invalid",
            "synthetic-test-only-password",
            role="STAFF",
            hospital=self.hospital,
            email_verified_at=timezone.now(),
        )
        self.doctor = User.objects.create_user(
            "media-doctor@example.invalid",
            "synthetic-test-only-password",
            role="DOCTOR",
            doctor_verified=True,
            email_verified_at=timezone.now(),
        )
        self.outsider = User.objects.create_user(
            "media-outsider@example.invalid",
            "synthetic-test-only-password",
            role="DOCTOR",
            doctor_verified=True,
            email_verified_at=timezone.now(),
        )
        self.link = LocumLink.objects.create(
            hospital=self.hospital,
            doctor=self.doctor,
            invited_by=self.staff,
            state="ACTIVE",
            accepted_at=timezone.now(),
        )
        self.case = Case.objects.create(
            hospital=self.hospital,
            created_by=self.staff,
            doctor=self.doctor,
            patient_name="Fictional video patient",
            age_years=40,
            presenting_complaint="Synthetic clinical handover",
            review_mode="VIDEO",
            status="IN_REVIEW",
            accepted_at=timezone.now(),
            consent_at=timezone.now(),
            consent_method="VERBAL",
            consent_party="PATIENT",
            video_consent_at=timezone.now(),
        )

    def start(self):
        with patch("care.media._provider") as provider:
            session = media.start_session(self.doctor.pk, self.case.pk)
        provider.assert_called_once_with("create", session.room_name)
        return session

    def test_real_sdk_generates_short_minimal_participant_grants(self):
        session = self.start()
        result = media.issue_token(self.staff.pk, self.case.pk)
        claims = jwt.decode(
            result["token"], self.secret, algorithms=["HS256"], issuer="synthetic-media-key"
        )
        self.assertEqual(claims["video"]["room"], session.room_name)
        self.assertLessEqual(claims["exp"] - claims["nbf"], 60)
        self.assertTrue(claims["video"]["roomJoin"])
        for grant in ("roomCreate", "roomAdmin", "roomRecord", "canPublishData"):
            self.assertFalse(claims["video"][grant])
        self.assertNotIn(self.case.patient_name, str(claims))
        self.assertNotIn(self.staff.email, str(claims))
        self.assertNotEqual(
            claims["sub"],
            jwt.decode(
                media.issue_token(self.doctor.pk, self.case.pk)["token"],
                self.secret,
                algorithms=["HS256"],
                issuer="synthetic-media-key",
            )["sub"],
        )

    def test_only_assigned_verified_doctor_starts_and_other_party_can_join(self):
        for actor in (self.staff, self.outsider):
            with self.assertRaises(PermissionDenied):
                media.start_session(actor.pk, self.case.pk)
        self.start()
        self.assertIn("token", media.issue_token(self.staff.pk, self.case.pk))
        with self.assertRaises(PermissionDenied):
            media.issue_token(self.outsider.pk, self.case.pk)

    def test_provider_failure_retains_cleanup_target_and_never_issues_a_token(self):
        with patch("care.media._provider", side_effect=media.MediaError("Unavailable")):
            with self.assertRaises(media.MediaError):
                media.start_session(self.doctor.pk, self.case.pk)
        session = VideoSession.objects.get()
        self.assertIsNotNone(session.ended_at)
        self.assertEqual(session.cleanup_state, "ERROR")
        with self.assertRaises(media.MediaError):
            media.issue_token(self.doctor.pk, self.case.pk)
        with self.assertRaises(media.MediaError):
            self.start()

    def test_either_participant_ends_and_provider_cleanup_is_not_falsely_confirmed(self):
        self.start()
        with patch("care.media._provider", side_effect=media.MediaError("Unavailable")):
            result = media.end_session(self.staff.pk, self.case.pk)
            self.assertEqual(result, {"ended": True, "cleanup": "ERROR"})
            self.assertEqual(media.end_session(self.doctor.pk, self.case.pk)["cleanup"], "ERROR")
        self.assertFalse(media.session_status(self.staff.pk, self.case.pk)["active"])
        with patch("care.media._provider"):
            self.assertEqual(media.end_session(self.doctor.pk, self.case.pk)["cleanup"], "DONE")
        self.assertEqual(CaseEvent.objects.filter(event="VIDEO_ENDED").count(), 1)
        with self.assertRaises(media.MediaError):
            media.issue_token(self.staff.pk, self.case.pk)

    def test_revocation_denies_old_routes_and_cleanup_worker_ends_remote_room(self):
        session = self.start()
        LocumLink.objects.filter(pk=self.link.pk).update(state="REVOKED")
        with self.assertRaises(PermissionDenied):
            media.issue_token(self.doctor.pk, self.case.pk)
        self.client.force_login(self.doctor)
        self.assertEqual(
            self.client.get(reverse("care:video-status", args=[self.case.pk])).status_code, 403
        )
        with patch("care.media._provider") as provider:
            counts = media.cleanup_sessions()
        provider.assert_called_once_with("delete", session.room_name)
        self.assertEqual(counts["ended"], 1)
        session.refresh_from_db()
        self.assertEqual(session.cleanup_state, "DONE")

    def test_expired_call_stops_status_and_tokens_and_is_reclaimed(self):
        session = self.start()
        VideoSession.objects.filter(pk=session.pk).update(
            created_at=timezone.now() - timedelta(hours=2)
        )
        self.assertFalse(media.session_status(self.staff.pk, self.case.pk)["active"])
        with self.assertRaises(media.MediaError):
            media.issue_token(self.staff.pk, self.case.pk)
        with patch("care.media._provider"):
            self.assertEqual(media.cleanup_sessions()["ended"], 1)

    def test_healthy_older_call_does_not_take_revoked_call_cleanup_slot(self):
        healthy = self.start()
        VideoSession.objects.filter(pk=healthy.pk).update(
            created_at=timezone.now() - timedelta(minutes=30)
        )
        LocumLink.objects.create(
            hospital=self.hospital,
            doctor=self.outsider,
            invited_by=self.staff,
            state="REVOKED",
            accepted_at=timezone.now(),
        )
        revoked_case = Case.objects.create(
            hospital=self.hospital,
            created_by=self.staff,
            doctor=self.outsider,
            patient_name="Fictional revoked-call patient",
            age_years=40,
            presenting_complaint="Synthetic clinical handover",
            review_mode="VIDEO",
            status="IN_REVIEW",
            accepted_at=timezone.now(),
            consent_at=timezone.now(),
            consent_method="VERBAL",
            consent_party="PATIENT",
            video_consent_at=timezone.now(),
        )
        revoked = VideoSession.objects.create(
            case=revoked_case,
            started_by=self.outsider,
            room_name="synthetic-revoked-room",
        )
        with patch("care.media._provider") as provider:
            counts = media.cleanup_sessions(limit=1)
        provider.assert_called_once_with("delete", revoked.room_name)
        self.assertEqual(counts, {"checked": 1, "ended": 1, "done": 1, "error": 0})
        healthy.refresh_from_db()
        revoked.refresh_from_db()
        self.assertIsNone(healthy.ended_at)
        self.assertIsNone(healthy.cleanup_attempted_at)
        self.assertIsNotNone(revoked.ended_at)
        self.assertIsNotNone(revoked.cleanup_attempted_at)
        self.assertEqual(revoked.cleanup_state, "DONE")

    def test_failed_cleanup_rotates_to_another_session_in_the_next_batch(self):
        first = self.start()
        VideoSession.objects.filter(pk=first.pk).update(
            created_at=timezone.now() - timedelta(minutes=30),
            ended_at=timezone.now(),
            cleanup_state="ERROR",
        )
        second = VideoSession.objects.create(
            case=self.case,
            started_by=self.doctor,
            room_name="synthetic-second-cleanup-room",
            ended_at=timezone.now(),
            cleanup_state="ERROR",
        )
        with patch(
            "care.media._provider", side_effect=[media.MediaError("Unavailable"), None]
        ) as provider:
            self.assertEqual(media.cleanup_sessions(limit=1)["error"], 1)
            self.assertEqual(media.cleanup_sessions(limit=1)["done"], 1)
        self.assertEqual(
            provider.call_args_list,
            [call("delete", first.room_name), call("delete", second.room_name)],
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.cleanup_state, "ERROR")
        self.assertEqual(second.cleanup_state, "DONE")
        self.assertIsNotNone(first.cleanup_attempted_at)
        self.assertIsNotNone(second.cleanup_attempted_at)
        self.assertLess(first.cleanup_attempted_at, second.cleanup_attempted_at)

    def test_text_case_or_missing_video_consent_cannot_obtain_media(self):
        for updates in ({"review_mode": "TEXT"}, {"video_consent_at": None}):
            with self.subTest(updates=updates):
                Case.objects.filter(pk=self.case.pk).update(**updates)
                with self.assertRaises(PermissionDenied):
                    media.start_session(self.doctor.pk, self.case.pk)
                Case.objects.filter(pk=self.case.pk).update(
                    review_mode="VIDEO", video_consent_at=timezone.now()
                )

    def test_unconfigured_video_is_honest_and_csrf_is_required(self):
        self.client.force_login(self.doctor)
        with self.settings(LIVEKIT_API_KEY=""):
            page = self.client.get(reverse("care:video", args=[self.case.pk]))
            self.assertContains(page, "Video is not configured")
            self.assertNotContains(page, "js/video.js")
            self.assertEqual(
                self.client.post(reverse("care:video-start", args=[self.case.pk])).status_code, 503
            )
        page = self.client.get(reverse("care:video", args=[self.case.pk]))
        self.assertContains(page, "js/video.js")
        self.assertContains(page, "livekit-client.umd.js")
        from django.test import Client

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.doctor)
        self.assertEqual(
            csrf_client.post(reverse("care:video-token", args=[self.case.pk])).status_code, 403
        )

    def test_response_waits_for_provider_cleanup_not_only_a_local_ended_flag(self):
        session = self.start()
        VideoSession.objects.filter(pk=session.pk).update(
            ended_at=timezone.now(), cleanup_state="ERROR"
        )
        with self.assertRaises(Conflict):
            finalize_response(
                self.doctor.pk,
                self.case.pk,
                {
                    "assessment": "Fictional assessment",
                    "recommendations": "Fictional advice",
                    "attestation": True,
                    "current_password": "synthetic-test-only-password",
                },
            )
