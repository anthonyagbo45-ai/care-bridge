"""CSRF-protected private report and optional video endpoints."""

import io

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from . import documents, media
from .services import authorized_case
from .views import private_view


@private_view
@login_required
@require_POST
def upload(request, case_id):
    form = documents.UploadReportForm(request.POST, request.FILES)
    if not form.is_valid():
        # Even invalid uploads must pass current case authorization.
        with authorized_case(request.user.pk, case_id):
            return HttpResponse("Choose a PDF, PNG or JPEG report.", status=400)
    try:
        documents.upload_report(request.user.pk, case_id, form.cleaned_data["report"])
    except documents.DocumentError as exc:
        return HttpResponse(str(exc), status=400)
    messages.success(
        request, "Report uploaded. Download is blocked until its malware scan succeeds."
    )
    return redirect("care:case-detail", case_id=case_id)


@private_view
@login_required
@require_GET
def download(request, report_id):
    try:
        report, data = documents.download_report(request.user.pk, report_id)
    except documents.DocumentError as exc:
        return HttpResponse(str(exc), status=409)
    response = FileResponse(
        io.BytesIO(data),
        as_attachment=True,
        filename=report.display_name,
        content_type=documents.CONTENT_TYPES[report.detected_type],
    )
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@private_view
@login_required
@require_GET
def preview(request, report_id):
    try:
        report, data = documents.download_report(request.user.pk, report_id)
    except documents.DocumentError as exc:
        return HttpResponse(str(exc), status=409)
    response = FileResponse(
        io.BytesIO(data),
        as_attachment=False,
        filename=report.display_name,
        content_type=documents.CONTENT_TYPES[report.detected_type],
    )
    response["Content-Security-Policy"] = "sandbox; default-src 'none'; frame-ancestors 'self'"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@private_view
@login_required
@require_GET
def video(request, case_id):
    with authorized_case(request.user.pk, case_id) as (user, case, _link):
        available = True
        try:
            media._require_video(case)
        except media.MediaError:
            available = False
        return render(
            request,
            "care/video.html",
            {
                "case": case,
                "video_available": available,
                "is_doctor": user.role == "DOCTOR",
                "active_video": media.active_session(case),
            },
        )


def _json_call(function, request, case_id):
    try:
        return JsonResponse(function(request.user.pk, case_id))
    except media.MediaError as exc:
        return JsonResponse({"error": str(exc)}, status=503)


@private_view
@login_required
@require_POST
def video_start(request, case_id):
    def start(user_id, current_case_id):
        session = media.start_session(user_id, current_case_id)
        return {"started": True, "version": session.version}

    return _json_call(start, request, case_id)


@private_view
@login_required
@require_POST
def video_token(request, case_id):
    return _json_call(media.issue_token, request, case_id)


@private_view
@login_required
@require_POST
def video_end(request, case_id):
    return _json_call(media.end_session, request, case_id)


@private_view
@login_required
@require_GET
def video_status(request, case_id):
    return _json_call(media.session_status, request, case_id)
