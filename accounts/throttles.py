"""Small fixed-window limits; cache failures deny the attempt."""

import time

from django.core.cache import cache
from django.http import HttpResponse
from django.utils.crypto import salted_hmac


def _hit(scope, value, limit, window):
    digest = salted_hmac("locumbridge.account-throttle", value, algorithm="sha256").hexdigest()
    bucket = int(time.time()) // window
    key = f"account-rate:{scope}:{bucket}:{digest}"
    try:
        if cache.add(key, 1, timeout=window + 1):
            count = 1
        else:
            count = cache.incr(key)
        return count <= limit
    except Exception:
        # Cache backend faults must not silently remove the authentication limit.
        return False


def account_throttle(request, action, email):
    if action == "login":
        window, ip_limit, email_limit = 600, 30, 8
    else:
        window, ip_limit, email_limit = 3600, 10, 3
    address = str(request.META.get("REMOTE_ADDR", "unknown"))[:100]
    normalized_email = str(email).strip().lower()[:254]
    allowed = _hit(f"{action}:ip", address, ip_limit, window)
    if allowed:
        allowed = _hit(f"{action}:email", normalized_email, email_limit, window)
    if allowed:
        return None
    response = HttpResponse("Too many attempts. Please try again later.", status=429)
    response["Retry-After"] = str(window)
    response["Cache-Control"] = "private, no-store, max-age=0"
    return response
