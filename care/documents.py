"""Bounded private report quarantine. Scanner failures never release a report."""

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import socket
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django import forms
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import Case, CaseEvent, Report
from .services import authorized_case

MAX_BYTES = 5 * 1024 * 1024
CONTENT_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "PDF": "application/pdf"}


class DocumentError(Exception):
    """A fixed, safe message which contains no uploaded content."""


class UploadReportForm(forms.Form):
    report = forms.FileField(
        label="Report (PDF, PNG or JPEG; maximum 5 MiB)",
        widget=forms.ClearableFileInput(attrs={"accept": ".pdf,.png,.jpg,.jpeg"}),
    )


# The child receives bytes on stdin, not a filename. Resource limits and a wall
# timeout bound third-party parser work. Nothing from the parser reaches logs.
_VALIDATOR = r"""
import io, resource, sys, warnings
resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024, 384 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
data = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
kind = sys.argv[1]
try:
    if not data or len(data) > 5 * 1024 * 1024:
        raise ValueError()
    if kind in ('PNG', 'JPEG'):
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 16_000_000
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as image:
            if image.format != kind or image.width * image.height > 16_000_000:
                raise ValueError()
            if getattr(image, 'n_frames', 1) != 1:
                raise ValueError()
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    elif kind == 'PDF':
        from pypdf import PdfReader
        from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 100:
            raise ValueError()
        forbidden = {'/OpenAction','/AA','/JS','/JavaScript','/Launch',
                     '/EmbeddedFiles','/EmbeddedFile','/AcroForm','/XFA',
                     '/RichMedia','/SubmitForm','/ImportData','/GoToR','/URI',
                     '/Movie','/Sound','/Rendition','/A'}
        seen = set()
        count = [0]
        def walk(value, depth=0):
            count[0] += 1
            if depth > 64 or count[0] > 50000:
                raise ValueError()
            if isinstance(value, IndirectObject):
                key = (value.idnum, value.generation)
                if key in seen:
                    return
                seen.add(key)
                walk(value.get_object(), depth + 1)
            elif isinstance(value, DictionaryObject):
                if forbidden.intersection(map(str, value.keys())):
                    raise ValueError()
                if str(value.get('/Type', '')) == '/EmbeddedFile':
                    raise ValueError()
                for child in value.values():
                    walk(child, depth + 1)
            elif isinstance(value, ArrayObject):
                for child in value:
                    walk(child, depth + 1)
        walk(reader.trailer)
    else:
        raise ValueError()
except Exception:
    sys.exit(1)
sys.stdout.write('VALID')
"""


