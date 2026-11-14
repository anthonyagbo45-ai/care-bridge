"""Synthetic account tests use only Django's disposable test database/email outbox."""

import re
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .admin import HospitalAdmin
from .forms import RegistrationForm
from .models import Hospital, LocumLink, User
from .services import active_link, change_link, eligible, invite_doctor
from .tokens import activation_token, valid_activation_token

PASSWORD = "fictional-cobalt-cloud-728!"


class AccountFixture(TestCase):
    def setUp(self):
        cache.clear()
        self.hospital = Hospital.objects.create(name="Fictional Harbour Hospital", is_approved=True)
        self.other_hospital = Hospital.objects.create(
            name="Fictional Hill Hospital", is_approved=True
        )
        self.staff = User.objects.create_user(
            "staff@example.invalid",
            PASSWORD,
            first_name="Fictional Staff",
            role=User.Role.STAFF,
            hospital=self.hospital,
            email_verified_at=timezone.now(),
        )
        self.other_staff = User.objects.create_user(
            "other-staff@example.invalid",
            PASSWORD,
            role=User.Role.STAFF,
            hospital=self.other_hospital,
            email_verified_at=timezone.now(),
        )
        self.doctor = User.objects.create_user(
            "doctor@example.invalid",
            PASSWORD,
            first_name="Fictional Doctor",
            role=User.Role.DOCTOR,
            email_verified_at=timezone.now(),
            doctor_verified=True,
            registration_number="FICTIONAL-1",
        )
        self.other_doctor = User.objects.create_user(
            "other-doctor@example.invalid",
            PASSWORD,
            role=User.Role.DOCTOR,
            email_verified_at=timezone.now(),
            doctor_verified=True,
        )

    def accepted_link(self):
        link = invite_doctor(self.staff.pk, self.doctor.email)
        return change_link(self.doctor.pk, link.pk, "accept")


