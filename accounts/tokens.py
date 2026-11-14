"""An activation proof expires after 24 hours and changes with credentials."""

from django.core import signing
from django.utils.crypto import constant_time_compare, salted_hmac

SALT = "locumbridge.email-activation.v1"
MAX_AGE = 24 * 60 * 60


def _digest(user):
    value = f"{user.pk}|{user.email}|{user.password}|{user.email_token_version}"
    return salted_hmac(SALT, value, algorithm="sha256").hexdigest()


def activation_token(user):
    return signing.dumps({"proof": _digest(user)}, salt=SALT)


def valid_activation_token(user, token):
    try:
        payload = signing.loads(token, salt=SALT, max_age=MAX_AGE)
        return constant_time_compare(payload.get("proof", ""), _digest(user))
    except (signing.BadSignature, TypeError, AttributeError):
        return False
