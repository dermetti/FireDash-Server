"""Security regressions use disposable credentials and independent sessions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import time

import pytest
from django.db import connections
from django.test import Client
from django.urls import reverse
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import User
from apps.accounts.services import create_setup_token
from apps.audit.models import AuditEvent


@pytest.mark.django_db
@pytest.mark.parametrize("failure", ["csrf", "exception"])
def test_setup_token_absent_from_django_error_log(caplog, monkeypatch, failure):
    actor = User.objects.create_user("probe@example.test", "Probe", "password")
    _, token = create_setup_token(
        actor=actor, email="setup-probe@example.test", display_name="Disposable"
    )
    client = Client(enforce_csrf_checks=True, raise_request_exception=False)
    url = reverse("accounts-setup", args=(token,))
    if failure == "csrf":
        response = client.post(url, {})
        assert response.status_code == 403
    else:

        def fail_render(*args, **kwargs):
            raise RuntimeError("Disposable setup error probe")

        monkeypatch.setattr("apps.accounts.views.render", fail_render)
        response = client.get(url)
        assert response.status_code == 500
    # Do not print or include the disposable token in assertion diagnostics.
    leaked = any(token in record.getMessage() for record in caplog.records)
    assert leaked is False
    assert all(token not in (record.exc_text or "") for record in caplog.records)
    assert response["X-Request-ID"]


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_requests_cannot_accept_same_totp(monkeypatch):
    user = User.objects.create_user("mfa-probe@example.test", "Probe", "password")
    user.mfa_enabled = True
    user.save(update_fields=("mfa_enabled",))
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    token = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift).token()
    code = f"{token:0{device.digits}d}"
    clients = [Client(), Client()]
    for client in clients:
        session = client.session
        session["pending_mfa_user_id"] = str(user.pk)
        session["pending_mfa_started_at"] = time()
        session.save()
    loaded = Barrier(2, timeout=15)
    from apps.accounts import views

    original = views.verify_totp

    def synchronized_verify(**kwargs):
        # Rendezvous before the service acquires any replay-state lock.
        loaded.wait()
        return original(**kwargs)

    monkeypatch.setattr(views, "verify_totp", synchronized_verify)

    def verify(client):
        try:
            response = client.post(reverse("accounts-mfa-verify"), {"token": code})
            return response.status_code == 302 and "_auth_user_id" in client.session
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        accepted = list(executor.map(verify, clients))
    assert sum(accepted) == 1
    assert (
        AuditEvent.objects.filter(action="authentication.login_succeeded", actor_user=user).count()
        == 1
    )
