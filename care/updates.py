"""Small, private change markers for open consultation pages.

Only keyed digests leave the server. Read-audit events and scan retry timestamps
are deliberately excluded so polling does not manufacture its own updates.
"""

import json

from django.db.models import Count, Max
from django.utils.crypto import salted_hmac

from .models import Report, VideoSession


def _revision(user_id, scope, state):
    payload = json.dumps([str(user_id), scope, state], default=str, sort_keys=True)
    return salted_hmac("care-bridge.page-updates.v1", payload, algorithm="sha256").hexdigest()


def inbox_revision(user_id, cases):
    # Aggregate the complete authorized inbox, including cases on other pages.
    # No patient field or user-controlled content enters this change marker.
    state = cases.aggregate(count=Count("pk"), latest=Max("updated_at"))
    return _revision(user_id, "inbox", state)


def case_revision(user_id, case):
    reports = list(Report.objects.filter(case=case).order_by("pk").values_list("pk", "state"))
    calls = list(
        VideoSession.objects.filter(case=case)
        .order_by("pk")
        .values_list("pk", "cleanup_state", "ended_at", "version")
    )
    state = [
        case.updated_at,
        case.status,
        case.review_mode,
        case.video_consent_at,
        reports,
        calls,
    ]
    return _revision(user_id, str(case.pk), state)
