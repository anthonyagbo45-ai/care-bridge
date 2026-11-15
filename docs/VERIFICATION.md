# Care Bridge verification — 15 November 2026

This is an independent development MVP for fictional demonstrations, built in
the `care-bridge` repository. It does not import MEDBRIDGE application modules.
The earlier MEDBRIDGE checkout remained clean at `dbd6d06`.

## Observed checks

- Installed `requirements-dev.txt` in Care Bridge's own fresh `.venv`, without
  system package installation or Docker. Dependency consistency passed.
- All **92 tests passed in 2.715 seconds**, with no skips, using
  `DJANGO_SETTINGS_MODULE=config.test_settings`. This forces memory SQLite,
  generated test keys, a temporary private report directory and local email.
  It never opens the configured application database.
- Django system checks, migration consistency, Ruff lint and formatting passed.
- A redacted Gitleaks scan of staged source files passed. Private configuration,
  virtual environments, databases and report directories are excluded from Git.
- The final headless Chromium proof passed **57 masked screenshots at
  1440, 390 and 320 pixels**, without horizontal overflow. It uses actual
  password/CSRF forms: hospital creates a case, uploads a quarantined PNG,
  doctor accepts, validation rejects a wrong confirmation password, doctor
  finalizes the response, hospital reads and acknowledges it. Replaying the
  response POST is refused. Another hospital and a revoked doctor are denied.
  A second video-request case shows the honest unconfigured state.
- Browser proof files, credentials, database, private keys and server are removed
  after the test. Only masked screenshots remain. Clinical text and passwords
  were absent from browser request URLs and server logs; browser traffic stayed
  on the local test origin. Phone and desktop layouts were visually inspected.

## Security and correctness coverage

Tests cover account/role injection, email ownership and single-use activation,
resend failure handling, hospital and doctor approval, accepted locum links,
current authorization, cross-hospital isolation, private response headers, CSRF,
normal/owner login and consultation-confirmation throttling, case transitions,
one final response, explicit consent and vital-sign validation.

Report tests use real isolated image/PDF format parsing and authenticated
encryption, including malformed/active PDFs, size limits, tampered private bytes
and current download/preview authorization. Antivirus success is a controlled
test double. No document is declared clean by the browser proof.

Media tests use the real LiveKit JWT implementation to inspect short-lived
participant grants; provider operations are controlled test doubles. Tests cover
authorization/consent, expired calls, revocation, end-for-both cleanup retries,
cleanup blocking final response, and unconfigured or malformed optional settings.
No actual two-device media connection was made.

Independent review identified two bounded-worker starvation problems and an
unthrottled owner login; these were corrected and regression-tested. Healthy
active calls cannot consume the cleanup batch. Failed report/call attempts
rotate behind unattempted work. Stale scan outcomes cannot release a changed
report. The owner entry now uses the authentication attempt limit too.

The initial browser attempt exposed `Referrer-Policy: no-referrer` producing a
null form origin. It was corrected to `same-origin`; CSRF checking remains enabled
and a null origin is explicitly rejected in regression tests.

## Open-page updates and naming

The visible application name is **Care Bridge**, including the owner console,
email confirmation and browser titles. Existing demo addresses, environment
variables and repository identifiers remain compatible.

Sixteen new regression tests cover opaque, private inbox/case revisions, current
scope checks, read-only polling, report and video state changes, activation of
GET-only endpoints, anonymous JSON checks and safe refresh URLs after validation
errors. Read-audit events and retry timestamps do not create false updates.
Ordered report/call metadata detects changes without sending clinical content.
Two concurrent-display regressions ensure a change arriving while a page is
being rendered cannot be silently absorbed into the page's baseline marker.

The browser proof additionally verifies new-case, acceptance and final-response
alerts; report-upload alerts while a doctor types; dismissing refresh preserves
the draft; and confirmation still appears for prefilled responses after a failed
password check. It exercises the real 15-second JavaScript timer with Playwright's
clock. Controlled HTTP 503 responses verify retry delays of 30 then 60 seconds
and recovery. Simulated visibility events verify hidden-page pauses. A real
permission revocation returns 403 and stops further checks. The phone alert
layout was visually inspected. No page is replaced automatically.

No dependency or database migration is required for this update. These checks
establish open-page alerts only; no background push, actual antivirus outcome,
live media connection or guaranteed doctor response is claimed.

## Remaining deployment checks

Actual GitHub Actions results are not established by local checks. A workflow
has been added for unit/system/migration/style checks.

Actual PostgreSQL concurrency, a live ClamAV engine/current definitions,
SMTP delivery, real LiveKit room policy, TURN/network connectivity, device
permissions and synchronized two-client termination still need verification
with the selected deployment. The app shows unavailable scanning/video states
and does not substitute a public meeting link or pretend a scan succeeded.

Real patient use additionally needs clinical/legal/privacy approval and the
operational controls listed in the README, including current licence review,
MFA/session hardening, clinical corrections, consent withdrawal and retention,
encrypted database/backups, monitoring and cover/escalation arrangements.
No real account, patient record, external message, server deployment or regulatory
approval was created by these tests.
