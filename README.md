# Care Bridge

A separate, focused Django app for a hospital team and its already hired locum
doctors. Each case is a standalone handover, with patient details, vitals and
reports. The doctor accepts the case, provides a written consultation and can
use a private video call when appropriate. The hospital acknowledges the reply.

The interface has three main screens: **Cases**, **New case** and **Consultation**.
Named users sign in as hospital team members or locum doctors. Hospitals link
only to their own engaged doctors; the doctor accepts the link before cases can
be sent. The app does not assess urgency automatically. Emergency treatment and
referral follow the hospital's onsite procedures, without waiting for remote care.

## Run locally on Linux

Use Python 3.12. No Docker or system installation is needed for the local written
consultation demonstration.

```bash
git clone https://github.com/anthonyagbo45-ai/care-bridge.git
cd care-bridge
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/setup_local.py
python manage.py migrate
python manage.py create_demo --confirm-fictional
python manage.py runserver --nothreading
```

Open **http://127.0.0.1:8000/**. The demo command prints a new random password
for `hospital@carebridge.test` and `doctor@carebridge.test`. These accounts and
approvals are explicitly fictional. Use two browser profiles to try both roles.
Running the command again preserves existing accounts and passwords.

If Ubuntu lacks `ensurepip` and you cannot install system packages, you can use
pip from an existing virtual environment to prepare this new environment. This
does not install packages into that existing environment:

```bash
python3 -m venv --without-pip .venv
/path/to/existing/venv/bin/python -m pip --python .venv install pip -r requirements.txt
source .venv/bin/activate
```

Use the actual path to your existing Python, such as the `venv/bin/python` in
your older project. Then continue with `setup_local.py` and the remaining commands.
For subsequent starts, activate `.venv` and run the server; no reinstallation
or database reset is needed. Preserve `.env`, the database and encrypted report
directory together. Losing `REPORT_ROOT_KEY` loses access to stored report bytes.

## Try the main journey

1. Sign in as the hospital and choose **New case**.
2. Enter fictional patient details and measured vitals, select the engaged
   doctor and record the patient's or representative's actual sharing decision.
3. Send the case. The hospital sees **Awaiting doctor acceptance**.
4. Sign in as the doctor, open the assigned case and choose **Accept case**.
5. Review the details and available reports. Write the assessment,
   recommendations and follow-up/escalation advice. Confirm the final response
   with the doctor's password.
6. The hospital sees **New updates are available**, chooses **Show updates**,
   reads the reply and confirms it has been seen. The response is retained and
   cannot be overwritten.

The app does not promise that a doctor is online or will respond within a
particular time. The hospital remains responsible for monitoring pending cases
and using its agreed cover/escalation arrangements.

## Updates without losing your place

The case inbox and consultation page check for changes about every 15 seconds
while visible. A small **New updates are available** message offers **Show
updates** when a case, reply, report scan result or call state changes. The page
never refreshes itself, so an unfinished written response stays where it is.
If you choose to refresh after typing, a confirmation warns that the unfinished
entries will be lost. Refresh always opens the case safely with a GET request,
including after an unsuccessful response submission.

Checks pause when the tab is hidden, retry more slowly after a connection
failure, and stop if your access is withdrawn. The check returns an opaque change
marker after checking current permissions; it sends no patient details or
consultation text. **Refresh** also works when JavaScript is unavailable.
These are open-page alerts, not phone notifications or closed-browser alerts.
They do not establish that a doctor has seen a case or guarantee a response time.

## Real accounts and owner setup

Public registration requires email confirmation before sign-in. The local email
backend prints the activation link in the server terminal; it does not deliver
mail. A real deployment needs configured SMTP delivery.

Create the trusted owner through the server:

```bash
python manage.py createsuperuser
```

Open **http://127.0.0.1:8000/owner/**. Approve the hospital after independent
checks and verify the doctor's registration using the authorized source and a
recorded verification reference. Registration does not self-approve a doctor or
hospital. The owner can create named colleagues under the same approved hospital.
Patient cases and consultation bodies are not registered in this approval admin.

The hospital's **Doctors** page invites an existing verified doctor by exact
email. The doctor accepts on **Hospitals**. Revoking this link denies the doctor's
old case and report links while preserving the hospital's records.

## Patient reports

PDF, PNG and JPEG uploads are bounded and format-validated in an isolated process,
then encrypted in a private directory outside static files. A report becomes
viewable/downloadable only after a real ClamAV scan succeeds. It stays private
when the scanner is unavailable. The app does not send patient reports to a
public scanning website.

Configure a current ClamAV daemon on the application host using `CLAMAV_HOST`
and `CLAMAV_PORT` in `.env` (loopback only). An existing native ClamAV installation
can be used; Docker is not required. Your hosting/IT operator must maintain
official, current antivirus definitions. Run the worker in a second terminal:

```bash
source .venv/bin/activate
python manage.py process_tasks --watch
```

This also retries video-room cleanup. Until a real scanner is configured, the
local demonstration shows reports waiting for a scan. Positive unit-test scan
results use controlled test doubles; they are not real antivirus evidence.

## Video calls

Written consultations work without a media provider. Real video requires a
private LiveKit service and HTTPS. Set these values privately in `.env`:

```text
LIVEKIT_URL=wss://your-approved-livekit-host
LIVEKIT_API_URL=https://your-approved-livekit-host
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
LIVEKIT_EXPLICIT_ROOMS_CONFIRMED=1
```

Set the confirmation only after your operator has verified that rooms cannot
be automatically recreated by old join tokens. Rooms allow two participants,
use opaque identifiers and short-lived join tokens, and disable recording in
this app. Secrets stay on the server. No public meeting link is used.

After the doctor accepts a case, the clinical team can request video and record
its separate consent. The doctor starts the room; the hospital joins. Patient
details and approved reports can open in another tab while video continues.
Either side can end the session. Failed provider cleanup remains visible and
blocks finalizing the response until cleanup succeeds. Run the worker above
while testing calls. Sessions expire after one hour.

No live provider has been configured or tested as part of the fictional browser
proof. The unconfigured state is displayed honestly. Camera, microphone, audio,
two-device connectivity and provider cleanup still need testing with your chosen
service. A written reply remains possible when no call was started; it must
accurately document what was assessed and any limitations.

## Development checks

```bash
python -m pip install -r requirements-dev.txt
DJANGO_SETTINGS_MODULE=config.test_settings python manage.py test
DJANGO_SETTINGS_MODULE=config.test_settings python manage.py makemigrations --check --dry-run
python scripts/verify_browser.py
ruff check .
ruff format --check .
```

Browser checks require Chromium on the development machine. They use disposable
fictional accounts, SQLite, local email and generated keys; only masked captures
remain after cleanup. No application database is opened by the test settings.
See [verification](docs/VERIFICATION.md) for observed results and limits.

This is a development MVP for fictional demonstrations. Before real patient use,
complete clinical/legal/privacy review, current practitioner-licence and hospital
checks, appropriate MFA/session controls, traceable clinical corrections and
consent-withdrawal/retention procedures. Deploy with HTTPS, encrypted PostgreSQL
storage/backups, shared rate limiting, SMTP and managed scanner/media services.
Production configuration rejects SQLite. The local prototype does not establish
Nigerian regulatory approval or production readiness.