def _detect(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data.startswith(b"%PDF-"):
        return "PDF"
    raise DocumentError("Use a valid PDF, PNG or JPEG report.")


def _validate(data, kind):
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", _VALIDATOR, kind],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DocumentError("The report could not be safely validated.") from exc
    if result.returncode or result.stdout != b"VALID":
        raise DocumentError("The report is malformed or contains unsupported content.")


def _root_key():
    try:
        raw = str(settings.REPORT_ROOT_KEY).encode("ascii")
        key = base64.b64decode(raw, altchars=b"-_", validate=True)
        if len(key) != 32:
            raise ValueError()
        return key
    except (AttributeError, UnicodeError, ValueError) as exc:
        raise DocumentError("Private report storage is unavailable.") from exc


def _path(storage_key):
    root = Path(settings.MEDIA_ROOT).resolve()
    forbidden = [Path(settings.BASE_DIR, "static").resolve()]
    if getattr(settings, "STATIC_ROOT", None):
        forbidden.append(Path(settings.STATIC_ROOT).resolve())
    if any(root == item or item in root.parents for item in forbidden):
        raise DocumentError("Private report storage is unavailable.")
    key = uuid.UUID(str(storage_key)).hex
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / f"{key}.enc"


def _store(storage_key, data):
    path = _path(storage_key)
    nonce = os.urandom(12)
    sealed = nonce + AESGCM(_root_key()).encrypt(nonce, data, str(storage_key).encode())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        fd = os.open(path, flags, 0o600)
        created = True
        with os.fdopen(fd, "wb") as target:
            target.write(sealed)
    except OSError as exc:
        if created:
            path.unlink(missing_ok=True)
        raise DocumentError("Private report storage is unavailable.") from exc
    return path


def _read(report):
    try:
        fd = os.open(_path(report.storage_key), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            sealed = source.read(MAX_BYTES + 29)
        if len(sealed) > MAX_BYTES + 28 or len(sealed) < 29:
            raise ValueError()
        data = AESGCM(_root_key()).decrypt(
            sealed[:12], sealed[12:], str(report.storage_key).encode()
        )
        if len(data) != report.size_bytes or not hmac.compare_digest(
            hashlib.sha256(data).hexdigest(), report.digest
        ):
            raise ValueError()
        return data
    except Exception as exc:
        raise DocumentError("This report is unavailable.") from exc


def upload_report(user_id, case_id, uploaded):
    path = None
    try:
        with authorized_case(user_id, case_id) as (user, case, _link):
            if user.role != "STAFF" or case.status not in (
                Case.Status.PENDING,
                Case.Status.IN_REVIEW,
            ):
                raise PermissionDenied
            if not uploaded or uploaded.size > MAX_BYTES:
                raise DocumentError("Reports must be no larger than 5 MiB.")
            chunks, size = [], 0
            for chunk in uploaded.chunks(chunk_size=64 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise DocumentError("Reports must be no larger than 5 MiB.")
                chunks.append(chunk)
            data = b"".join(chunks)
            kind = _detect(data)
            declared = getattr(uploaded, "content_type", "")
            if declared and declared not in (CONTENT_TYPES[kind], "application/octet-stream"):
                raise DocumentError("The report type does not match its contents.")
            _validate(data, kind)
            name = str(uploaded.name).replace("\\", "/").rsplit("/", 1)[-1]
            name = re.sub(r"[^\w .()\-]", "_", name, flags=re.UNICODE).strip(" .")[:160]
            storage_key = uuid.uuid4()
            path = _store(storage_key, data)
            report = Report.objects.create(
                case=case,
                uploaded_by=user,
                display_name=name or f"report.{kind.lower()}",
                storage_key=storage_key,
                size_bytes=len(data),
                detected_type=kind,
                digest=hashlib.sha256(data).hexdigest(),
                state=Report.State.PENDING,
            )
            CaseEvent.objects.create(case=case, actor=user, event=CaseEvent.Event.FILE_UPLOADED)
            return report
    except Exception:
        if path:
            path.unlink(missing_ok=True)
        raise


def download_report(user_id, report_id):
    try:
        case_id = Report.objects.values_list("case_id", flat=True).get(pk=report_id)
    except Report.DoesNotExist as exc:
        raise PermissionDenied from exc
    with authorized_case(user_id, case_id) as (user, case, _link):
        report = Report.objects.select_for_update().get(pk=report_id, case=case)
        if report.state != Report.State.CLEAN:
            raise DocumentError("The report is awaiting a successful malware scan.")
        data = _read(report)
        CaseEvent.objects.create(case=case, actor=user, event=CaseEvent.Event.FILE_READ)
        return report, data


def _scan(data):
    """Real ClamAV INSTREAM only, with a total deadline and loopback destination."""
    try:
        host = str(getattr(settings, "CLAMAV_HOST", "127.0.0.1"))
        if not ipaddress.ip_address(host).is_loopback:
            return Report.State.UNAVAILABLE
        port = int(getattr(settings, "CLAMAV_PORT", 3310))
        timeout = min(5.0, max(0.1, float(getattr(settings, "CLAMAV_TIMEOUT", 5))))
        deadline = time.monotonic() + timeout
        with socket.create_connection((host, port), timeout=timeout) as connection:

            def send(value):
                connection.settimeout(max(0.01, deadline - time.monotonic()))
                connection.sendall(value)

            send(b"zINSTREAM\0")
            for start in range(0, len(data), 64 * 1024):
                chunk = data[start : start + 64 * 1024]
                send(struct.pack("!I", len(chunk)) + chunk)
            send(b"\x00\x00\x00\x00")
            response = bytearray()
            while b"\0" not in response and len(response) <= 1024:
                connection.settimeout(max(0.01, deadline - time.monotonic()))
                block = connection.recv(min(256, 1025 - len(response)))
                if not block:
                    break
                response.extend(block)
        if bytes(response) == b"stream: OK\0":
            return Report.State.CLEAN
        if bytes(response).endswith(b" FOUND\0") and len(response) <= 1024:
            return Report.State.REJECTED
    except (OSError, ValueError, TypeError, OverflowError):
        pass
    return Report.State.UNAVAILABLE


def scan_pending(limit=10):
    """Retry encrypted quarantine; return counts, never clinical metadata."""
    counts = {"scanned": 0, "clean": 0, "rejected": 0, "unavailable": 0}
    retryable = (Report.State.PENDING, Report.State.UNAVAILABLE)
    metadata_fields = (
        "case_id",
        "uploaded_by_id",
        "storage_key",
        "size_bytes",
        "digest",
        "detected_type",
    )
    ids = list(
        Report.objects.filter(state__in=retryable)
        .order_by(F("scan_attempted_at").asc(nulls_first=True), "created_at", "pk")
        .values_list("pk", flat=True)[: max(0, min(int(limit), 100))]
    )
    for report_id in ids:
        with transaction.atomic():
            report = Report.objects.select_for_update().filter(pk=report_id).first()
            if not report or report.state not in retryable:
                continue
            report.scan_attempted_at = timezone.now()
            report.save(update_fields=["scan_attempted_at"])
            metadata = tuple(getattr(report, field) for field in metadata_fields)
        # File access and bounded scanner I/O must not hold the report row lock.
        # Failed attempts still rotate behind reports that have not been tried.
        try:
            state = _scan(_read(report))
        except DocumentError:
            state = Report.State.UNAVAILABLE
        with transaction.atomic():
            current = Report.objects.select_for_update().filter(pk=report_id).first()
            if (
                not current
                or current.state != report.state
                or current.scan_attempted_at != report.scan_attempted_at
                or tuple(getattr(current, field) for field in metadata_fields) != metadata
            ):
                # Another attempt or a metadata/state change invalidates this
                # result; it must never release bytes using a stale scan.
                continue
            current.state = state
            current.save(update_fields=["state"])
            counts["scanned"] += 1
            counts[state.lower()] += 1
    return counts
