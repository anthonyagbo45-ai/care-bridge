"""Create private local configuration once; never overwrite existing keys."""

import base64
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
path = root / ".env"
if path.exists():
    print("Existing .env preserved. Run python manage.py migrate next.")
else:
    content = (
        "DJANGO_DEBUG=1\n"
        f"DJANGO_SECRET_KEY={secrets.token_urlsafe(64)}\n"
        f"REPORT_ROOT_KEY={base64.urlsafe_b64encode(os.urandom(32)).decode()}\n"
        "DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1\n"
        "DATABASE_ENGINE=sqlite\n"
        "SITE_URL=http://127.0.0.1:8000\n"
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)
    print("Created private .env. Use fictional records for this local pilot.")
