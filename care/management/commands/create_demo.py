"""Prepare fictional accounts without fixed or committed passwords."""

import json
import os
import secrets
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import Hospital, LocumLink, User


class Command(BaseCommand):
    help = "Create two fictional local demonstration accounts, never real clinical authority."

    def add_arguments(self, parser):
        parser.add_argument("--confirm-fictional", action="store_true")
        parser.add_argument(
            "--credential-file", help="Write new 0600 JSON instead of printing demo credentials."
        )

    def handle(self, *args, **options):
        if not settings.DEBUG or not options["confirm_fictional"]:
            raise CommandError("Demo creation needs DEBUG=1 and --confirm-fictional.")
        emails = {"staff": "hospital@carebridge.test", "doctor": "doctor@carebridge.test"}
        if User.objects.filter(email__in=emails.values()).exists():
            raise CommandError(
                "Demo accounts already exist. Existing accounts/passwords were preserved."
            )
        path = Path(options["credential_file"]) if options["credential_file"] else None
        if path and path.exists():
            raise CommandError("The credential file already exists; choose a new private path.")
        password = secrets.token_urlsafe(24)
        credentials = {**emails, "password": password}
        with transaction.atomic():
            hospital = Hospital.objects.create(name="Fictional Riverside Clinic", is_approved=True)
            staff = User.objects.create_user(
                emails["staff"],
                password,
                first_name="Demo hospital team",
                role=User.Role.STAFF,
                hospital=hospital,
                email_verified_at=timezone.now(),
            )
            doctor = User.objects.create_user(
                emails["doctor"],
                password,
                first_name="Demo locum doctor",
                role=User.Role.DOCTOR,
                registration_number="FICTIONAL-DEMO-ONLY",
                doctor_verified=True,
                verification_reference="Synthetic local demonstration, not actual verification",
                email_verified_at=timezone.now(),
            )
            LocumLink.objects.create(
                hospital=hospital,
                doctor=doctor,
                invited_by=staff,
                state=LocumLink.State.ACTIVE,
                accepted_at=timezone.now(),
            )
            if path:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(credentials, stream)
        self.stdout.write(
            "Fictional hospital and doctor accounts created. No patient case was added."
        )
        if path:
            self.stdout.write("Demo credentials written to the requested private file.")
        else:
            self.stdout.write(
                f"Hospital: {emails['staff']}\nDoctor: {emails['doctor']}\n"
                f"Demo password: {password}"
            )
