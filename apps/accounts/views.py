import base64
from io import BytesIO

import qrcode
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
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


def _no_store(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


@never_cache
@require_http_methods(["GET", "POST"])
def account_login(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("health-live")
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
        if form.is_valid() and device.verify_token(form.cleaned_data["token"]):
            device.confirmed = True
            device.save(update_fields=("confirmed",))
            user.mfa_enabled = True
            user.save(update_fields=("mfa_enabled",))
            login(request, user)
            clear_pre_mfa_session(request.session)
            record_event(
                action="authentication.mfa_enrolled",
                request=request,
                actor_user=user,
                target_type="user",
                target_uuid=user.id,
            )
            return redirect("health-live")
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
            clear_pre_mfa_session(request.session)
            record_event(
                action="authentication.login_succeeded",
                request=request,
                actor_user=user,
                target_type="user",
                target_uuid=user.id,
            )
            return redirect("health-live")
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
    return _no_store(render(request, "accounts/mfa_verify.html", {"form": form}))


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
    return redirect("accounts-login")


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def reauthenticate(request: HttpRequest) -> HttpResponse:
    current_user = User.objects.get(pk=request.user.pk)
    form = ReauthenticationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = authenticate(
            request, username=current_user.email, password=form.cleaned_data["password"]
        )
        device = TOTPDevice.objects.filter(user=current_user, confirmed=True).first()
        if user and device and device.verify_token(form.cleaned_data["token"]):
            request.session["recent_reauthentication_at"] = timezone_now_timestamp()
            record_event(
                action="authentication.reauthenticated",
                request=request,
                actor_user=current_user,
                target_type="user",
                target_uuid=current_user.id,
            )
            return redirect("health-live")
        record_event(
            action="authentication.reauthentication_failed",
            request=request,
            actor_user=current_user,
            target_type="user",
            target_uuid=current_user.id,
        )
        messages.error(request, "Reauthentication failed.")
    return _no_store(render(request, "accounts/reauthenticate.html", {"form": form}))
