from time import time
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import User
from apps.accounts.reauth import ReauthRedirect, pending_action, require_recent_reauthentication
from apps.authorization.models import SystemRole
from apps.organizations.models import Department


def _confirmed_device(user: User) -> TOTPDevice:
    user.mfa_enabled = True
    user.save(update_fields=("mfa_enabled",))
    return TOTPDevice.objects.create(
        user=user,
        name="default",
        key="3132333435363738393031323334353637383930",
        confirmed=True,
    )


def _current_token(device: TOTPDevice) -> str:
    token = TOTP(
        device.bin_key,
        step=device.step,
        t0=device.t0,
        digits=device.digits,
        drift=device.drift,
    ).token()
    return f"{token:0{device.digits}d}"


def _set_pending_mfa(client, user: User) -> None:
    session = client.session
    session["pending_mfa_user_id"] = str(user.id)
    session["pending_mfa_started_at"] = time()
    session.save()


@pytest.mark.django_db
def test_login_always_redirects_to_mfa_and_never_swaps_fragment(client) -> None:
    user = User.objects.create_user("admin@example.test", "Admin", "safe-password")
    _confirmed_device(user)

    htmx_response = client.post(
        reverse("accounts-login"),
        {"email": user.email, "password": "safe-password"},
        HTTP_HX_REQUEST="true",
    )

    assert htmx_response.status_code == 302
    assert htmx_response.url == reverse("accounts-mfa-verify")
    assert client.session["pending_mfa_user_id"] == str(user.id)

    browser_response = client.post(
        reverse("accounts-login"),
        {"email": user.email, "password": "safe-password"},
    )

    assert browser_response.status_code == 302
    assert browser_response.url == reverse("accounts-mfa-verify")


@pytest.mark.django_db
def test_login_and_mfa_failures_return_generic_errors(client) -> None:
    user = User.objects.create_user("admin@example.test", "Admin", "safe-password")
    _confirmed_device(user)

    password_response = client.post(
        reverse("accounts-login"),
        {"email": user.email, "password": "incorrect-password"},
    )

    assert password_response.status_code == 200
    assert b"Invalid credentials." in password_response.content
    assert b"incorrect-password" not in password_response.content

    _set_pending_mfa(client, user)
    mfa_response = client.post(reverse("accounts-mfa-verify"), {"token": "000000"})

    assert mfa_response.status_code == 200
    assert b"Invalid verification code." in mfa_response.content
    assert b"default" not in mfa_response.content


@pytest.mark.django_db
def test_verified_mfa_login_sets_a_fresh_reauthentication_timestamp(client) -> None:
    user = User.objects.create_user("admin@example.test", "Admin", "safe-password")
    device = _confirmed_device(user)
    _set_pending_mfa(client, user)

    response = client.post(reverse("accounts-mfa-verify"), {"token": _current_token(device)})

    assert response.status_code == 302
    assert response.url == reverse("dashboard")
    assert client.session["_auth_user_id"] == str(user.id)
    assert client.session["recent_reauthentication_at"] >= time() - 5
    assert "pending_mfa_user_id" not in client.session


@pytest.mark.django_db
def test_reauthentication_pending_action_expires_and_is_single_use(client) -> None:
    user = User.objects.create_user("admin@example.test", "Admin", "safe-password")
    SystemRole.objects.create(user=user)
    device = _confirmed_device(user)
    client.force_login(user)
    action_url = reverse("portal-system-departments")

    pending_response = client.post(action_url, {"name": "North", "short_code": "NORTH"})

    assert pending_response.status_code == 302
    pending_url = pending_response.url
    token = parse_qs(urlparse(pending_url).query)["pending"][0]
    assert not Department.objects.filter(short_code="NORTH").exists()
    assert client.session["pending_reauth"] == {
        "token": token,
        "user_id": str(user.id),
        "url": action_url,
        "method": "POST",
        "return_url": action_url,
        "exp": client.session["pending_reauth"]["exp"],
    }

    session = client.session
    session["pending_reauth"]["exp"] = int(time()) - 1
    session.save()
    expired_response = client.get(pending_url)

    assert expired_response.status_code == 200
    assert b"North" not in expired_response.content

    pending_response = client.post(action_url, {"name": "North", "short_code": "NORTH"})
    token = parse_qs(urlparse(pending_response.url).query)["pending"][0]
    reauth_response = client.post(
        reverse("accounts-reauthenticate"),
        {"pending": token, "token": _current_token(device)},
    )

    assert reauth_response.status_code == 302
    assert reauth_response.url == action_url
    assert client.session["recent_reauthentication_at"] >= time() - 5
    assert "pending_reauth" not in client.session
    continuation_response = client.get(reauth_response.url)
    assert continuation_response.status_code == 200
    assert not Department.objects.filter(short_code="NORTH").exists()

    second_action_response = client.post(action_url, {"name": "North", "short_code": "NORTH"})
    assert second_action_response.status_code == 302
    assert Department.objects.filter(short_code="NORTH").exists()

    replay_response = client.get(f"{reverse('accounts-reauthenticate')}?pending={token}")

    assert replay_response.status_code == 200
    assert b"North" not in replay_response.content


