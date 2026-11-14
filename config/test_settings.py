"""Disposable test configuration; never opens a configured application DB."""

import atexit
import base64
import os
import secrets
import tempfile
from pathlib import Path

os.environ["DJANGO_SECRET_KEY"] = secrets.token_urlsafe(64)
os.environ["DJANGO_DEBUG"] = "1"
os.environ["DATABASE_ENGINE"] = "sqlite"
os.environ["REPORT_ROOT_KEY"] = base64.urlsafe_b64encode(os.urandom(32)).decode()

from .settings import *  # noqa: E402,F403

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
_reports = tempfile.TemporaryDirectory(prefix="locumbridge-test-reports-")
atexit.register(_reports.cleanup)
MEDIA_ROOT = Path(_reports.name)
LIVEKIT_API_KEY = LIVEKIT_API_SECRET = LIVEKIT_API_URL = LIVEKIT_URL = ""
LIVEKIT_EXPLICIT_ROOMS_CONFIRMED = False
