from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from time import time

import pytest
from django.db import DatabaseError, connections
from django.test import Client
from django.urls import reverse
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts import views
from apps.accounts.models import User
from apps.accounts.services import enrollment_device
from apps.audit.models import AuditEvent


def pending_client(user):
    client = Client()
    session = client.session
    session["pending_mfa_user_id"] = str(user.pk)
    session["pending_mfa_started_at"] = time()
    session.save()
    return client


def code(device, timestamp):
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    totp.time = timestamp
    return f"{totp.token():0{device.digits}d}"


def concurrent_calls(calls):
    def run(call):
        try:
            return call()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(run, calls))


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("modes", [("reauth", "reauth"), ("login", "reauth"), ("enroll", "enroll")])
def test_shared_replay_boundary(monkeypatch, modes):
    enrolling = modes[0] == "enroll"
    user = User.objects.create_user("concurrent@example.test", "Concurrent", "password")
    user.mfa_enabled = not enrolling
    user.save()
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=not enrolling)
    timestamp = time()
    monkeypatch.setattr("django_otp.plugins.otp_totp.models.time.time", lambda: timestamp)
    monkeypatch.setattr(
        "django.utils.timezone.now", lambda: datetime.fromtimestamp(timestamp, tz=UTC)
    )
    token = code(device, timestamp)
    clients = [pending_client(user), pending_client(user)]
    for client, mode in zip(clients, modes, strict=True):
        if mode == "reauth":
            client.force_login(user)
    barrier = Barrier(2, timeout=15)
    original = views.verify_totp

    def ready(**kwargs):
        barrier.wait()  # outside the transaction and device lock
        return original(**kwargs)

    monkeypatch.setattr(views, "verify_totp", ready)
    urls = {
        "reauth": "accounts-reauthenticate",
        "login": "accounts-mfa-verify",
        "enroll": "accounts-mfa-enroll",
    }
    calls = [
        lambda c=c, m=m: c.post(reverse(urls[m]), {"token": token})
        for c, m in zip(clients, modes, strict=True)
    ]
    responses = concurrent_calls(calls)
    assert sum(r.status_code == 302 and r.url == reverse("dashboard") for r in responses) == 1
    assert sum("recent_reauthentication_at" in c.session for c in clients) == 1
    for client, mode in zip(clients, modes, strict=True):
        if mode != "reauth" and "recent_reauthentication_at" not in client.session:
            assert "_auth_user_id" not in client.session
    assert (
        AuditEvent.objects.filter(
            actor_user=user,
            action__in=[
                "authentication.reauthenticated",
                "authentication.login_succeeded",
                "authentication.mfa_enrolled",
            ],
        ).count()
        == 1
    )
    user.refresh_from_db()
    device.refresh_from_db()
    assert user.mfa_enabled and device.confirmed and device.last_t >= 0
    assert TOTPDevice.objects.filter(user=user).count() == 1


@pytest.mark.django_db(transaction=True)
def test_enrollment_creation_is_serialized_and_never_overwrites_confirmation():
    user = User.objects.create_user("initialize@example.test", "Initialize", "password")
    barrier = Barrier(2, timeout=15)

    def initialize():
        barrier.wait()
        return enrollment_device(user).pk

    ids = concurrent_calls([initialize, initialize])
    assert ids[0] == ids[1]
    assert TOTPDevice.objects.filter(user=user).count() == 1
    device = TOTPDevice.objects.get(pk=ids[0])
    device.confirmed = True
    device.save()
    assert enrollment_device(user) is None
    device.refresh_from_db()
    assert device.confirmed


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["login", "reauth", "enroll"])
def test_persistence_failure_does_not_publish_success(monkeypatch, mode):
    user = User.objects.create_user("rollback@example.test", "Rollback", "password")
    user.mfa_enabled = mode != "enroll"
    user.save()
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=mode != "enroll")
    client = pending_client(user)
    if mode == "reauth":
        client.force_login(user)
    original = TOTPDevice.verify_token

    def fail_after_save(self, token):
        assert original(self, token)
        raise DatabaseError("Disposable persistence failure")

    monkeypatch.setattr(TOTPDevice, "verify_token", fail_after_save)
    urls = {
        "login": "accounts-mfa-verify",
        "reauth": "accounts-reauthenticate",
        "enroll": "accounts-mfa-enroll",
    }
    with pytest.raises(DatabaseError):
        client.post(reverse(urls[mode]), {"token": code(device, time())})
    assert "recent_reauthentication_at" not in client.session
    if mode != "reauth":
        assert "_auth_user_id" not in client.session
    device.refresh_from_db()
    user.refresh_from_db()
    assert device.last_t == -1
    assert device.confirmed == (mode != "enroll")
    assert user.mfa_enabled == (mode != "enroll")
    assert not AuditEvent.objects.filter(
        action__in=[
            "authentication.login_succeeded",
            "authentication.reauthenticated",
            "authentication.mfa_enrolled",
        ]
    ).exists()


