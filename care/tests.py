from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Hospital, LocumLink, User

from .forms import CaseForm
from .models import Case, CaseEvent, Response, VideoSession
from .services import Conflict, authorized_case, create_case, finalize_response, perform_action


class ConsultationTests(TestCase):
    """Disposable, fictional consultation records; no external calls or emails."""

    password = "Fictional-test-passphrase-704!"

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def test_real_password_confirmation_throttle_denies_repeated_attempts(self):
        case = self.new_case()
        self.accept(case)
        self.client.force_login(self.doctor)
        url = reverse("care:case-respond", args=[case.pk])
        for _ in range(6):
            self.assertEqual(
                self.client.post(url, self.final_data(current_password="wrong")).status_code, 400
            )
        blocked = self.client.post(url, self.final_data())
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("no-store", blocked["Cache-Control"])
        self.assertFalse(Response.objects.exists())

    def test_form_policy_preserves_same_origin_csrf_protection(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("care:case-create"))
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        client.get(reverse("care:case-create"))
        data = {**self.case_data(), "csrfmiddlewaretoken": client.cookies["csrftoken"].value}
        self.assertEqual(
            client.post(reverse("care:case-create"), data, HTTP_ORIGIN="null").status_code, 403
        )

    @classmethod
    def setUpTestData(cls):
        cls.hospital = Hospital.objects.create(name="Fictional Cedar Hospital", is_approved=True)
        cls.other_hospital = Hospital.objects.create(
            name="Fictional Elm Hospital", is_approved=True
        )
        cls.staff = User.objects.create_user(
            email="cedar@example.test",
            password=cls.password,
            first_name="Cedar Team",
            role=User.Role.STAFF,
            hospital=cls.hospital,
            email_verified_at=timezone.now(),
        )
        cls.other_staff = User.objects.create_user(
            email="elm@example.test",
            password=cls.password,
            role=User.Role.STAFF,
            hospital=cls.other_hospital,
            email_verified_at=timezone.now(),
        )
        cls.doctor = User.objects.create_user(
            email="doctor@example.test",
            password=cls.password,
            first_name="Fictional Doctor",
            role=User.Role.DOCTOR,
            email_verified_at=timezone.now(),
            doctor_verified=True,
            registration_number="FICTIONAL-001",
        )
        cls.other_doctor = User.objects.create_user(
            email="other-doctor@example.test",
            password=cls.password,
            role=User.Role.DOCTOR,
            email_verified_at=timezone.now(),
            doctor_verified=True,
        )
        cls.link = LocumLink.objects.create(
            hospital=cls.hospital,
            doctor=cls.doctor,
            invited_by=cls.staff,
            state=LocumLink.State.ACTIVE,
            accepted_at=timezone.now(),
        )
        cls.other_link = LocumLink.objects.create(
            hospital=cls.other_hospital,
            doctor=cls.other_doctor,
            invited_by=cls.other_staff,
            state=LocumLink.State.ACTIVE,
            accepted_at=timezone.now(),
        )

    def case_data(self, **overrides):
        data = {
            "doctor": self.doctor,
            "patient_name": "Fictional Adult",
            "patient_reference": "SYNTHETIC-42",
            "age_years": 42,
            "presenting_complaint": "Fictional non-emergency review request.",
            "history": "",
            "allergies": "",
            "medications": "",
            "systolic_bp": 120,
            "diastolic_bp": 80,
            "pulse_bpm": 75,
            "respiratory_rate": 16,
            "temperature_c": "36.8",
            "spo2_percent": "98.0",
            "review_mode": Case.ReviewMode.TEXT,
            "consent_method": Case.ConsentMethod.VERBAL,
            "consent_party": Case.ConsentParty.PATIENT,
            "consent_confirmed": True,
            "video_consent": False,
        }
        data.update(overrides)
        return data

    def final_data(self, **overrides):
        data = {
            "assessment": "Fictional clinician assessment.",
            "recommendations": "Fictional clinician recommendations.",
            "follow_up": "Fictional follow-up.",
            "escalation": "Onsite escalation if needed.",
            "current_password": self.password,
            "attestation": True,
        }
        data.update(overrides)
        return data

    def new_case(self, **overrides):
        return create_case(self.staff.pk, self.case_data(**overrides))

    def accept(self, case):
        return perform_action(self.doctor.pk, case.pk, "accept")

    def test_complete_written_journey_and_attributed_acknowledgement(self):
        case = self.new_case()
        self.assertEqual(case.status, Case.Status.PENDING)
        self.accept(case)
        response = finalize_response(self.doctor.pk, case.pk, self.final_data())
        perform_action(self.staff.pk, case.pk, "acknowledge")
        case.refresh_from_db()
        self.assertEqual(case.status, Case.Status.ACKNOWLEDGED)
        self.assertIsNotNone(case.accepted_at)
        self.assertIsNotNone(case.acknowledged_at)
        self.assertEqual(response.doctor_id, self.doctor.pk)
        self.assertEqual(
            list(case.events.values_list("event", "actor_id")),
            [
                (CaseEvent.Event.CREATED, self.staff.pk),
                (CaseEvent.Event.ACCEPTED, self.doctor.pk),
                (CaseEvent.Event.RESPONSE_ADDED, self.doctor.pk),
                (CaseEvent.Event.ACKNOWLEDGED, self.staff.pk),
            ],
        )
        self.assertEqual(
            {field.name for field in CaseEvent._meta.fields},
            {"id", "case", "actor", "event", "created_at"},
        )

    def test_hospital_cannot_inject_another_hospital_or_workflow_fields(self):
        data = self.case_data(
            hospital=self.other_hospital,
            created_by=self.other_staff,
            status=Case.Status.ACKNOWLEDGED,
        )
        case = create_case(self.staff.pk, data)
        self.assertEqual(case.hospital_id, self.hospital.pk)
        self.assertEqual(case.created_by_id, self.staff.pk)
        self.assertEqual(case.status, Case.Status.PENDING)
        with self.assertRaises(PermissionDenied):
            create_case(self.staff.pk, self.case_data(doctor=self.other_doctor))
        form = CaseForm(
            data={**self.case_data(), "doctor": str(self.other_doctor.pk)}, user=self.staff
        )
        self.assertFalse(form.is_valid())
        self.assertIn("doctor", form.errors)

    def test_patient_consent_and_source_plausibility_are_required(self):
        for overrides in (
            {"consent_confirmed": False},
            {"age_years": 10},
            {"systolic_bp": 120, "diastolic_bp": None},
            {"systolic_bp": 60, "diastolic_bp": 80},
            {"pulse_bpm": 900},
            {"spo2_percent": "101.0"},
            {"review_mode": Case.ReviewMode.VIDEO},
        ):
            with self.subTest(fields=tuple(overrides)), self.assertRaises(ValidationError):
                self.new_case(**overrides)
        minor = self.new_case(age_years=10, consent_party=Case.ConsentParty.REPRESENTATIVE)
        self.assertEqual(minor.age_years, 10)
        self.assertEqual(Case.objects.count(), 1)

    def test_scope_uses_current_actor_role_hospital_and_link(self):
        case = self.new_case()
        for actor in (self.other_staff, self.other_doctor):
            with self.subTest(actor=actor.pk), self.assertRaises(PermissionDenied):
                with authorized_case(actor.pk, case.pk):
                    self.fail("Foreign scope was accepted")
        User.objects.filter(pk=self.staff.pk).update(hospital=self.other_hospital)
        with self.assertRaises(PermissionDenied):
            with authorized_case(self.staff.pk, case.pk):
                pass
        User.objects.filter(pk=self.staff.pk).update(hospital=self.hospital)
        LocumLink.objects.filter(pk=self.link.pk).update(state=LocumLink.State.REVOKED)
        with self.assertRaises(PermissionDenied):
            with authorized_case(self.doctor.pk, case.pk):
                pass
        with authorized_case(self.staff.pk, case.pk) as (fresh, retained, _link):
            self.assertEqual(fresh.hospital_id, self.hospital.pk)
            self.assertEqual(retained.pk, case.pk)

    def test_current_verification_and_approval_are_required_without_superuser_bypass(self):
        case = self.new_case()
        for changes in (
            {"doctor_verified": False},
            {"is_active": False},
            {"email_verified_at": None},
        ):
            User.objects.filter(pk=self.doctor.pk).update(**changes)
            with self.subTest(changes=tuple(changes)), self.assertRaises(PermissionDenied):
                with authorized_case(self.doctor.pk, case.pk):
                    pass
            User.objects.filter(pk=self.doctor.pk).update(
                doctor_verified=True, is_active=True, email_verified_at=timezone.now()
            )
        Hospital.objects.filter(pk=self.hospital.pk).update(is_approved=False)
        with self.assertRaises(PermissionDenied):
            with authorized_case(self.staff.pk, case.pk):
                pass
        Hospital.objects.filter(pk=self.hospital.pk).update(is_approved=True)
        User.objects.filter(pk=self.other_staff.pk).update(is_staff=True, is_superuser=True)
        with self.assertRaises(PermissionDenied):
            with authorized_case(self.other_staff.pk, case.pk):
                pass

    def test_active_without_acceptance_timestamp_does_not_authorize_case(self):
        case = self.new_case()
        LocumLink.objects.filter(pk=self.link.pk).update(accepted_at=None)
        with self.assertRaises(PermissionDenied):
            create_case(self.staff.pk, self.case_data())
        with self.assertRaises(PermissionDenied):
            with authorized_case(self.doctor.pk, case.pk):
                pass

    def test_only_doctor_can_accept_and_response_requires_acceptance(self):
        case = self.new_case()
        with self.assertRaises(PermissionDenied):
            perform_action(self.staff.pk, case.pk, "accept")
        with self.assertRaises(Conflict):
            finalize_response(self.doctor.pk, case.pk, self.final_data())
        self.accept(case)
        with self.assertRaises(Conflict):
            self.accept(case)
        with self.assertRaises(PermissionDenied):
            finalize_response(self.staff.pk, case.pk, self.final_data())

    def test_final_response_password_attestation_and_immutability(self):
        case = self.new_case()
        self.accept(case)
        for overrides in ({"current_password": "incorrect"}, {"attestation": False}):
            with self.subTest(field=tuple(overrides)), self.assertRaises(ValidationError):
                finalize_response(self.doctor.pk, case.pk, self.final_data(**overrides))
        self.assertFalse(Response.objects.exists())
        first = finalize_response(self.doctor.pk, case.pk, self.final_data())
        with self.assertRaises(Conflict):
            finalize_response(self.doctor.pk, case.pk, self.final_data(assessment="Replacement"))
        first.refresh_from_db()
        self.assertEqual(first.assessment, "Fictional clinician assessment.")
        self.assertEqual(Response.objects.count(), 1)

    def test_video_request_requires_consent_but_does_not_require_a_call_before_response(self):
        case = self.new_case()
        self.accept(case)
        perform_action(self.doctor.pk, case.pk, "request-video")
        case.refresh_from_db()
        self.assertEqual(case.review_mode, Case.ReviewMode.VIDEO)
        self.assertIsNone(case.video_consent_at)
        with self.assertRaises(ValidationError):
            perform_action(self.staff.pk, case.pk, "video-consent")
        perform_action(self.staff.pk, case.pk, "video-consent", video_consent=True)
        case.refresh_from_db()
        self.assertIsNotNone(case.video_consent_at)
        finalize_response(self.doctor.pk, case.pk, self.final_data())

    def test_active_call_must_end_before_final_response_or_cancellation(self):
        case = self.new_case(review_mode=Case.ReviewMode.VIDEO, video_consent=True)
        self.accept(case)
        video = VideoSession.objects.create(
            case=case, started_by=self.doctor, room_name="opaque-fictional-room"
        )
        with self.assertRaises(Conflict):
            finalize_response(self.doctor.pk, case.pk, self.final_data())
        with self.assertRaises(Conflict):
            perform_action(self.staff.pk, case.pk, "cancel")
        video.ended_at = timezone.now()
        video.cleanup_state = VideoSession.CleanupState.DONE
        video.save(update_fields=["ended_at", "cleanup_state"])
        finalize_response(self.doctor.pk, case.pk, self.final_data())

    def test_declined_and_cancelled_cases_cannot_be_responded_to(self):
        declined = self.new_case()
        perform_action(self.doctor.pk, declined.pk, "decline")
        cancelled = self.new_case()
        perform_action(self.staff.pk, cancelled.pk, "cancel")
        for case in (declined, cancelled):
            with self.subTest(case=case.pk), self.assertRaises(Conflict):
                finalize_response(self.doctor.pk, case.pk, self.final_data())

    def test_http_written_journey_replay_and_private_headers(self):
        self.client.force_login(self.staff)
        data = self.case_data()
        data["doctor"] = str(self.doctor.pk)
        create = self.client.post(reverse("care:case-create"), data)
        self.assertEqual(create.status_code, 302)
        case = Case.objects.get()
        detail_url = reverse("care:case-detail", args=[case.pk])
        self.client.force_login(self.doctor)
        detail = self.client.get(detail_url)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("no-store", detail["Cache-Control"])
        self.assertEqual(
            self.client.post(reverse("care:case-action", args=[case.pk, "accept"])).status_code, 302
        )
        response_url = reverse("care:case-respond", args=[case.pk])
        wrong = self.client.post(response_url, self.final_data(current_password="incorrect"))
        self.assertEqual(wrong.status_code, 400)
        self.assertNotContains(wrong, 'value="incorrect"', status_code=400)
        self.assertEqual(self.client.post(response_url, self.final_data()).status_code, 302)
        self.assertEqual(self.client.post(response_url, self.final_data()).status_code, 409)
        self.client.force_login(self.staff)
        self.assertEqual(
            self.client.post(
                reverse("care:case-action", args=[case.pk, "acknowledge"])
            ).status_code,
            302,
        )
        case.refresh_from_db()
        self.assertEqual(case.status, Case.Status.ACKNOWLEDGED)

    def test_http_current_link_scope_and_pagination(self):
        case = self.new_case()
        for _ in range(21):
            self.new_case()
        self.client.force_login(self.staff)
        page = self.client.get(reverse("care:case-list"))
        self.assertEqual(len(page.context["cases"]), 20)
        self.assertEqual(page.context["cases"].paginator.count, 22)
        self.client.force_login(self.other_staff)
        foreign = self.client.get(reverse("care:case-detail", args=[case.pk]))
        self.assertEqual(foreign.status_code, 403)
        self.assertNotContains(foreign, case.patient_name, status_code=403)
        self.client.force_login(self.doctor)
        LocumLink.objects.filter(pk=self.link.pk).update(state=LocumLink.State.REVOKED)
        self.assertEqual(
            self.client.get(reverse("care:case-detail", args=[case.pk])).status_code, 403
        )
        self.assertEqual(len(self.client.get(reverse("care:case-list")).context["cases"]), 0)

    def test_mutations_reject_get_and_require_csrf(self):
        case = self.new_case()
        action_url = reverse("care:case-action", args=[case.pk, "accept"])
        self.client.force_login(self.doctor)
        self.assertEqual(self.client.get(action_url).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.doctor)
        self.assertEqual(csrf_client.post(action_url).status_code, 403)
        case.refresh_from_db()
        self.assertEqual(case.status, Case.Status.PENDING)

    def test_database_failure_rolls_back_final_response_and_is_generic(self):
        case = self.new_case()
        self.accept(case)
        self.client.force_login(self.doctor)
        with patch(
            "care.services.CaseEvent.objects.create",
            side_effect=DatabaseError("private database diagnostics"),
        ):
            result = self.client.post(
                reverse("care:case-respond", args=[case.pk]), self.final_data()
            )
        self.assertEqual(result.status_code, 503)
        self.assertNotContains(result, "private database diagnostics", status_code=503)
        self.assertFalse(Response.objects.exists())
        case.refresh_from_db()
        self.assertEqual(case.status, Case.Status.IN_REVIEW)
