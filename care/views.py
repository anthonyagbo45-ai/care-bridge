from functools import wraps

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils.cache import patch_vary_headers
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.models import User
from accounts.services import eligible
from accounts.throttles import _hit

from .forms import CaseForm, ReportUploadForm, ResponseForm
from .models import Case, CaseEvent, Response, VideoSession
from .services import (
    Conflict,
    authorized_case,
    authorized_list,
    create_case,
    finalize_response,
    perform_action,
)
from .updates import case_revision, inbox_revision


def private_view(view):
    """Keep clinical HTTP responses private and avoid database-error details."""

    @wraps(view)
    @sensitive_post_parameters()
    def wrapped(request, *args, **kwargs):
        try:
            if not request.user.is_authenticated:
                response = (
                    HttpResponse("Sign in again to continue.", status=401)
                    if request.headers.get("Accept") == "application/json"
                    else redirect_to_login(request.path)
                )
            else:
                response = view(request, *args, **kwargs)
        except PermissionDenied:
            response = HttpResponse(
                "This record or action is not available to your account.", status=403
            )
        except Conflict as error:
            response = HttpResponse(str(error), status=409)
        except ValidationError:
            response = HttpResponse("Check the submitted information and try again.", status=400)
        except DatabaseError:
            # The service transaction has rolled back before this handler runs.
            response = HttpResponse(
                "The service is temporarily unavailable. Please try again.", status=503
            )
        response["Cache-Control"] = "private, no-store, max-age=0"
        response["Pragma"] = "no-cache"
        response["Referrer-Policy"] = "same-origin"
        patch_vary_headers(response, ["Cookie"])
        return response

    return wrapped


def _form_errors(form, error):
    if hasattr(error, "message_dict"):
        for field, errors in error.message_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, error)


@private_view
@require_GET
def case_list(request):
    # Pending doctors and hospitals see their own approval/link status, with no
    # patient queryset created or evaluated for their account.
    if not eligible(request.user):
        return redirect("accounts:links")
    with authorized_list(request.user.pk) as (user, cases):
        # Take the marker first: a concurrent change may cause a harmless
        # refresh prompt, but must never be absorbed by a marker newer than UI.
        revision = inbox_revision(user.pk, cases)
        page = Paginator(cases, 20).get_page(request.GET.get("page"))
        page.object_list = list(page.object_list)
        return render(
            request,
            "care/case_list.html",
            {
                "cases": page,
                "page_obj": page,
                "is_staff_user": user.role == User.Role.STAFF,
                "is_doctor": user.role == User.Role.DOCTOR,
                "updates_revision": revision,
            },
        )


@private_view
@require_GET
def inbox_updates(request):
    with authorized_list(request.user.pk) as (user, cases):
        return JsonResponse({"revision": inbox_revision(user.pk, cases)})


@private_view
@require_GET
def case_updates(request, case_id):
    with authorized_case(request.user.pk, case_id) as (user, case, _link):
        return JsonResponse({"revision": case_revision(user.pk, case)})


@private_view
@require_http_methods(["GET", "POST"])
def case_create(request):
    with authorized_list(request.user.pk) as (user, _cases):
        if user.role != User.Role.STAFF:
            raise PermissionDenied
        form = CaseForm(request.POST if request.method == "POST" else None, user=user)
        if request.method == "POST" and form.is_valid():
            try:
                case = create_case(user.pk, form.cleaned_data)
            except ValidationError as error:
                _form_errors(form, error)
            else:
                messages.success(request, "Case sent to your hired locum doctor.")
                return redirect("care:case-detail", case_id=case.pk)
        return render(request, "care/case_form.html", {"form": form})


def _render_detail(request, case_id, *, response_form=None, status=200):
    from .media import configured

    with authorized_case(request.user.pk, case_id) as (user, case, _link):
        revision = case_revision(user.pk, case)
        doctor = user.role == User.Role.DOCTOR
        staff = user.role == User.Role.STAFF
        in_review = case.status == Case.Status.IN_REVIEW
        response = Response.objects.filter(case=case).select_related("doctor").first()
        active_video = VideoSession.objects.filter(case=case, ended_at__isnull=True).first()
        context = {
            "case": case,
            "response": response,
            "reports": list(case.reports.all()),
            "is_staff_user": staff,
            "is_doctor": doctor,
            "can_accept": doctor and case.status == Case.Status.PENDING,
            "can_respond": doctor and in_review,
            "can_ack": staff and case.status == Case.Status.RESPONDED,
            "can_cancel": staff and case.status in (Case.Status.PENDING, Case.Status.IN_REVIEW),
            "can_upload": staff and case.status in (Case.Status.PENDING, Case.Status.IN_REVIEW),
            "can_request_video": doctor and in_review and case.review_mode == Case.ReviewMode.TEXT,
            "can_record_video_consent": staff
            and in_review
            and case.review_mode == Case.ReviewMode.VIDEO
            and not case.video_consent_at,
            "response_form": (response_form if response_form is not None else ResponseForm())
            if doctor and in_review
            else None,
            "upload_form": ReportUploadForm(),
            "video_available": configured(),
            "active_video": active_video,
            "updates_revision": revision,
        }
        CaseEvent.objects.create(case=case, actor=user, event=CaseEvent.Event.READ)
        return render(request, "care/case_detail.html", context, status=status)


@private_view
@require_GET
def case_detail(request, case_id):
    return _render_detail(request, case_id)


@private_view
@require_POST
def case_action(request, case_id, action):
    perform_action(
        request.user.pk, case_id, action, video_consent=request.POST.get("video_consent") == "on"
    )
    messages.success(request, "Case updated.")
    return redirect("care:case-detail", case_id=case_id)


@private_view
@require_POST
def case_respond(request, case_id):
    if not _hit("consultation-confirm", str(request.user.pk), 6, 60):
        response = HttpResponse("Too many confirmation attempts. Try again shortly.", status=429)
        response["Retry-After"] = "60"
        return response
    with authorized_case(request.user.pk, case_id) as (user, case, _link):
        if user.role != User.Role.DOCTOR:
            raise PermissionDenied
        if case.status != Case.Status.IN_REVIEW:
            raise Conflict("A final response cannot be added in the current case state.")
        form = ResponseForm(request.POST)
        if form.is_valid():
            try:
                finalize_response(user.pk, case_id, form.cleaned_data)
            except ValidationError as error:
                _form_errors(form, error)
            else:
                messages.success(request, "Your final written consultation has been recorded.")
                return redirect("care:case-detail", case_id=case_id)
        return _render_detail(request, case_id, response_form=form, status=400)