class RegistrationTests(AccountFixture):
    def registration_data(self, **extra):
        data = {
            "full_name": "Fictional New Doctor",
            "email": "new@example.invalid",
            "role": "DOCTOR",
            "registration_number": "FICTIONAL-2",
            "password1": PASSWORD,
            "password2": PASSWORD,
        }
        data.update(extra)
        return data

    def test_register_cannot_self_approve_or_join_named_existing_hospital(self):
        response = self.client.post(
            reverse("accounts:register"),
            self.registration_data(
                role="STAFF",
                hospital_name=self.hospital.name,
                hospital=str(self.hospital.pk),
                doctor_verified="1",
                email_verified_at="2026-01-01",
                is_staff="1",
                is_superuser="1",
            ),
        )
        self.assertRedirects(response, reverse("accounts:registration-done"))
        user = User.objects.get(email="new@example.invalid")
        self.assertNotEqual(user.hospital_id, self.hospital.pk)
        self.assertFalse(user.hospital.is_approved)
        self.assertFalse(user.doctor_verified)
        self.assertIsNone(user.email_verified_at)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.first_name, "Fictional New Doctor")
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_doctor_registration_has_no_hospital_and_needs_owner_verification(self):
        form = RegistrationForm(self.registration_data(hospital_name="Ignored Hospital"))
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertIsNone(user.hospital_id)
        self.assertFalse(user.doctor_verified)
        self.assertEqual(user.registration_number, "FICTIONAL-2")

    def test_role_requirements_and_password_validation(self):
        for data, field in [
            (self.registration_data(role="STAFF"), "hospital_name"),
            (self.registration_data(registration_number=""), "registration_number"),
            (self.registration_data(password1="abc", password2="abc"), "password1"),
            (self.registration_data(password2="different-password-123"), "password2"),
        ]:
            with self.subTest(field=field):
                form = RegistrationForm(data)
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)

    def test_email_normalized_and_case_duplicate_has_generic_result(self):
        response = self.client.post(
            reverse("accounts:register"), self.registration_data(email="DOCTOR@EXAMPLE.INVALID")
        )
        self.assertRedirects(response, reverse("accounts:registration-done"))
        self.assertEqual(User.objects.filter(email__iexact=self.doctor.email).count(), 1)
        self.assertEqual(len(mail.outbox), 0)
        user = User.objects.create_user("UPPER@EXAMPLE.INVALID", PASSWORD)
        self.assertEqual(user.email, "upper@example.invalid")

    def test_registration_to_confirmed_login_journey_without_auto_login(self):
        self.client.post(reverse("accounts:register"), self.registration_data())
        url = re.search(r"http://testserver(/accounts/activate/\S+)", mail.outbox[0].body).group(1)
        user = User.objects.get(email="new@example.invalid")
        self.assertEqual(self.client.get(url).status_code, 200)
        user.refresh_from_db()
        self.assertIsNone(user.email_verified_at)
        self.assertEqual(self.client.post(url).status_code, 200)
        user.refresh_from_db()
        self.assertIsNotNone(user.email_verified_at)
        self.assertFalse(user.doctor_verified)
        self.assertNotIn("_auth_user_id", self.client.session)
        response = self.client.post(
            reverse("login"), {"username": user.email, "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("care:case-list"))


class ActivationAndLoginTests(AccountFixture):
    def test_owner_approval_login_has_a_real_cache_rate_limit(self):
        owner = User.objects.create_superuser("owner@example.invalid", PASSWORD)
        for _ in range(8):
            response = self.client.post(
                "/owner/login/", {"username": owner.email, "password": "wrong"}
            )
            self.assertEqual(response.status_code, 200)
        blocked = self.client.post("/owner/login/", {"username": owner.email, "password": PASSWORD})
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("no-store", blocked["Cache-Control"])
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_resend_is_generic_and_never_verifies_or_logs_in(self):
        self.doctor.email_verified_at = None
        self.doctor.save(update_fields=["email_verified_at"])
        for email in (self.doctor.email, "missing@example.invalid"):
            response = self.client.post(reverse("accounts:resend"), {"email": email})
            self.assertRedirects(response, reverse("accounts:registration-done"))
        self.assertEqual(len(mail.outbox), 1)
        self.doctor.refresh_from_db()
        self.assertIsNone(self.doctor.email_verified_at)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_failed_email_transport_leaves_account_unverified_without_error_details(self):
        self.doctor.email_verified_at = None
        self.doctor.save(update_fields=["email_verified_at"])
        with patch(
            "accounts.views.send_mail", side_effect=RuntimeError("PRIVATE TRANSPORT DETAIL")
        ):
            response = self.client.post(reverse("accounts:resend"), {"email": self.doctor.email})
        self.assertRedirects(response, reverse("accounts:registration-done"))
        self.doctor.refresh_from_db()
        self.assertIsNone(self.doctor.email_verified_at)

    def test_activation_proof_is_consumed_and_cannot_return_after_unverification(self):
        self.doctor.email_verified_at = None
        self.doctor.save(update_fields=["email_verified_at"])
        url = self.activation_url(self.doctor)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url).status_code, 200)
        self.assertEqual(self.client.post(url).status_code, 400)
        self.doctor.refresh_from_db()
        self.assertEqual(self.doctor.email_token_version, 1)
        self.doctor.email_verified_at = None
        self.doctor.save(update_fields=["email_verified_at"])
        self.assertEqual(self.client.post(url).status_code, 400)
        self.doctor.refresh_from_db()
        self.assertIsNone(self.doctor.email_verified_at)

    def activation_url(self, user, token=None):
        return reverse(
            "accounts:activate",
            kwargs={
                "uid": urlsafe_base64_encode(force_bytes(user.pk)),
                "token": token or activation_token(user),
            },
        )

    def test_proof_expires_after_twenty_four_hours(self):
        with patch("django.core.signing.time.time", return_value=100000):
            token = activation_token(self.doctor)
        with patch("django.core.signing.time.time", return_value=100000 + 24 * 60 * 60 + 1):
            self.assertFalse(valid_activation_token(self.doctor, token))

    def test_proof_binds_email_password_and_user(self):
        token = activation_token(self.doctor)
        self.assertFalse(valid_activation_token(self.other_doctor, token))
        self.doctor.email = "changed@example.invalid"
        self.assertFalse(valid_activation_token(self.doctor, token))
        self.doctor.email = "doctor@example.invalid"
        self.doctor.set_password("changed-fictional-password-982!")
        self.assertFalse(valid_activation_token(self.doctor, token))

    def test_invalid_proof_get_and_post_do_not_verify(self):
        self.doctor.email_verified_at = None
        self.doctor.save(update_fields=["email_verified_at"])
        url = self.activation_url(self.doctor, "bad-proof")
        self.assertEqual(self.client.get(url).status_code, 400)
        self.assertEqual(self.client.post(url).status_code, 400)
        self.doctor.refresh_from_db()
        self.assertIsNone(self.doctor.email_verified_at)

    def test_activation_post_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        self.assertEqual(csrf_client.post(self.activation_url(self.doctor)).status_code, 403)

    def test_unverified_login_uses_same_generic_error_as_bad_password(self):
        self.doctor.email_verified_at = None
        self.doctor.save(update_fields=["email_verified_at"])
        first = self.client.post(
            reverse("login"), {"username": self.doctor.email, "password": PASSWORD}
        )
        second = self.client.post(
            reverse("login"), {"username": self.doctor.email, "password": "wrong"}
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            first.context["form"].non_field_errors(), second.context["form"].non_field_errors()
        )
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_confirmed_login_accepts_normalized_email(self):
        response = self.client.post(
            reverse("login"), {"username": "DOCTOR@EXAMPLE.INVALID", "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.doctor.pk)

    def test_logout_only_accepts_post(self):
        self.client.force_login(self.doctor)
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        self.assertEqual(self.client.post(reverse("logout")).status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)


class LinkServiceTests(AccountFixture):
    def test_invite_accept_revoke_and_reinvite(self):
        link = invite_doctor(self.staff.pk, self.doctor.email)
        self.assertEqual(link.state, LocumLink.State.PENDING)
        self.assertIsNone(active_link(self.hospital.pk, self.doctor.pk))
        link = change_link(self.doctor.pk, link.pk, "accept")
        self.assertIsNotNone(link.accepted_at)
        self.assertEqual(active_link(self.hospital.pk, self.doctor.pk).pk, link.pk)
        change_link(self.staff.pk, link.pk, "revoke")
        self.assertIsNone(active_link(self.hospital.pk, self.doctor.pk))
        invited = invite_doctor(self.staff.pk, self.doctor.email)
        self.assertEqual(invited.pk, link.pk)
        self.assertEqual(invited.state, LocumLink.State.PENDING)
        self.assertIsNone(invited.accepted_at)

    def test_other_participants_and_hospitals_cannot_change_link(self):
        link = invite_doctor(self.staff.pk, self.doctor.email)
        for user, action in [
            (self.other_doctor, "accept"),
            (self.other_doctor, "decline"),
            (self.other_staff, "revoke"),
            (self.doctor, "revoke"),
            (self.staff, "accept"),
        ]:
            with self.subTest(user=user.email, action=action), self.assertRaises(PermissionDenied):
                change_link(user.pk, link.pk, action)
        link.refresh_from_db()
        self.assertEqual(link.state, LocumLink.State.PENDING)

    def test_invite_requires_exact_existing_verified_doctor(self):
        self.doctor.doctor_verified = False
        self.doctor.save(update_fields=["doctor_verified"])
        for email in [self.doctor.email, self.staff.email, "doctor", "missing@example.invalid"]:
            with self.subTest(email=email), self.assertRaises(ValidationError):
                invite_doctor(self.staff.pk, email)
        self.assertEqual(LocumLink.objects.count(), 0)

    def test_no_duplicate_pending_or_active_link_and_no_repeated_accept(self):
        link = invite_doctor(self.staff.pk, self.doctor.email)
        with self.assertRaises(ValidationError):
            invite_doctor(self.staff.pk, self.doctor.email)
        change_link(self.doctor.pk, link.pk, "accept")
        with self.assertRaises(ValidationError):
            change_link(self.doctor.pk, link.pk, "accept")
        with self.assertRaises(ValidationError):
            invite_doctor(self.staff.pk, self.doctor.email)
        self.assertEqual(LocumLink.objects.count(), 1)

    def test_decline_and_subsequent_reinvite(self):
        link = invite_doctor(self.staff.pk, self.doctor.email)
        change_link(self.doctor.pk, link.pk, "decline")
        link.refresh_from_db()
        self.assertEqual(link.state, LocumLink.State.DECLINED)
        self.assertIsNone(link.accepted_at)
        self.assertEqual(
            invite_doctor(self.staff.pk, self.doctor.email).state, LocumLink.State.PENDING
        )

    def test_helpers_recheck_stale_user_and_hospital_approval(self):
        self.accepted_link()
        self.assertTrue(eligible(self.doctor))
        User.objects.filter(pk=self.doctor.pk).update(doctor_verified=False)
        self.assertFalse(eligible(self.doctor))
        self.assertIsNone(active_link(self.hospital.pk, self.doctor.pk))
        User.objects.filter(pk=self.doctor.pk).update(doctor_verified=True)
        Hospital.objects.filter(pk=self.hospital.pk).update(is_approved=False)
        self.assertFalse(eligible(self.staff))
        self.assertIsNone(active_link(self.hospital.pk, self.doctor.pk))
        with self.assertRaises(PermissionDenied):
            invite_doctor(self.staff.pk, self.other_doctor.email)

    def test_active_user_and_email_confirmation_rechecked(self):
        link = invite_doctor(self.staff.pk, self.doctor.email)
        User.objects.filter(pk=self.doctor.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            change_link(self.doctor.pk, link.pk, "accept")
        User.objects.filter(pk=self.staff.pk).update(email_verified_at=None)
        with self.assertRaises(PermissionDenied):
            change_link(self.staff.pk, link.pk, "revoke")


class LinkViewAndAdminTests(AccountFixture):
    def test_link_list_is_scoped_to_own_hospital_or_doctor(self):
        link = invite_doctor(self.staff.pk, self.doctor.email)
        other = invite_doctor(self.other_staff.pk, self.other_doctor.email)
        for user in (self.staff, self.doctor):
            self.client.force_login(user)
            response = self.client.get(reverse("accounts:links"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(list(response.context["links"]), [link])
            self.assertNotIn(other, response.context["links"])
            self.assertIn("no-store", response["Cache-Control"])

    def test_pending_doctor_can_see_own_verification_status_without_case_access(self):
        User.objects.filter(pk=self.doctor.pk).update(doctor_verified=False)
        self.client.force_login(self.doctor)
        response = self.client.get(reverse("accounts:links"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["eligible"])
        self.assertContains(response, "awaiting owner review")

    def test_link_mutations_are_post_only_and_csrf_protected(self):
        link = invite_doctor(self.staff.pk, self.doctor.email)
        self.client.force_login(self.doctor)
        url = reverse("accounts:link-action", args=[link.pk, "accept"])
        self.assertEqual(self.client.get(url).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.doctor)
        self.assertEqual(csrf_client.post(url).status_code, 403)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("accounts:link-invite")).status_code, 405)
        csrf_client.force_login(self.staff)
        self.assertEqual(
            csrf_client.post(
                reverse("accounts:link-invite"), {"doctor_email": self.other_doctor.email}
            ).status_code,
            403,
        )

    def test_unknown_or_out_of_scope_link_is_generic_forbidden(self):
        link = invite_doctor(self.other_staff.pk, self.other_doctor.email)
        self.client.force_login(self.staff)
        self.assertEqual(
            self.client.post(reverse("accounts:link-action", args=[link.pk, "revoke"])).status_code,
            403,
        )

    def test_link_public_posts_cannot_mutate_role_hospital_or_approval(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("accounts:link-invite"),
            {
                "doctor_email": self.doctor.email,
                "role": "DOCTOR",
                "hospital": self.other_hospital.pk,
                "doctor_verified": "1",
                "is_superuser": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.role, User.Role.STAFF)
        self.assertEqual(self.staff.hospital_id, self.hospital.pk)
        self.assertFalse(self.staff.is_superuser)

    def test_database_failure_returns_generic_unavailable(self):
        self.client.force_login(self.staff)
        with patch(
            "accounts.views.invite_doctor",
            side_effect=DatabaseError("synthetic private diagnostic"),
        ):
            response = self.client.post(
                reverse("accounts:link-invite"), {"doctor_email": self.doctor.email}
            )
        self.assertEqual(response.status_code, 503)
        self.assertNotContains(response, "synthetic private diagnostic", status_code=503)

    def test_only_owner_can_use_approval_admin_and_no_clinical_admin(self):
        request = RequestFactory().get("/owner/")
        self.staff.is_staff = True
        request.user = self.staff
        hospital_admin = HospitalAdmin(Hospital, admin.site)
        self.assertFalse(hospital_admin.has_change_permission(request, self.hospital))
        request.user = get_user_model().objects.create_superuser("owner@example.invalid", PASSWORD)
        self.assertTrue(hospital_admin.has_change_permission(request, self.hospital))
        self.assertFalse(any(model._meta.app_label == "care" for model in admin.site._registry))
