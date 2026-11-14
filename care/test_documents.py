"""Synthetic reports only; successful malware scans are controlled mocks."""

import base64
import io
import os
import struct
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, TextStringObject

from accounts.models import Hospital, LocumLink, User

from . import documents
from .models import Case, CaseEvent, Report


class PrivateReportTests(TestCase):
    """No clinical data, real ClamAV pass, external service or email is used."""

    def test_controlled_clean_preview_reauthorizes_and_keeps_private_policy(self):
        report = self.upload()
        # A controlled UI state only; this does not establish a real AV pass.
        Report.objects.filter(pk=report.pk).update(state=Report.State.CLEAN)
        self.client.force_login(self.staff)
        response = self.client.get(reverse("care:document-preview", args=[report.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertTrue(b"".join(response.streaming_content).startswith(b"\x89PNG"))
        self.assertIn("sandbox", response["Content-Security-Policy"])
        self.assertIn("no-store", response["Cache-Control"])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="locum-synthetic-reports-")
        self.addCleanup(self.directory.cleanup)
        self.settings_override = override_settings(
            MEDIA_ROOT=self.directory.name,
            REPORT_ROOT_KEY=base64.urlsafe_b64encode(os.urandom(32)).decode(),
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        now = timezone.now()
        self.hospital = Hospital.objects.create(name="Fictional test hospital", is_approved=True)
        self.staff = User.objects.create_user(
            email="reports-staff@example.test",
            password="synthetic-password-only",
            role="STAFF",
            hospital=self.hospital,
            email_verified_at=now,
        )
        self.doctor = User.objects.create_user(
            email="reports-doctor@example.test",
            password="synthetic-password-only",
            role="DOCTOR",
            doctor_verified=True,
            email_verified_at=now,
        )
        self.link = LocumLink.objects.create(
            hospital=self.hospital,
            doctor=self.doctor,
            invited_by=self.staff,
            state="ACTIVE",
            accepted_at=now,
        )
        self.case = Case.objects.create(
            hospital=self.hospital,
            created_by=self.staff,
            doctor=self.doctor,
            patient_name="Synthetic fixture",
            age_years=35,
            presenting_complaint="Synthetic fixture only",
            consent_method="VERBAL",
            consent_party="PATIENT",
            consent_at=now,
        )

    @staticmethod
    def image(kind="PNG"):
        content = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(content, format=kind)
        return content.getvalue()

    @staticmethod
    def pdf(active=False):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        if active:
            writer._root_object[NameObject("/OpenAction")] = DictionaryObject(
                {
                    NameObject("/S"): NameObject("/JavaScript"),
                    NameObject("/JS"): TextStringObject("synthetic forbidden action"),
                }
            )
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()

    def upload(self, data=None, name="synthetic.png", content_type="image/png"):
        return documents.upload_report(
            self.staff.pk,
            self.case.pk,
            SimpleUploadedFile(name, data if data is not None else self.image(), content_type),
        )

    def synthetic_clean_report(self):
        report = self.upload()
        with patch("care.documents._scan", return_value=Report.State.CLEAN):
            counts = documents.scan_pending()
        self.assertEqual(counts["clean"], 1)
        report.refresh_from_db()
        return report

    def test_synthetic_valid_formats_are_pending_encrypted_and_private(self):
        for kind, data in (
            ("PNG", self.image()),
            ("JPEG", self.image("JPEG")),
            ("PDF", self.pdf()),
        ):
            with self.subTest(kind=kind):
                report = self.upload(
                    data, f"synthetic.{kind.lower()}", documents.CONTENT_TYPES[kind]
                )
                self.assertEqual(report.state, Report.State.PENDING)
                self.assertEqual(report.detected_type, kind)
                path = documents._path(report.storage_key)
                self.assertNotEqual(path.read_bytes(), data)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(documents.DocumentError):
                    documents.download_report(self.doctor.pk, report.pk)
        self.assertEqual(CaseEvent.objects.filter(event="FILE_UPLOADED").count(), 3)

    def test_invalid_mismatch_active_and_oversize_uploads_never_store(self):
        invalid = [
            (b"not an image", "image/png"),
            (self.image(), "application/pdf"),
            (self.pdf(active=True), "application/pdf"),
            (b"%PDF-1.7\nmalformed", "application/pdf"),
            (b"x" * (documents.MAX_BYTES + 1), "image/png"),
        ]
        for data, mime in invalid:
            with self.subTest(mime=mime, size=len(data)):
                with self.assertRaises(documents.DocumentError):
                    self.upload(data, "synthetic.report", mime)
        self.assertFalse(Report.objects.exists())
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_validator_timeout_fails_closed(self):
        from subprocess import TimeoutExpired

        with patch("care.documents.subprocess.run", side_effect=TimeoutExpired("validator", 5)):
            with self.assertRaises(documents.DocumentError):
                self.upload()
        self.assertFalse(Report.objects.exists())

    def test_mocked_clean_scan_allows_current_authorized_download_with_audit(self):
        report = self.synthetic_clean_report()
        downloaded, data = documents.download_report(self.doctor.pk, report.pk)
        self.assertEqual(downloaded.pk, report.pk)
        self.assertEqual(data, self.image())
        self.assertTrue(CaseEvent.objects.filter(actor=self.doctor, event="FILE_READ").exists())

    def test_mocked_unavailable_is_retried_and_mocked_infection_stays_blocked(self):
        report = self.upload()
        with patch("care.documents._scan", return_value=Report.State.UNAVAILABLE):
            self.assertEqual(documents.scan_pending()["unavailable"], 1)
        report.refresh_from_db()
        self.assertEqual(report.state, Report.State.UNAVAILABLE)
        with self.assertRaises(documents.DocumentError):
            documents.download_report(self.staff.pk, report.pk)
        with patch("care.documents._scan", return_value=Report.State.REJECTED):
            self.assertEqual(documents.scan_pending()["rejected"], 1)
        with patch("care.documents._scan") as scanner:
            self.assertEqual(documents.scan_pending()["scanned"], 0)
            scanner.assert_not_called()

    def test_unreadable_old_report_does_not_starve_newer_report(self):
        unreadable = self.upload()
        newer = self.upload()
        documents._path(unreadable.storage_key).unlink()
        with patch("care.documents._scan", return_value=Report.State.CLEAN) as scanner:
            self.assertEqual(documents.scan_pending(limit=1)["unavailable"], 1)
            scanner.assert_not_called()
            unreadable.refresh_from_db()
            newer.refresh_from_db()
            self.assertIsNotNone(unreadable.scan_attempted_at)
            self.assertIsNone(newer.scan_attempted_at)
            self.assertEqual(documents.scan_pending(limit=1)["clean"], 1)
            scanner.assert_called_once_with(self.image())
        unreadable.refresh_from_db()
        newer.refresh_from_db()
        self.assertEqual(unreadable.state, Report.State.UNAVAILABLE)
        self.assertEqual(newer.state, Report.State.CLEAN)

    def test_scan_result_is_discarded_when_report_metadata_changes(self):
        report = self.upload()

        def scan_and_change_metadata(_data):
            Report.objects.filter(pk=report.pk).update(digest="0" * 64)
            return Report.State.CLEAN

        with patch("care.documents._scan", side_effect=scan_and_change_metadata):
            self.assertEqual(documents.scan_pending()["clean"], 0)
        report.refresh_from_db()
        self.assertEqual(report.state, Report.State.PENDING)
        self.assertIsNotNone(report.scan_attempted_at)
        with self.assertRaises(documents.DocumentError):
            documents.download_report(self.staff.pk, report.pk)

    def test_scan_result_does_not_overwrite_a_newer_state(self):
        report = self.upload()

        def scan_and_change_state(_data):
            Report.objects.filter(pk=report.pk).update(state=Report.State.REJECTED)
            return Report.State.CLEAN

        with patch("care.documents._scan", side_effect=scan_and_change_state):
            self.assertEqual(documents.scan_pending()["clean"], 0)
        report.refresh_from_db()
        self.assertEqual(report.state, Report.State.REJECTED)

    def test_revocation_blocks_doctor_old_file_but_hospital_retains_record(self):
        report = self.synthetic_clean_report()
        self.link.state = "REVOKED"
        self.link.save(update_fields=["state"])
        with self.assertRaises(PermissionDenied):
            documents.download_report(self.doctor.pk, report.pk)
        self.assertEqual(documents.download_report(self.staff.pk, report.pk)[1], self.image())

    def test_cross_hospital_superuser_and_disabled_stale_actor_cannot_read(self):
        report = self.synthetic_clean_report()
        other = Hospital.objects.create(name="Other fictional hospital", is_approved=True)
        outsider = User.objects.create_user(
            email="reports-outsider@example.test",
            password="synthetic-password-only",
            role="STAFF",
            hospital=other,
            email_verified_at=timezone.now(),
            is_superuser=True,
            is_staff=True,
        )
        with self.assertRaises(PermissionDenied):
            documents.download_report(outsider.pk, report.pk)
        User.objects.filter(pk=self.staff.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            documents.download_report(self.staff.pk, report.pk)

    def test_doctor_cannot_upload_and_closed_case_rejects_upload(self):
        with self.assertRaises(PermissionDenied):
            documents.upload_report(
                self.doctor.pk, self.case.pk, SimpleUploadedFile("x.png", self.image())
            )
        self.case.status = Case.Status.RESPONDED
        self.case.save(update_fields=["status"])
        with self.assertRaises(PermissionDenied):
            self.upload()

    def test_ciphertext_or_digest_tamper_never_releases_bytes(self):
        report = self.synthetic_clean_report()
        path = documents._path(report.storage_key)
        content = bytearray(path.read_bytes())
        content[-1] ^= 1
        path.write_bytes(content)
        with self.assertRaises(documents.DocumentError):
            documents.download_report(self.doctor.pk, report.pk)
        self.assertFalse(CaseEvent.objects.filter(event="FILE_READ").exists())

    def test_filename_sanitized_and_duplicate_storage_does_not_remove_existing(self):
        report = self.upload(name="../../synthetic\nname.png")
        self.assertNotIn("/", report.display_name)
        self.assertNotIn("\n", report.display_name)
        path = documents._path(report.storage_key)
        previous = path.read_bytes()
        with self.assertRaises(documents.DocumentError):
            documents._store(report.storage_key, self.image())
        self.assertEqual(path.read_bytes(), previous)

    def test_http_pending_and_cross_hospital_routes_private_and_csrf_upload(self):
        report = self.upload()
        self.client.force_login(self.staff)
        response = self.client.get(reverse("care:document-download", args=[report.pk]))
        self.assertEqual(response.status_code, 409)
        self.assertIn("no-store", response["Cache-Control"])
        from django.test import Client

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)
        response = csrf_client.post(
            reverse("care:document-upload", args=[self.case.pk]),
            {"report": SimpleUploadedFile("synthetic.png", self.image(), "image/png")},
        )
        self.assertEqual(response.status_code, 403)


class ClamAVProtocolTests(TestCase):
    """Controlled socket doubles exercise protocol; no real scanner pass is claimed."""

    @override_settings(CLAMAV_HOST="203.0.113.4")
    def test_non_loopback_destination_is_never_contacted(self):
        with patch("care.documents.socket.create_connection") as connect:
            self.assertEqual(documents._scan(b"synthetic"), Report.State.UNAVAILABLE)
            connect.assert_not_called()

    @override_settings(CLAMAV_HOST="127.0.0.1", CLAMAV_TIMEOUT=0.1)
    def test_mock_socket_unavailable_and_precise_clean_response(self):
        with patch("care.documents.socket.create_connection", side_effect=OSError):
            self.assertEqual(documents._scan(b"synthetic"), Report.State.UNAVAILABLE)
        with patch("care.documents.socket.create_connection") as connect:
            stream = connect.return_value.__enter__.return_value
            stream.recv.return_value = b"stream: OK\0"
            self.assertEqual(documents._scan(b"synthetic"), Report.State.CLEAN)
            sent = [call.args[0] for call in stream.sendall.call_args_list]
            self.assertEqual(
                sent, [b"zINSTREAM\0", struct.pack("!I", 9) + b"synthetic", b"\0\0\0\0"]
            )
        for malformed in (b"OK\0", b"stream: ERROR\0", b"stream: OK\0unexpected", b"x" * 1025):
            with self.subTest(response_length=len(malformed)):
                with patch("care.documents.socket.create_connection") as connect:
                    connect.return_value.__enter__.return_value.recv.return_value = malformed
                    self.assertEqual(documents._scan(b"synthetic"), Report.State.UNAVAILABLE)