@pytest.mark.django_db
def test_htmx_reauthentication_uses_full_page_redirect_and_explicit_form_action(client) -> None:
    user = User.objects.create_user("htmx-admin@example.test", "HTMX Admin", "safe-password")
    SystemRole.objects.create(user=user)
    device = _confirmed_device(user)
    client.force_login(user)
    action_url = reverse("portal-system-departments")

    htmx_response = client.post(
        action_url,
        {"name": "North", "short_code": "NORTH"},
        HTTP_HX_REQUEST="true",
    )

    assert htmx_response.status_code == 200
    assert htmx_response.content == b""
    assert "HX-Redirect" in htmx_response
    reauth_url = htmx_response["HX-Redirect"]
    token = parse_qs(urlparse(reauth_url).query)["pending"][0]
    assert reauth_url.startswith(reverse("accounts-reauthenticate"))
    assert client.session["pending_reauth"]["url"] == action_url
    assert not Department.objects.filter(short_code="NORTH").exists()

    reauth_page = client.get(reauth_url)

    assert reauth_page.status_code == 200
    assert reauth_page.wsgi_request.path == reverse("accounts-reauthenticate")
    assert (
        f'action="{reverse("accounts-reauthenticate")}?pending={token}"'
        in reauth_page.content.decode()
    )

    valid_response = client.post(
        reauth_url,
        {"pending": token, "token": _current_token(device)},
    )

    # Pending reauthentication deliberately returns to the safe page without
    # replaying the original POST body. The operator submits the action once
    # after reauthentication, rather than posting back to the old HTMX page.
    assert valid_response.status_code == 302
    assert valid_response.url == action_url
    assert "pending_reauth" not in client.session
    assert not Department.objects.filter(short_code="NORTH").exists()

    first_resubmission = client.post(action_url, {"name": "North", "short_code": "NORTH"})

    assert first_resubmission.status_code == 302
    assert Department.objects.filter(short_code="NORTH").count() == 1


@pytest.mark.django_db
def test_invalid_htmx_reauthentication_otp_stays_on_the_reauthentication_page(client) -> None:
    user = User.objects.create_user("invalid-otp@example.test", "Invalid OTP", "safe-password")
    SystemRole.objects.create(user=user)
    _confirmed_device(user)
    client.force_login(user)
    action_url = reverse("portal-system-departments")
    htmx_response = client.post(
        action_url,
        {"name": "East", "short_code": "EAST"},
        HTTP_HX_REQUEST="true",
    )
    reauth_url = htmx_response["HX-Redirect"]
    token = parse_qs(urlparse(reauth_url).query)["pending"][0]

    invalid_response = client.post(
        reauth_url,
        {"pending": token, "token": "000000"},
    )

    assert invalid_response.status_code == 200
    assert invalid_response.wsgi_request.path == reverse("accounts-reauthenticate")
    assert b"Invalid TOTP code." in invalid_response.content
    assert "pending_reauth" in client.session
    assert not Department.objects.filter(short_code="EAST").exists()


@pytest.mark.django_db
def test_non_htmx_reauthentication_keeps_ordinary_redirect(client) -> None:
    user = User.objects.create_user("browser-admin@example.test", "Browser Admin", "safe-password")
    SystemRole.objects.create(user=user)
    _confirmed_device(user)
    client.force_login(user)

    response = client.post(
        reverse("portal-system-departments"),
        {"name": "West", "short_code": "WEST"},
    )

    assert response.status_code == 302
    assert response.url.startswith(reverse("accounts-reauthenticate"))
    assert "HX-Redirect" not in response


@pytest.mark.parametrize("return_url", ("https://attacker.example/", "//attacker.example/"))
def test_reauthentication_rejects_unsafe_or_missing_continuations(return_url) -> None:
    request = RequestFactory().post("/sensitive-action/")
    SessionMiddleware(lambda request: HttpResponse()).process_request(request)
    request.user = User(id="user-id")

    with pytest.raises(ValueError, match="safe local URL"):
        require_recent_reauthentication(request, return_url=return_url)
    with pytest.raises(TypeError):
        cast(Any, require_recent_reauthentication)(request)


def test_pending_reauthentication_rejects_tampered_return_url() -> None:
    request = RequestFactory().post("/sensitive-action/")
    SessionMiddleware(lambda request: HttpResponse()).process_request(request)
    request.user = User(id="user-id")

    with pytest.raises(ReauthRedirect):
        require_recent_reauthentication(request, return_url="/safe-management-page/")
    token = request.session["pending_reauth"]["token"]
    request.session["pending_reauth"]["return_url"] = "//attacker.example/"

    assert pending_action(request, token) is None
