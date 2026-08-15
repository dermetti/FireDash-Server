import base64
from io import BytesIO

import qrcode
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.forms import LoginForm, ReauthenticationForm, SetupForm, TokenForm
from apps.accounts.models import AuthenticationThrottle, User
from apps.accounts.services import (
    clear_pre_mfa_session,
    consume_setup_token,
    is_throttled,
    pending_mfa_user,
    record_auth_failure,
)
from apps.audit.services import record_event

AUTH_PAGE_CSP = (
    "default-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'; img-src 'self' data:; object-src 'none'"
)


def _no_store(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "origin"
    # Do not sandbox these top-level pages: it gives form POSTs an opaque Origin.
    response["Content-Security-Policy"] = AUTH_PAGE_CSP
    return response


@never_cache
@require_http_methods(["GET", "POST"])
def account_login(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].casefold()
        if is_throttled(
            scope=AuthenticationThrottle.Scope.PASSWORD,
            account=email,
            source_ip=getattr(request, "client_ip", None),
        ):
            messages.error(request, "Authentication is temporarily unavailable.")
        else:
            user = authenticate(request, username=email, password=form.cleaned_data["password"])
            if user is None:
                record_auth_failure(
                    scope=AuthenticationThrottle.Scope.PASSWORD,
                    account=email,
                    source_ip=getattr(request, "client_ip", None),
                )
                record_event(
                    action="authentication.login_failed", request=request, target_type="user"
                )
                messages.error(request, "Invalid credentials.")
            else:
                request.session.cycle_key()
                request.session["pending_mfa_user_id"] = str(user.id)
                request.session["pending_mfa_started_at"] = timezone_now_timestamp()
                if request.headers.get("HX-Request") == "true":
                    return render(request, "accounts/_mfa_form.html", {"form": TokenForm()})
                return redirect(
                    "accounts-mfa-verify" if user.mfa_enabled else "accounts-mfa-enroll"
                )
    return _no_store(render(request, "accounts/login.html", {"form": form}))


def timezone_now_timestamp() -> float:
    from django.utils import timezone

    return timezone.now().timestamp()


@never_cache
@require_http_methods(["GET", "POST"])
def mfa_enroll(request: HttpRequest) -> HttpResponse:
    user = pending_mfa_user(request.session)
    if user is None or user.mfa_enabled:
        return redirect("accounts-login")
    device, _ = TOTPDevice.objects.get_or_create(
        user=user, name="default", defaults={"confirmed": False}
    )
    if request.method == "POST":
        form = TokenForm(request.POST)
        account = str(user.id)
        if is_throttled(
            scope=AuthenticationThrottle.Scope.MFA,
            account=account,
            source_ip=getattr(request, "client_ip", None),
        ):
            messages.error(request, "Authentication is temporarily unavailable.")
        elif form.is_valid() and device.verify_token(form.cleaned_data["token"]):
            device.confirmed = True
            device.save(update_fields=("confirmed",))
            user.mfa_enabled = True
            user.save(update_fields=("mfa_enabled",))
            login(request, user)
            request.session["recent_reauthentication_at"] = timezone_now_timestamp()
            clear_pre_mfa_session(request.session)
            record_event(
                action="authentication.mfa_enrolled",
                request=request,
                actor_user=user,
                target_type="user",
                target_uuid=user.id,
            )
            return redirect("dashboard")
        else:
            record_auth_failure(
                scope=AuthenticationThrottle.Scope.MFA,
                account=account,
                source_ip=getattr(request, "client_ip", None),
            )
            record_event(
                action="authentication.mfa_enrollment_failed",
                request=request,
                actor_user=user,
                target_type="user",
                target_uuid=user.id,
            )
            messages.error(request, "Invalid verification code.")
    else:
        form = TokenForm()
    image = qrcode.make(device.config_url).get_image()
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return _no_store(
        render(
            request,
            "accounts/mfa_enroll.html",
            {"form": form, "qr_code": base64.b64encode(buffer.getvalue()).decode()},
        )
    )


@never_cache
@require_http_methods(["GET", "POST"])
def mfa_verify(request: HttpRequest) -> HttpResponse:
    user = pending_mfa_user(request.session)
    if user is None or not user.mfa_enabled:
        return redirect("accounts-login")
    form = TokenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        account = str(user.id)
        if is_throttled(
            scope=AuthenticationThrottle.Scope.MFA,
            account=account,
            source_ip=getattr(request, "client_ip", None),
        ):
            messages.error(request, "Authentication is temporarily unavailable.")
        elif (
            device := TOTPDevice.objects.filter(user=user, confirmed=True).first()
        ) and device.verify_token(form.cleaned_data["token"]):
            login(request, user)
            request.session["recent_reauthentication_at"] = timezone_now_timestamp()
            clear_pre_mfa_session(request.session)
            record_event(
                action="authentication.login_succeeded",
                request=request,
                actor_user=user,
                target_type="user",
                target_uuid=user.id,
            )
            return redirect("dashboard")
        else:
            record_auth_failure(
                scope=AuthenticationThrottle.Scope.MFA,
                account=account,
                source_ip=getattr(request, "client_ip", None),
            )
            record_event(
                action="authentication.mfa_failed",
                request=request,
                actor_user=user,
                target_type="user",
                target_uuid=user.id,
            )
            messages.error(request, "Invalid verification code.")
    template = (
        "accounts/_mfa_form.html"
        if request.headers.get("HX-Request") == "true"
        else "accounts/mfa_verify.html"
    )
    return _no_store(render(request, template, {"form": form}))


@never_cache
@require_http_methods(["GET", "POST"])
def account_setup(request: HttpRequest, token: str) -> HttpResponse:
    form = SetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if is_throttled(
            scope=AuthenticationThrottle.Scope.SETUP,
            account=token,
            source_ip=getattr(request, "client_ip", None),
        ):
            messages.error(request, "Account setup is temporarily unavailable.")
        elif (
            user := consume_setup_token(raw_token=token, password=form.cleaned_data["password"])
        ) is not None:
            record_event(
                action="authentication.account_setup_completed",
                request=request,
                actor_user=user,
                target_type="user",
                target_uuid=user.id,
            )
            return redirect("accounts-login")
        else:
            record_auth_failure(
                scope=AuthenticationThrottle.Scope.SETUP,
                account=token,
                source_ip=getattr(request, "client_ip", None),
            )
            messages.error(request, "This setup link is invalid or expired.")
    return _no_store(render(request, "accounts/setup.html", {"form": form}))


@login_required
@require_http_methods(["POST"])
def account_logout(request: HttpRequest) -> HttpResponse:
    record_event(
        action="authentication.logout",
        request=request,
        actor_user=request.user,
        target_type="user",
        target_uuid=request.user.id,
    )
    logout(request)
    return _no_store(redirect("accounts-login"))


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def reauthenticate(request: HttpRequest) -> HttpResponse:
    user_id = request.user.pk
    if user_id is None:
        raise PermissionDenied("An authenticated user is required.")
    current_user = User.objects.get(pk=user_id)
    pending_token = request.GET.get("pending") or request.POST.get("pending")
    operation = ""
    return_url = ""
    if pending_token:
        from apps.accounts.reauth import pending_action

        pending = pending_action(request, pending_token)
        if pending is not None:
            operation = pending.action_url
            return_url = pending.return_url
    form = ReauthenticationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        device = TOTPDevice.objects.filter(user=current_user, confirmed=True).first()
        account = str(current_user.id)
        if is_throttled(
            scope=AuthenticationThrottle.Scope.MFA,
            account=account,
            source_ip=getattr(request, "client_ip", None),
        ):
            messages.error(request, "Authentication is temporarily unavailable.")
        elif device and device.verify_token(form.cleaned_data["token"]):
            request.session["recent_reauthentication_at"] = timezone_now_timestamp()
            record_event(
                action="authentication.reauthenticated",
                request=request,
                actor_user=current_user,
                target_type="user",
                target_uuid=current_user.id,
            )
            if pending_token and return_url:
                pending_action(request, pending_token, consume=True)
                # The operator must submit the sensitive action again; no POST body is replayed.
                return redirect(return_url)
            return redirect("dashboard")
        else:
            record_auth_failure(
                scope=AuthenticationThrottle.Scope.MFA,
                account=account,
                source_ip=getattr(request, "client_ip", None),
            )
            record_event(
                action="authentication.reauthentication_failed",
                request=request,
                actor_user=current_user,
                target_type="user",
                target_uuid=current_user.id,
            )
            messages.error(request, "Invalid TOTP code.")
    return _no_store(
        render(
            request,
            "accounts/reauthenticate.html",
            {"form": form, "pending_token": pending_token, "operation": operation},
        )
    )