@pytest.mark.django_db
def test_reuse_rejected_but_later_code_accepted(monkeypatch):
    user = User.objects.create_user("sequential@example.test", "Sequential", "password")
    user.mfa_enabled = True
    user.save()
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    timestamp = time()
    monkeypatch.setattr("django_otp.plugins.otp_totp.models.time.time", lambda: timestamp)
    monkeypatch.setattr(
        "django.utils.timezone.now", lambda: datetime.fromtimestamp(timestamp, tz=UTC)
    )
    token = code(device, timestamp)
    first, second = pending_client(user), pending_client(user)
    url = reverse("accounts-mfa-verify")
    assert first.post(url, {"token": token}).status_code == 302
    assert second.post(url, {"token": token}).status_code == 200
    assert "_auth_user_id" not in second.session
    timestamp += device.step * 2
    assert second.post(url, {"token": code(device, timestamp)}).status_code == 302
    assert "_auth_user_id" in second.session


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("mode", ["login", "reauth", "enroll"])
def test_commit_failure_does_not_publish_success(monkeypatch, mode):
    from django.db import connection

    user = User.objects.create_user("commit@example.test", "Commit", "password")
    user.mfa_enabled = mode != "enroll"
    user.save()
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=mode != "enroll")
    client = pending_client(user)
    if mode == "reauth":
        client.force_login(user)
    original = TOTPDevice.verify_token
    real_commit = connection.commit

    def fail_commit():
        monkeypatch.setattr(connection, "commit", real_commit)
        raise DatabaseError("Disposable commit failure")

    def arm_failure(self, token):
        result = original(self, token)
        assert result
        monkeypatch.setattr(connection, "commit", fail_commit)
        return result

    monkeypatch.setattr(TOTPDevice, "verify_token", arm_failure)
    url = {
        "login": "accounts-mfa-verify",
        "reauth": "accounts-reauthenticate",
        "enroll": "accounts-mfa-enroll",
    }[mode]
    with pytest.raises(DatabaseError):
        client.post(reverse(url), {"token": code(device, time())})
    assert "recent_reauthentication_at" not in client.session
    if mode != "reauth":
        assert "_auth_user_id" not in client.session
    device.refresh_from_db()
    user.refresh_from_db()
    assert device.last_t == -1
    assert user.mfa_enabled == (mode != "enroll")
    assert device.confirmed == (mode != "enroll")
    assert not AuditEvent.objects.filter(
        action__in=[
            "authentication.login_succeeded",
            "authentication.reauthenticated",
            "authentication.mfa_enrolled",
        ]
    ).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["login", "enroll"])
def test_expired_pending_session_cannot_verify(mode):
    from django.conf import settings

    user = User.objects.create_user("expired@example.test", "Expired", "password")
    user.mfa_enabled = mode == "login"
    user.save()
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=mode == "login")
    client = pending_client(user)
    session = client.session
    session["pending_mfa_started_at"] = time() - settings.PRE_MFA_SESSION_MAX_AGE_SECONDS - 1
    session.save()
    response = client.post(
        reverse("accounts-mfa-verify" if mode == "login" else "accounts-mfa-enroll"),
        {"token": code(device, time())},
    )
    assert response.status_code == 302 and response.url == reverse("accounts-login")
    assert "_auth_user_id" not in client.session
    assert "recent_reauthentication_at" not in client.session
    device.refresh_from_db()
    assert device.last_t == -1


@pytest.mark.django_db
def test_device_throttling_remains_effective(monkeypatch):
    user = User.objects.create_user("throttle@example.test", "Throttle", "password")
    user.mfa_enabled = True
    user.save()
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    timestamp = time()
    monkeypatch.setattr("django_otp.plugins.otp_totp.models.time.time", lambda: timestamp)
    monkeypatch.setattr(
        "django.utils.timezone.now", lambda: datetime.fromtimestamp(timestamp, tz=UTC)
    )
    valid = code(device, timestamp)
    invalid = "000001" if valid != "000001" else "000002"
    client = pending_client(user)
    url = reverse("accounts-mfa-verify")
    assert client.post(url, {"token": invalid}).status_code == 200
    device.refresh_from_db()
    assert device.throttling_failure_count == 1 and device.last_t == -1
    assert client.post(url, {"token": valid}).status_code == 200
    assert "_auth_user_id" not in client.session
    timestamp += 2
    assert client.post(url, {"token": code(device, timestamp)}).status_code == 302


@pytest.mark.django_db(transaction=True)
def test_verification_refuses_enclosing_transaction():
    from django.db import transaction

    from apps.accounts.services import verify_totp

    user = User.objects.create_user("outer@example.test", "Outer", "password")
    user.mfa_enabled = True
    user.save()
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    with transaction.atomic():
        with pytest.raises(RuntimeError, match="durable atomic block"):
            verify_totp(user=user, token=code(device, time()))
    device.refresh_from_db()
    assert device.last_t == -1
