"""Repeatable fictional browser journey, with private credentials and cleanup."""

import base64
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PATIENT = "Fictional Locum Patient"
QUESTION = "Fictional request for a doctor's assessment."
ADVICE = "Fictional doctor advice for this demonstration only."


def main():
    artifacts = Path(tempfile.mkdtemp(prefix="locumbridge-browser-captures-"))
    artifacts.chmod(0o700)
    captures = []
    with tempfile.TemporaryDirectory(prefix="locumbridge-browser-private-") as directory:
        private = Path(directory)
        private.chmod(0o700)
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        (private / "browser_settings.py").write_text(
            "from config.settings import *\nfrom pathlib import Path\n"
            "DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3',"
            "'NAME':Path(__file__).with_name('private.sqlite3')}}\n"
            "EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'\n"
            "MEDIA_ROOT=Path(__file__).with_name('private-reports')\n"
            "PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']\n"
            "LIVEKIT_API_KEY=LIVEKIT_API_SECRET=LIVEKIT_URL=LIVEKIT_API_URL=''\n"
            "LIVEKIT_EXPLICIT_ROOMS_CONFIRMED=False\n"
        )
        environment = dict(os.environ)
        environment.update(
            DJANGO_SETTINGS_MODULE="browser_settings",
            DJANGO_DEBUG="1",
            DJANGO_SECRET_KEY=secrets.token_urlsafe(64),
            REPORT_ROOT_KEY=base64.urlsafe_b64encode(os.urandom(32)).decode(),
            DATABASE_ENGINE="sqlite",
            PYTHONPATH=str(private) + os.pathsep + str(ROOT),
            PROOF_MANIFEST=str(private / "credentials.json"),
        )

        def command(arguments):
            result = subprocess.run(
                [sys.executable, "manage.py", *arguments],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                timeout=60,
            )
            if result.returncode:
                raise RuntimeError("Private browser setup/check failed; details suppressed.")

        command(["migrate", "--noinput"])
        command(
            [
                "create_demo",
                "--confirm-fictional",
                "--credential-file",
                environment["PROOF_MANIFEST"],
            ]
        )
        command(
            [
                "shell",
                "-c",
                """
import json, os
from pathlib import Path
from django.utils import timezone
from accounts.models import User, Hospital
p=Path(os.environ['PROOF_MANIFEST']); data=json.loads(p.read_text())
other=Hospital.objects.create(name='Fictional Other Clinic',is_approved=True)
User.objects.create_user('outsider@carebridge.test',data['password'],role='STAFF',
    hospital=other,email_verified_at=timezone.now(),first_name='Fictional other staff')
data['doctor_id']=User.objects.get(email=data['doctor']).pk
p.write_text(json.dumps(data))
""",
            ]
        )
        credentials = json.loads((private / "credentials.json").read_text())
        Image.new("RGB", (32, 32), "white").save(private / "fictional-report.png")
        server = None
        with (private / "server.log").open("w") as log:
            try:
                server = subprocess.Popen(
                    [
                        sys.executable,
                        "manage.py",
                        "runserver",
                        f"127.0.0.1:{port}",
                        "--noreload",
                        "--nothreading",
                    ],
                    cwd=ROOT,
                    env=environment,
                    stdout=log,
                    stderr=log,
                )
                for _ in range(80):
                    try:
                        with urllib.request.urlopen(base + "/accounts/login/", timeout=1):
                            break
                    except OSError:
                        if server.poll() is not None:
                            raise RuntimeError("Private server stopped.") from None
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Private server did not become ready.")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(
                        executable_path=shutil.which("chromium"),
                        args=["--no-sandbox", "--disable-dev-shm-usage"],
                    )
                    contexts = {
                        name: browser.new_context() for name in ("staff", "doctor", "outsider")
                    }
                    requests = []
                    for context in contexts.values():
                        context.on("request", lambda request: requests.append(request.url))
                    pages = {name: context.new_page() for name, context in contexts.items()}

                    def capture(page, label):
                        for width in (1440, 390, 320):
                            page.set_viewport_size({"width": width, "height": 1000})
                            assert page.evaluate(
                                "document.documentElement.scrollWidth <= innerWidth + 1"
                            ), label
                            page.screenshot(
                                path=str(artifacts / f"{label}-{width}.png"),
                                full_page=True,
                                mask=[
                                    page.locator("input:visible,textarea:visible"),
                                    page.locator(
                                        "h1,.clinical-notes,.vitals-grid,.detail-facts,.report-list,"
                                        ".account-name,.case-excerpt,.case-title-line,"
                                        ".response-byline,.page-heading p"
                                    ),
                                ],
                            )
                        captures.append(label)
                        page.set_viewport_size({"width": 1440, "height": 1000})
                        print(f"PASS: {label} at 1440/390/320.", flush=True)

                    def login(name, email):
                        page = pages[name]
                        page.goto(base + "/accounts/login/")
                        page.locator('[name="username"]').fill(email)
                        page.locator('[name="password"]').fill(credentials["password"])
                        page.get_by_role("button", name="Sign in", exact=True).click()
                        expect(page).to_have_url(base + "/")

                    def new_case(video=False):
                        page = pages["staff"]
                        page.goto(base + "/new/")
                        for name, value in {
                            "patient_name": PATIENT,
                            "age_years": "42",
                            "patient_reference": "LBR-DEMO-01",
                            "presenting_complaint": QUESTION,
                            "systolic_bp": "120",
                            "diastolic_bp": "80",
                            "pulse_bpm": "75",
                            "temperature_c": "36.8",
                            "spo2_percent": "98",
                        }.items():
                            page.locator('[name="' + name + '"]').fill(value)
                        page.locator('[name="doctor"]').select_option(str(credentials["doctor_id"]))
                        page.locator('[name="review_mode"]').select_option(
                            "VIDEO" if video else "TEXT"
                        )
                        page.locator('[name="consent_method"]').select_option("VERBAL")
                        page.locator('[name="consent_party"]').select_option("PATIENT")
                        page.locator('[name="consent_confirmed"]').check()
                        if video:
                            page.locator('[name="video_consent"]').check()
                        capture(page, "new-video-case" if video else "new-written-case")
                        page.get_by_role("button", name=re.compile("Send case to doctor")).click()
                        expect(page).to_have_url(re.compile(re.escape(base) + r"/[0-9a-f-]{36}/$"))
                        expect(
                            page.get_by_role("heading", name="Awaiting doctor acceptance")
                        ).to_be_visible()
                        return page.url

                    try:
                        login("staff", credentials["staff"])
                        login("doctor", credentials["doctor"])
                        login("outsider", "outsider@carebridge.test")
                        staff, doctor, outsider = (
                            pages[name] for name in ("staff", "doctor", "outsider")
                        )
                        capture(staff, "hospital-empty-inbox")
                        case_url = new_case()
                        capture(staff, "hospital-awaiting-doctor")
                        staff.locator('input[type="file"]').set_input_files(
                            private / "fictional-report.png"
                        )
                        staff.get_by_role("button", name=re.compile("Upload report")).click()
                        expect(staff.locator("main")).to_contain_text("Waiting for malware scan")
                        expect(
                            staff.get_by_role("link", name=re.compile("^Download"))
                        ).to_have_count(0)
                        capture(staff, "report-quarantined")
                        response = outsider.goto(case_url)
                        assert response.status == 403 and PATIENT not in outsider.content()
                        capture(outsider, "other-hospital-denied")
                        doctor.goto(base + "/")
                        expect(doctor.locator("main")).to_contain_text(PATIENT)
                        capture(doctor, "doctor-assigned-inbox")
                        doctor.goto(case_url)
                        expect(doctor.locator("main")).to_contain_text("120/80")
                        doctor.get_by_role("button", name=re.compile("^Accept case")).click()
                        expect(
                            doctor.get_by_role("heading", name="Doctor reviewing")
                        ).to_be_visible()
                        for name, value in {
                            "assessment": "Fictional clinician assessment.",
                            "recommendations": ADVICE,
                            "follow_up": "Fictional follow-up.",
                        }.items():
                            doctor.locator('[name="' + name + '"]').fill(value)
                        doctor.locator('[name="attestation"]').check()
                        doctor.locator('[name="current_password"]').fill("incorrect-demo-password")
                        doctor.get_by_role(
                            "button", name=re.compile("Confirm final response")
                        ).click()
                        expect(doctor.locator("main")).to_contain_text(
                            "The current password was not accepted"
                        )
                        capture(doctor, "doctor-response-validation")
                        doctor.locator('[name="current_password"]').fill(credentials["password"])
                        old_body = doctor.locator('form[action$="/respond/"]').evaluate(
                            "form => Object.fromEntries(new FormData(form))"
                        )
                        doctor.get_by_role(
                            "button", name=re.compile("Confirm final response")
                        ).click()
                        expect(doctor.locator("main")).to_contain_text(ADVICE)
                        capture(doctor, "doctor-final-response")
                        repeated = contexts["doctor"].request.post(
                            case_url + "respond/", form=old_body
                        )
                        assert repeated.status == 409
                        staff.goto(case_url)
                        expect(staff.locator("main")).to_contain_text(ADVICE)
                        capture(staff, "hospital-consultation-ready")
                        staff.get_by_role(
                            "button", name=re.compile("Confirm hospital has seen this")
                        ).click()
                        expect(
                            staff.get_by_role("heading", name="Seen by hospital")
                        ).to_be_visible()
                        capture(staff, "hospital-acknowledged")
                        video_url = new_case(video=True)
                        doctor.goto(video_url)
                        doctor.get_by_role("button", name=re.compile("^Accept case")).click()
                        expect(doctor.locator("main")).to_contain_text("Video is not configured")
                        doctor.goto(video_url + "video/")
                        expect(doctor.locator("main")).to_contain_text("Video is not configured")
                        expect(doctor.locator('script[src$="video.js"]')).to_have_count(0)
                        capture(doctor, "video-unconfigured-honest")
                        staff.goto(base + "/accounts/links/")
                        staff.get_by_role("button", name="Revoke access", exact=True).click()
                        assert doctor.goto(case_url).status == 403
                        capture(doctor, "revoked-doctor-denied")
                        command(
                            [
                                "shell",
                                "-c",
                                """
from care.models import Case, Response, Report, VideoSession
assert Case.objects.count()==2
assert Response.objects.count()==1
assert Case.objects.filter(status='ACKNOWLEDGED').count()==1
assert Report.objects.count()==1 and Report.objects.get().state=='PENDING'
assert VideoSession.objects.count()==0
""",
                            ]
                        )
                        server_text = (private / "server.log").read_text()
                        assert all(
                            text not in server_text
                            for text in (PATIENT, QUESTION, ADVICE, credentials["password"])
                        )
                        assert all(url.startswith(base + "/") for url in requests)
                        assert all(
                            text not in url
                            for url in requests
                            for text in (PATIENT, QUESTION, ADVICE, credentials["password"])
                        )
                    finally:
                        browser.close()
            finally:
                if server:
                    server.terminate()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=5)
    print(
        f"PASS: written hospital/doctor journey; {len(captures) * 3} masked captures. "
        "Live video and real AV not claimed."
    )
    print(f"Screenshots: {artifacts}")


if __name__ == "__main__":
    main()
