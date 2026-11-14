import logging
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import DatabaseError, IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from .forms import EmailAuthenticationForm, InviteDoctorForm, RegistrationForm
from .models import LocumLink, User
from .services import change_link, eligible, invite_doctor
from .throttles import account_throttle
from .tokens import activation_token, valid_activation_token

logger = logging.getLogger(__name__)


def _send_activation(request, user):
    url = request.build_absolute_uri(
        reverse(
            "accounts:activate",
            kwargs={
                "uid": urlsafe_base64_encode(force_bytes(user.pk)),
                "token": activation_token(user),
            },
        )
    )
    try:
        send_mail(
            "Confirm your CareBridge email address",
            "Confirm this email address to continue registration:\n\n"
            f"{url}\n\nThis link expires in 24 hours. Open the page and confirm the request. "
            "Hospital and doctor approval are separate owner checks. "
            "If you did not register, you can ignore this message.",
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
        )
    except Exception:
        # A transport failure must not expose credentials or confirm an email.
        # The account remains unverified and can request a fresh delivery later.
        logger.warning("Email activation delivery unavailable.")


class SignInView(LoginView):
    template_name = "registration/login.html"
    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = True

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            limited = account_throttle(request, "login", request.POST.get("username", ""))
            if limited is not None:
                return limited
        response = super().dispatch(request, *args, **kwargs)
        response["Cache-Control"] = "private, no-store, max-age=0"
        response["Pragma"] = "no-cache"
        return response


def private_page(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            response = view(request, *args, **kwargs)
        except DatabaseError:
            response = HttpResponse(
                "This service is temporarily unavailable. Please try again.", status=503
            )
        response["Cache-Control"] = "private, no-store, max-age=0"
        response["Pragma"] = "no-cache"
        return response

    return wrapped


@private_page
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("accounts:links")
    if request.method == "POST":
        limited = account_throttle(request, "register", request.POST.get("email", ""))
        if limited is not None:
            return limited
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            user = form.save()
        except IntegrityError:
            # Existing accounts receive the same public result as a new account.
            return redirect("accounts:registration-done")
        else:
            _send_activation(request, user)
            return redirect("accounts:registration-done")
    return render(request, "accounts/register.html", {"form": form})


@private_page
@require_http_methods(["GET"])
def registration_done(request):
    return render(request, "accounts/registration_done.html")


@private_page
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def resend(request):
    from django import forms

    class ResendForm(forms.Form):
        email = forms.EmailField(label="Email address")

    form = ResendForm(request.POST or None)
    if request.method == "POST":
        limited = account_throttle(request, "register", request.POST.get("email", ""))
        if limited is not None:
            return limited
        if form.is_valid():
            user = User.objects.filter(
                email__iexact=form.cleaned_data["email"],
                is_active=True,
                email_verified_at__isnull=True,
            ).first()
            if user:
                _send_activation(request, user)
            return redirect("accounts:registration-done")
    return render(request, "accounts/resend.html", {"form": form})


def _activation_user(uid):
    try:
        return User.objects.filter(pk=force_str(urlsafe_base64_decode(uid))).first()
    except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
        return None


@private_page
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def activate(request, uid, token):
    user = _activation_user(uid)
    valid = bool(user and user.is_active and valid_activation_token(user, token))
    activated = False
    if request.method == "POST" and valid:
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=user.pk)
            valid = user.is_active and valid_activation_token(user, token)
            if valid:
                if user.email_verified_at is None:
                    user.email_verified_at = timezone.now()
                    user.email_token_version += 1
                    user.save(update_fields=["email_verified_at", "email_token_version"])
                activated = True
    return render(
        request,
        "accounts/activate.html",
        {
            "activation_valid": valid,
            "activated": activated,
        },
        status=200 if valid else 400,
    )


def _links_context(request, form=None):
    user = User.objects.filter(pk=request.user.pk).first()
    if not user or not user.is_active or not user.email_verified_at:
        raise PermissionDenied
    allowed = eligible(user)
    is_staff = user.role == User.Role.STAFF
    is_doctor = user.role == User.Role.DOCTOR
    if is_staff:
        links = (
            LocumLink.objects.filter(hospital_id=user.hospital_id)
            if user.hospital_id
            else LocumLink.objects.none()
        )
        status = (
            "Your hospital is approved." if allowed else "Your hospital is awaiting owner approval."
        )
    elif is_doctor:
        links = LocumLink.objects.filter(doctor=user)
        status = (
            "Your doctor account is verified."
            if allowed
            else "Your doctor verification is awaiting owner review."
        )
    else:
        raise PermissionDenied
    links = links.select_related("hospital", "doctor")
    page = Paginator(links, 20).get_page(request.GET.get("page"))
    return {
        "user": user,
        "links": page.object_list,
        "page_obj": page,
        "form": form or InviteDoctorForm(),
        "is_staff_user": is_staff,
        "is_doctor": is_doctor,
        "eligible": allowed,
        "eligibility_message": status,
    }


@private_page
@login_required
@require_http_methods(["GET"])
def links(request):
    return render(request, "accounts/links.html", _links_context(request))


@private_page
@login_required
@sensitive_post_parameters()
@require_POST
def link_invite(request):
    form = InviteDoctorForm(request.POST)
    if form.is_valid():
        try:
            invite_doctor(request.user.pk, form.cleaned_data["doctor_email"])
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(
                request, "Invitation sent. The doctor must accept before receiving cases."
            )
            return redirect("accounts:links")
    return render(request, "accounts/links.html", _links_context(request, form), status=400)


@private_page
@login_required
@sensitive_post_parameters()
@require_POST
def link_action(request, link_id, action):
    try:
        change_link(request.user.pk, link_id, action)
    except ValidationError:
        messages.error(request, "The link changed before this request. Review its current status.")
        return redirect("accounts:links")
    messages.success(request, "Hospital–doctor link updated.")
    return redirect("accounts:links")
