"""Private update indicators for fictional cases; no provider or mail calls."""

from datetime import timedelta
from html.parser import HTMLParser
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Hospital, LocumLink, User

from . import tests as consultation_tests
from .models import Case, CaseEvent, Report, Response, VideoSession
from .services import create_case, finalize_response, perform_action


class _UpdateElement(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.matches = []
        self.refresh_links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "data-case-updates" in attributes:
            self.matches.append(attributes)
        if "data-refresh-updates" in attributes:
            self.refresh_links.append(attributes)


class UpdateTests(TestCase):
    # Reuse the fictional workflow fixture without inheriting/discovering the
    # separate ConsultationTests test methods a second time.
    password = consultation_tests.ConsultationTests.password
    setUpTestData = classmethod(consultation_tests.ConsultationTests.setUpTestData.__func__)
    case_data = consultation_tests.ConsultationTests.case_data
    final_data = consultation_tests.ConsultationTests.final_data
    new_case = consultation_tests.ConsultationTests.new_case
    accept = consultation_tests.ConsultationTests.accept

    def setUp(self):
        cache.clear()
        self.client.force_login(self.staff)

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def snapshot(self, case=None, *, client=None):
        endpoint = (
            reverse("care:case-updates", args=[case.pk])
            if case is not None
            else reverse("care:inbox-updates")
        )
        response = (client or self.client).get(endpoint)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"revision"})
        revision = response.json()["revision"]
        self.assertRegex(revision, r"^[a-f0-9]{64}$")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("Cookie", response["Vary"])
        return revision

    def test_snapshots_are_opaque_private_and_polling_makes_no_writes(self):
        case = self.new_case()
        before = (
            CaseEvent.objects.count(),
            Response.objects.count(),
            Case.objects.get(pk=case.pk).updated_at,
        )
        for _ in range(3):
            for current in (None, case):
                self.snapshot(current)
        self.assertEqual(
            before,
            (
                CaseEvent.objects.count(),
                Response.objects.count(),
                Case.objects.get(pk=case.pk).updated_at,
            ),
        )
        payload = self.client.get(reverse("care:case-updates", args=[case.pk])).content.decode()
        for private in (
            case.patient_name,
            case.patient_reference,
            case.presenting_complaint,
            self.staff.email,
            self.doctor.email,
            self.hospital.name,
            str(case.pk),
            case.created_at.isoformat(),
        ):
            self.assertNotIn(private, payload)

    def test_inbox_changes_for_new_case_acceptance_response_and_acknowledgement(self):
        previous = self.snapshot()
        case = self.new_case()
        current = self.snapshot()
        self.assertNotEqual(previous, current)
        for action in (
            lambda: self.accept(case),
            lambda: finalize_response(self.doctor.pk, case.pk, self.final_data()),
            lambda: perform_action(self.staff.pk, case.pk, "acknowledge"),
        ):
            previous = current
            action()
            current = self.snapshot()
            self.assertNotEqual(previous, current)

    def test_detail_changes_for_case_video_request_and_consent(self):
        case = self.new_case()
        previous = self.snapshot(case)
        self.accept(case)
        current = self.snapshot(case)
        self.assertNotEqual(previous, current)
        for action in (
            lambda: perform_action(self.doctor.pk, case.pk, "request-video"),
            lambda: perform_action(self.staff.pk, case.pk, "video-consent", video_consent=True),
        ):
            previous = current
            action()
            current = self.snapshot(case)
            self.assertNotEqual(previous, current)

    def test_detail_detects_report_upload_scan_states_but_not_retry_time(self):
        case = self.new_case()
        previous = self.snapshot(case)
        report = Report.objects.create(
            case=case,
            uploaded_by=self.staff,
            display_name="Fictional report.png",
            detected_type=Report.DetectedType.PNG,
            size_bytes=100,
            digest="0" * 64,
        )
        current = self.snapshot(case)
        self.assertNotEqual(previous, current)
        for state in (Report.State.UNAVAILABLE, Report.State.CLEAN, Report.State.REJECTED):
            previous = current
            Report.objects.filter(pk=report.pk).update(state=state)
            current = self.snapshot(case)
            self.assertNotEqual(previous, current)
        Report.objects.filter(pk=report.pk).update(scan_attempted_at=timezone.now())
        self.assertEqual(current, self.snapshot(case))

    def test_detail_detects_call_start_end_and_cleanup_changes(self):
        case = self.new_case(review_mode=Case.ReviewMode.VIDEO, video_consent=True)
        self.accept(case)
        previous = self.snapshot(case)
        video = VideoSession.objects.create(
            case=case, started_by=self.doctor, room_name="fictional-private-updates-room"
        )
        current = self.snapshot(case)
        self.assertNotEqual(previous, current)
        for changes in (
            {"ended_at": timezone.now(), "version": 2},
            {"cleanup_state": VideoSession.CleanupState.ERROR},
            {"cleanup_state": VideoSession.CleanupState.DONE},
        ):
            previous = current
            VideoSession.objects.filter(pk=video.pk).update(**changes)
            current = self.snapshot(case)
            self.assertNotEqual(previous, current)

    def test_read_audit_does_not_raise_false_update(self):
        case = self.new_case()
        inbox = self.snapshot()
        detail = self.snapshot(case)
        self.assertEqual(
            self.client.get(reverse("care:case-detail", args=[case.pk])).status_code, 200
        )
        self.assertTrue(CaseEvent.objects.filter(case=case, event=CaseEvent.Event.READ).exists())
        self.assertEqual(inbox, self.snapshot())
        self.assertEqual(detail, self.snapshot(case))

    def test_unrelated_hospital_and_doctor_cannot_influence_revisions(self):
        case = self.new_case()
        inbox, detail = self.snapshot(), self.snapshot(case)
        other_case = create_case(
            self.other_staff.pk, self.case_data(doctor=self.other_doctor, patient_name="Other case")
        )
        perform_action(self.other_doctor.pk, other_case.pk, "accept")
        self.assertEqual(inbox, self.snapshot())
        self.assertEqual(detail, self.snapshot(case))
        self.client.force_login(self.doctor)
        inbox = self.snapshot()
        create_case(self.other_staff.pk, self.case_data(doctor=self.other_doctor))
        self.assertEqual(inbox, self.snapshot())

    def test_actor_and_case_scopes_are_rechecked_after_login(self):
        case = self.new_case()
        detail_url = reverse("care:case-updates", args=[case.pk])
        for user in (self.other_staff, self.other_doctor):
            with self.subTest(actor=user.role):
                self.client.force_login(user)
                response = self.client.get(detail_url)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn(case.patient_name, response.content.decode())
        self.client.force_login(self.doctor)
        self.snapshot(case)
        LocumLink.objects.filter(pk=self.link.pk).update(state=LocumLink.State.REVOKED)
        self.assertEqual(self.client.get(detail_url).status_code, 403)
        # The hospital retains its own record after terminating the doctor link.
        self.client.force_login(self.staff)
        self.snapshot(case)
        self.assertEqual(
            self.client.get(reverse("care:case-updates", args=[uuid4()])).status_code, 403
        )

    def test_revoking_current_approval_or_email_blocks_updates(self):
        case = self.new_case()
        for changes in ({"email_verified_at": None}, {"hospital": self.other_hospital}):
            User.objects.filter(pk=self.staff.pk).update(**changes)
            self.assertEqual(
                self.client.get(reverse("care:case-updates", args=[case.pk])).status_code, 403
            )
            User.objects.filter(pk=self.staff.pk).update(
                email_verified_at=timezone.now(), hospital=self.hospital
            )
        Hospital.objects.filter(pk=self.hospital.pk).update(is_approved=False)
        for endpoint in (
            reverse("care:inbox-updates"),
            reverse("care:case-updates", args=[case.pk]),
        ):
            self.assertEqual(self.client.get(endpoint).status_code, 403)

    def test_updates_require_sign_in_and_get(self):
        case = self.new_case()
        anonymous = Client()
        for endpoint in (
            reverse("care:inbox-updates"),
            reverse("care:case-updates", args=[case.pk]),
        ):
            with self.subTest(endpoint=endpoint):
                response = anonymous.get(endpoint)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response["Location"])
                self.assertEqual(self.client.post(endpoint).status_code, 405)

    def test_html_baselines_match_first_poll(self):
        case = self.new_case()
        for page_url, endpoint in (
            (reverse("care:case-list"), reverse("care:inbox-updates")),
            (
                reverse("care:case-detail", args=[case.pk]),
                reverse("care:case-updates", args=[case.pk]),
            ),
        ):
            with self.subTest(page=page_url):
                page = self.client.get(page_url)
                self.assertEqual(page.status_code, 200)
                elements = _UpdateElement(page.content.decode()).matches
                self.assertEqual(len(elements), 1)
                self.assertEqual(elements[0]["data-url"], endpoint)
                self.assertEqual(
                    elements[0]["data-revision"], self.client.get(endpoint).json()["revision"]
                )

    def test_older_reports_swapping_states_still_raise_an_update(self):
        case = self.new_case()
        reports = [
            Report.objects.create(
                case=case,
                uploaded_by=self.staff,
                display_name=f"Fictional report {index}.png",
                detected_type=Report.DetectedType.PNG,
                size_bytes=100,
                digest="0" * 64,
                state=state,
            )
            for index, state in enumerate((Report.State.CLEAN, Report.State.PENDING) * 2)
        ]
        now = timezone.now()
        for index, report in enumerate(reports):
            Report.objects.filter(pk=report.pk).update(
                created_at=now - timedelta(minutes=4 - index)
            )
        previous = self.snapshot(case)
        # The newer clean and pending reports stay fixed. State counts and the
        # newest creation time within each state therefore do not change.
        Report.objects.filter(pk=reports[0].pk).update(state=Report.State.PENDING)
        Report.objects.filter(pk=reports[1].pk).update(state=Report.State.CLEAN)
        self.assertNotEqual(previous, self.snapshot(case))

    def test_expired_session_json_checks_return_private_401_without_redirect(self):
        case = self.new_case()
        anonymous = Client()
        for endpoint in (
            reverse("care:inbox-updates"),
            reverse("care:case-updates", args=[case.pk]),
        ):
            with self.subTest(endpoint=endpoint):
                response = anonymous.get(endpoint, HTTP_ACCEPT="application/json")
                self.assertEqual(response.status_code, 401)
                self.assertNotIn("Location", response)
                self.assertIn("no-store", response["Cache-Control"])
                self.assertIn("Cookie", response["Vary"])
                self.assertNotIn(case.patient_name, response.content.decode())

    def test_invalid_response_post_refreshes_to_canonical_case_get(self):
        case = self.new_case()
        self.accept(case)
        self.client.force_login(self.doctor)
        response = self.client.post(
            reverse("care:case-respond", args=[case.pk]),
            self.final_data(current_password="wrong-fictional-password"),
        )
        self.assertEqual(response.status_code, 400)
        parser = _UpdateElement(response.content.decode())
        self.assertEqual(len(parser.refresh_links), 1)
        self.assertEqual(
            parser.refresh_links[0]["href"], reverse("care:case-detail", args=[case.pk])
        )
        self.assertEqual(
            parser.matches[0]["data-url"], reverse("care:case-updates", args=[case.pk])
        )
        self.assertFalse(Response.objects.filter(case=case).exists())

    def test_scan_state_change_after_display_read_is_not_absorbed_by_baseline(self):
        case = self.new_case()
        # Metadata alone simulates a scanner outcome. No bytes are uploaded or
        # scanned, and this test does not provide real antivirus evidence.
        report = Report.objects.create(
            case=case,
            uploaded_by=self.staff,
            display_name="Fictional concurrent report.png",
            detected_type=Report.DetectedType.PNG,
            size_bytes=100,
            digest="0" * 64,
            state=Report.State.PENDING,
        )
        original_from_db = Report.from_db
        changed = False

        def scanner_after_materialization(*args, **kwargs):
            nonlocal changed
            row = original_from_db(*args, **kwargs)
            if row.pk == report.pk and not changed:
                changed = True
                Report.objects.filter(pk=report.pk).update(state=Report.State.CLEAN)
            return row

        with patch.object(Report, "from_db", side_effect=scanner_after_materialization):
            page = self.client.get(reverse("care:case-detail", args=[case.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertTrue(changed)
        self.assertEqual(page.context["reports"][0].state, Report.State.PENDING)
        self.assertContains(page, "Waiting for malware scan")
        baseline = _UpdateElement(page.content.decode()).matches[0]["data-revision"]
        self.assertNotEqual(baseline, self.snapshot(case))

    def test_case_change_after_inbox_display_read_is_not_absorbed_by_baseline(self):
        case = self.new_case()
        original_from_db = Case.from_db
        changed = False

        def accept_after_materialization(*args, **kwargs):
            nonlocal changed
            row = original_from_db(*args, **kwargs)
            if row.pk == case.pk and not changed:
                changed = True
                # Simulate the doctor's independently committed acceptance after
                # the old inbox row was materialized for display.
                Case.objects.filter(pk=case.pk).update(
                    status=Case.Status.IN_REVIEW,
                    accepted_at=timezone.now(),
                    updated_at=timezone.now(),
                )
            return row

        with patch.object(Case, "from_db", side_effect=accept_after_materialization):
            page = self.client.get(reverse("care:case-list"))
        self.assertEqual(page.status_code, 200)
        self.assertTrue(changed)
        self.assertEqual(page.context["cases"].object_list[0].status, Case.Status.PENDING)
        baseline = _UpdateElement(page.content.decode()).matches[0]["data-revision"]
        self.assertNotEqual(baseline, self.snapshot())
