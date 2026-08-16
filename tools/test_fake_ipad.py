"""Unit tests for the FireDash fake iPad client (no network, no Django, no DB).

These tests exercise protocol boundaries: canonical crypto, local state,
adoption/reactivation sequencing against a stubbed transport, authentication
header placement, secret redaction, and CLI exit codes.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import io
import json
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.fake_ipad import datasets  # noqa: E402
from tools.fake_ipad.cli import get_token, main  # noqa: E402
from tools.fake_ipad.client import FakeIPadClient  # noqa: E402
from tools.fake_ipad.crypto import (  # noqa: E402
    ADOPTION_PROTOCOL,
    HPKE_SUITE,
    HPKE_SUITE_NAME,
    CryptoState,
    canonical_json_bytes,
    challenge_proof,
    ed25519_from_bytes,
)
from tools.fake_ipad.errors import ClientError  # noqa: E402
from tools.fake_ipad.output import Output  # noqa: E402
from tools.fake_ipad.state import DeviceState, secure_write  # noqa: E402
from tools.fake_ipad.transport import HttpResponse, parse_problem, problem_text  # noqa: E402

# --- stubbed transport -------------------------------------------------------


class StubApi:
    """A recording stand-in for ``ApiClient`` that serves canned responses."""

    def __init__(self, *, server_url: str = "https://test.example"):
        self.server_url = server_url
        self.out = Output()
        self.calls: list[dict[str, Any]] = []
        self.handlers: dict[str, Callable[..., tuple[int, dict[str, Any], dict[str, str]]]] = {}

    def call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        bearer: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "json_body": json_body,
                "bearer": bearer,
                "headers": headers or {},
            }
        )
        handler = self.handlers.get(path)
        if handler is None:
            return HttpResponse(status=404, headers={}, body=b"{}", url=path)
        status, body, response_headers = handler(json_body, bearer)
        payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        return HttpResponse(status=status, headers=response_headers, body=payload, url=path)

    def expect(self, response: HttpResponse, *ok: int, label: str) -> HttpResponse:
        if response.status not in ok:
            raise ClientError(f"{label}: {problem_text(response)}")
        return response


def _client(state: DeviceState, api: StubApi, *, out: Output | None = None) -> FakeIPadClient:
    return FakeIPadClient(state, api, out=out or Output())  # type: ignore[arg-type]


# --- crypto ------------------------------------------------------------------


def test_public_key_is_65_byte_uncompressed():
    crypto = CryptoState.generate()
    raw = crypto.public_bytes()
    assert len(raw) == 65
    assert raw[0] == 0x04


def test_challenge_proof_matches_server_hmac_contract():
    nonce = b"n" * 32
    context = canonical_json_bytes({"a": 1, "b": 2})
    assert challenge_proof(nonce, context) == hmac.new(nonce, context, hashlib.sha256).digest()


def test_hpke_challenge_roundtrip():
    crypto = CryptoState.generate()
    info = canonical_json_bytes({"installation_uuid": str(uuid.uuid4())})
    nonce = os.urandom(32)
    encrypted = HPKE_SUITE.encrypt(nonce, crypto.private_key.public_key(), info=info)
    assert crypto.hpke_open(encrypted, info=info) == nonce


def test_private_key_pem_roundtrip():
    crypto = CryptoState.generate()
    restored = CryptoState.from_pem(crypto.private_pem())
    assert restored.public_bytes() == crypto.public_bytes()


@pytest.mark.parametrize("version", ["1.0", "1.0.0-beta", "01.0.0", "1.0.-1"])
def test_fake_app_version_identity_uses_frozen_three_component_grammar(
    tmp_path: Path, version: str
):
    with pytest.raises(ClientError, match="MAJOR.MINOR.PATCH"):
        FakeIPadClient(DeviceState(tmp_path / "state"), StubApi(), app_version=version)


# --- local state -------------------------------------------------------------


def test_ensure_identity_creates_and_reuses_installation_uuid(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="2.4.0")
    first = state.installation_uuid
    assert state.has_identity
    assert state.private_key_path.exists()

    again = DeviceState(tmp_path / "state")
    again.ensure_identity(server_url="https://test.example", app_version="2.4.0")
    assert again.installation_uuid == first


def test_store_installation_persists_credential(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    state.store_installation(
        installation_id=str(uuid.uuid4()),
        tablet_id=str(uuid.uuid4()),
        credential="credential-secret-value",
        authorization_valid_until="2026-08-16T00:00:00+00:00",
    )
    reloaded = DeviceState(tmp_path / "state")
    assert reloaded.is_adopted
    assert reloaded.credential == "credential-secret-value"


def test_reset_removes_only_local_state(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    state.store_installation(
        installation_id=str(uuid.uuid4()),
        tablet_id=str(uuid.uuid4()),
        credential="cred",
        authorization_valid_until="2026-08-16T00:00:00+00:00",
    )
    assert state.state_path.exists()
    removed = state.reset()
    assert "state.json" in removed
    assert not state.state_path.exists()
    assert not state.private_key_path.exists()


def test_local_summary_never_includes_credential_or_key(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    state.store_installation(
        installation_id=str(uuid.uuid4()),
        tablet_id=str(uuid.uuid4()),
        credential="credential-secret-value",
        authorization_valid_until="2026-08-16T00:00:00+00:00",
    )
    summary = json.dumps(state.local_summary())
    assert "credential-secret-value" not in summary
    assert "private" not in summary.lower() or "credential" not in summary.lower()


@pytest.mark.skipif(sys.platform == "win32", reason="owner-only mode is a POSIX behavior")
def test_secure_write_is_owner_only(tmp_path: Path):
    path = tmp_path / "secret.json"
    secure_write(path, b"{}")
    assert (path.stat().st_mode & 0o777) == 0o600


# --- adoption protocol -------------------------------------------------------


def _build_challenge(
    crypto: CryptoState,
    *,
    installation_uuid: str,
    mode: str = "adoption",
):
    request_id = str(uuid.uuid4())
    tablet_id = str(uuid.uuid4())
    expires_at = "2026-08-09T12:39:56.789012+00:00"
    context = {
        "adoption_request_id": request_id,
        "expires_at": expires_at,
        "hpke_ciphersuite": HPKE_SUITE_NAME,
        "hpke_public_key_fingerprint": crypto.fingerprint(),
        "installation_uuid": installation_uuid,
        "mode": mode,
        "protocol": ADOPTION_PROTOCOL,
        "tablet_id": tablet_id,
    }
    info = canonical_json_bytes(context)
    nonce = os.urandom(32)
    encrypted = HPKE_SUITE.encrypt(nonce, crypto.private_key.public_key(), info=info)
    expected_proof = challenge_proof(nonce, info)
    preview = {
        "adoption_request_id": request_id,
        "encrypted_challenge": base64.b64encode(encrypted).decode("ascii"),
        "expires_at": expires_at,
        "tablet_id": tablet_id,
        "hpke_ciphersuite": HPKE_SUITE_NAME,
        "hpke_public_key_fingerprint": crypto.fingerprint(),
        "mode": mode,
        "protocol": ADOPTION_PROTOCOL,
    }
    return preview, expected_proof


def test_adoption_flow_proof_credential_and_no_plaintext_token(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    crypto = state.ensure_identity(server_url="https://test.example", app_version="2.4.0")
    preview, expected_proof = _build_challenge(crypto, installation_uuid=state.installation_uuid)

    token = "out-of-band-invitation-token-1234567890"
    credential = "credential-issued-once"
    complete_calls: list[dict[str, Any]] = []

    api = StubApi()

    def preview_handler(body, bearer):
        assert body["token"] == token
        assert body["installation_uuid"] == state.installation_uuid
        assert body["app_version"] == "2.4.0"
        assert body["hpke_ciphersuite"] == HPKE_SUITE_NAME
        assert base64.b64decode(body["hpke_public_key"]) == crypto.public_bytes()
        return 201, preview, {}

    def complete_handler(body, bearer):
        complete_calls.append(body)
        if len(complete_calls) == 1:
            return (
                201,
                {
                    "installation_id": str(uuid.uuid4()),
                    "credential": credential,
                    "authorization_valid_until": "2026-08-16T00:00:00+00:00",
                    "server_time": "2026-08-09T00:00:00+00:00",
                },
                {},
            )
        return 409, {"detail": "already completed"}, {}

    api.handlers["/api/v1/adoption/preview"] = preview_handler
    api.handlers["/api/v1/adoption/complete"] = complete_handler

    client = _client(state, api)
    result = client.adopt(token, verify=False)

    # Complete request shape + proof correctness.
    assert complete_calls[0]["adoption_request_id"] == preview["adoption_request_id"]
    assert complete_calls[0]["confirmed"] is True
    assert base64.b64decode(complete_calls[0]["challenge_response"]) == expected_proof

    # Credential persisted; invitation token not persisted.
    assert state.credential == credential
    assert result["installation_id"] == state.installation_id
    persisted = state.state_path.read_text("utf-8")
    assert credential in persisted
    assert token not in persisted


def test_adoption_does_not_leak_credential_in_output(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    crypto = state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    preview, _ = _build_challenge(crypto, installation_uuid=state.installation_uuid)

    credential = "credential-secret-value-9876543210"
    api = StubApi()

    def preview_handler(body, bearer):
        return 201, preview, {}

    calls = {"n": 0}

    def complete_handler(body, bearer):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                201,
                {
                    "installation_id": str(uuid.uuid4()),
                    "credential": credential,
                    "authorization_valid_until": "2026-08-16T00:00:00+00:00",
                    "server_time": "2026-08-09T00:00:00+00:00",
                },
                {},
            )
        return 409, {"detail": "already completed"}, {}

    api.handlers["/api/v1/adoption/preview"] = preview_handler
    api.handlers["/api/v1/adoption/complete"] = complete_handler

    stream = io.StringIO()
    client = _client(state, api, out=Output(stream=stream))
    client.adopt("invitation-token-value", verify=False)

    assert credential not in stream.getvalue()
    assert "invitation-token-value" not in stream.getvalue()


def test_adoption_rejects_non_201_preview_with_problem(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    api = StubApi()

    def preview_handler(body, bearer):
        return (
            403,
            {"detail": "Invitation is invalid or expired."},
            {"content-type": "application/problem+json"},
        )

    api.handlers["/api/v1/adoption/preview"] = preview_handler
    client = _client(state, api)
    with pytest.raises(ClientError, match="invalid or expired"):
        client.adopt("bad-token", verify=False)


def test_adoption_rejects_malformed_json_response(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    api = StubApi()
    api.handlers["/api/v1/adoption/preview"] = lambda b, r: (201, b"not-json", {})
    client = _client(state, api)
    with pytest.raises(ClientError):
        client.adopt("token", verify=False)


# --- reactivation ------------------------------------------------------------


def test_reactivation_rotates_credential_and_old_is_rejected(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    crypto = state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    old_credential = "old-credential"
    new_credential = "new-credential"
    state.store_installation(
        installation_id=str(uuid.uuid4()),
        tablet_id=str(uuid.uuid4()),
        credential=old_credential,
        authorization_valid_until="2026-08-16T00:00:00+00:00",
    )
    preview, expected_proof = _build_challenge(
        crypto, installation_uuid=state.installation_uuid, mode="reactivation"
    )

    api = StubApi()
    rotated = {"value": False}

    def status_handler(body, bearer):
        if bearer == old_credential and not rotated["value"]:
            return (
                200,
                {
                    "status": "stale",
                    "authorization_valid_until": "x",
                    "purge_provisioned_data": False,
                    "server_time": "2026-08-09T00:00:00+00:00",
                },
                {},
            )
        if bearer == old_credential:
            return 403, {"detail": "replaced"}, {}
        if bearer == new_credential:
            return (
                200,
                {
                    "status": "active",
                    "authorization_valid_until": "y",
                    "purge_provisioned_data": False,
                    "server_time": "2026-08-09T00:00:00+00:00",
                },
                {},
            )
        return 401, {"detail": "invalid"}, {}

    def preview_handler(body, bearer):
        assert body["token"] == "reactivation-token"
        assert body["installation_uuid"] == state.installation_uuid
        return 201, preview, {}

    def complete_handler(body, bearer):
        assert bearer == old_credential
        rotated["value"] = True
        return (
            201,
            {
                "installation_id": state.installation_id,
                "credential": new_credential,
                "authorization_valid_until": "2026-08-16T00:00:00+00:00",
                "server_time": "2026-08-09T00:00:00+00:00",
            },
            {},
        )

    api.handlers["/api/v1/tablet/status"] = status_handler
    api.handlers["/api/v1/tablet/reactivation/preview"] = preview_handler
    api.handlers["/api/v1/tablet/reactivation/complete"] = complete_handler

    client = _client(state, api)
    client.reactivate("reactivation-token", verify=False)

    assert state.credential == new_credential
    assert rotated["value"] is True


# --- authentication ----------------------------------------------------------


def test_check_in_sends_bearer_credential(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    state.store_installation(
        installation_id=str(uuid.uuid4()),
        tablet_id=str(uuid.uuid4()),
        credential="the-bearer-credential",
        authorization_valid_until="2026-08-16T00:00:00+00:00",
    )
    api = StubApi()
    api.handlers["/api/v1/tablet/check-in"] = lambda b, r: (
        200,
        {
            "status": "active",
            "server_time": "2026-08-09T00:00:00+00:00",
            "authorization_valid_until": "2026-08-16T00:00:00+00:00",
        },
        {},
    )
    client = _client(state, api)
    client.check_in()

    check_in_calls = [c for c in api.calls if c["path"] == "/api/v1/tablet/check-in"]
    assert check_in_calls[0]["bearer"] == "the-bearer-credential"
    assert state.authorization_valid_until == "2026-08-16T00:00:00+00:00"


def test_lifecycle_telemetry_modes_and_server_time_are_persisted(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="1.2.0", app_build=25)
    state.store_installation(
        installation_id=str(uuid.uuid4()),
        tablet_id=str(uuid.uuid4()),
        credential="the-bearer-credential",
        authorization_valid_until="2026-08-16T00:00:00+00:00",
    )
    api = StubApi()
    api.handlers["/api/v1/tablet/check-in"] = lambda b, r: (
        200,
        {
            "status": "active",
            "server_time": "2026-08-09T00:00:00Z",
            "authorization_valid_until": "2026-08-16T00:00:00Z",
        },
        {},
    )
    client = _client(state, api)
    client.check_in()
    client.check_in(telemetry="version-only")
    client.check_in(telemetry="none")

    calls = [call for call in api.calls if call["path"] == "/api/v1/tablet/check-in"]
    assert calls[0]["headers"] == {
        "X-FireDash-App-Version": "1.2.0",
        "X-FireDash-App-Build": "25",
    }
    assert calls[1]["headers"] == {"X-FireDash-App-Version": "1.2.0"}
    assert calls[2]["headers"] == {}
    assert state.server_time == "2026-08-09T00:00:00Z"


def test_adoption_preview_sends_configured_app_build(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    crypto = state.ensure_identity(
        server_url="https://test.example", app_version="1.2.0", app_build=25
    )
    preview, _ = _build_challenge(crypto, installation_uuid=state.installation_uuid)
    api = StubApi()
    api.handlers["/api/v1/adoption/preview"] = lambda b, r: (201, preview, {})
    api.handlers["/api/v1/adoption/complete"] = lambda b, r: (
        201,
        {
            "installation_id": str(uuid.uuid4()),
            "credential": "credential",
            "authorization_valid_until": "2026-08-16T00:00:00Z",
            "server_time": "2026-08-09T00:00:00Z",
        },
        {},
    )
    _client(state, api).adopt("token", verify=False)
    preview_call = next(call for call in api.calls if call["path"].endswith("preview"))
    assert preview_call["json_body"]["app_version"] == "1.2.0"
    assert preview_call["json_body"]["app_build"] == 25


def test_client_update_required_is_parsed_by_stable_code(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    state.store_installation(
        installation_id=str(uuid.uuid4()),
        tablet_id=str(uuid.uuid4()),
        credential="credential",
        authorization_valid_until="2026-08-16T00:00:00Z",
    )
    api = StubApi()
    api.handlers["/api/v1/tablet/check-in"] = lambda b, r: (
        426,
        {
            "code": "client_update_required",
            "minimum_app_version": "1.2.0",
            "detail": "message text is not protocol state",
            "request_id": "request-1",
        },
        {"content-type": "application/problem+json"},
    )
    with pytest.raises(ClientError, match="fake app version 1.0.0, server minimum 1.2.0"):
        _client(state, api).check_in()
    problem = parse_problem(
        HttpResponse(
            status=426,
            headers={"content-type": "application/problem+json"},
            body=b'{"code":"client_update_required","minimum_app_version":"1.2.0"}',
            url="https://test.example/api/v1/tablet/check-in",
        )
    )
    assert (problem.status, problem.code, problem.minimum_app_version) == (
        426,
        "client_update_required",
        "1.2.0",
    )


def test_adoption_lost_response_replays_exact_completion_without_persisting_first_credential(
    tmp_path: Path,
):
    state = DeviceState(tmp_path / "state")
    crypto = state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    preview, _ = _build_challenge(crypto, installation_uuid=state.installation_uuid)
    first_credential, recovered_credential = "first-lost-credential", "recovered-credential"
    complete_bodies: list[dict[str, Any]] = []
    api = StubApi()
    api.handlers["/api/v1/adoption/preview"] = lambda b, r: (201, preview, {})
    installation_id = str(uuid.uuid4())

    def completion(body, bearer):
        assert bearer is None
        complete_bodies.append(body)
        return (
            201,
            {
                "installation_id": installation_id,
                "credential": first_credential
                if len(complete_bodies) == 1
                else recovered_credential,
                "authorization_valid_until": "2026-08-16T00:00:00Z",
                "server_time": "2026-08-09T00:00:00Z",
            },
            {},
        )

    api.handlers["/api/v1/adoption/complete"] = completion
    _client(state, api).adopt("token", verify=False, simulate_lost_completion_response=True)

    assert complete_bodies[0] == complete_bodies[1]
    assert state.credential == recovered_credential
    assert first_credential not in state.state_path.read_text("utf-8")


def test_reactivation_lost_response_replays_without_the_rotated_credential(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    crypto = state.ensure_identity(
        server_url="https://test.example", app_version="1.2.0", app_build=25
    )
    old_credential, first_credential, recovered_credential = "old", "first", "recovered"
    state.store_installation(
        installation_id=str(uuid.uuid4()),
        tablet_id=str(uuid.uuid4()),
        credential=old_credential,
        authorization_valid_until="2026-08-16T00:00:00Z",
    )
    preview, _ = _build_challenge(
        crypto, installation_uuid=state.installation_uuid, mode="reactivation"
    )
    api = StubApi()
    status_calls = 0

    def status(body, bearer):
        nonlocal status_calls
        status_calls += 1
        if status_calls == 1:
            return (
                200,
                {
                    "status": "stale",
                    "authorization_valid_until": "2026-08-16T00:00:00Z",
                    "purge_provisioned_data": False,
                    "server_time": "2026-08-09T00:00:00Z",
                },
                {},
            )
        if status_calls == 2:
            assert bearer == old_credential
            return 403, {"code": "invalid_credential"}, {}
        assert bearer == recovered_credential
        return (
            200,
            {
                "status": "active",
                "authorization_valid_until": "2026-08-16T00:00:00Z",
                "purge_provisioned_data": False,
                "server_time": "2026-08-09T00:00:00Z",
            },
            {},
        )

    completion_bearers: list[str | None] = []

    def completion(body, bearer):
        completion_bearers.append(bearer)
        return (
            201,
            {
                "installation_id": state.installation_id,
                "credential": first_credential
                if len(completion_bearers) == 1
                else recovered_credential,
                "authorization_valid_until": "2026-08-16T00:00:00Z",
                "server_time": "2026-08-09T00:00:00Z",
            },
            {},
        )

    api.handlers["/api/v1/tablet/status"] = status
    api.handlers["/api/v1/tablet/reactivation/preview"] = lambda b, r: (201, preview, {})
    api.handlers["/api/v1/tablet/reactivation/complete"] = completion
    _client(state, api).reactivate("token", verify=False, simulate_lost_completion_response=True)

    assert completion_bearers == [old_credential, None]
    assert state.credential == recovered_credential
    assert first_credential not in state.state_path.read_text("utf-8")


def test_signing_key_cache_is_exact_version_and_never_falls_back(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    state.store_installation(
        installation_id=str(uuid.uuid4()),
        tablet_id=str(uuid.uuid4()),
        credential="credential",
        authorization_valid_until="2026-08-16T00:00:00Z",
    )
    public_key = (
        ed25519.Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    api = StubApi()
    api.handlers["/api/v1/tablet/signing-keys/1"] = lambda b, r: (
        200,
        {
            "algorithm": "Ed25519",
            "version": "1",
            "public_key": base64.b64encode(public_key).decode(),
        },
        {},
    )
    client = _client(state, api)
    client.get_signing_key("1", {})
    client.get_signing_key("1", {})
    assert len([c for c in api.calls if c["path"].endswith("/1")]) == 1
    with pytest.raises(ClientError, match="signing-key 2"):
        client.get_signing_key("2", {})


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "algorithm": "ECDSA",
                "version": "2",
                "public_key": base64.b64encode(b"k" * 32).decode(),
            },
            "algorithm",
        ),
        (
            {
                "algorithm": "Ed25519",
                "version": "3",
                "public_key": base64.b64encode(b"k" * 32).decode(),
            },
            "mismatch",
        ),
        ({"algorithm": "Ed25519", "version": "2", "public_key": "not base64!"}, "Base64"),
        (
            {
                "algorithm": "Ed25519",
                "version": "2",
                "public_key": base64.b64encode(b"k" * 31).decode(),
            },
            "32 bytes",
        ),
    ],
)
def test_signing_key_rejects_wrong_algorithm_version_or_encoding(
    tmp_path: Path, payload: dict[str, str], message: str
):
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    state.store_installation(
        installation_id=str(uuid.uuid4()),
        tablet_id=str(uuid.uuid4()),
        credential="credential",
        authorization_valid_until="2026-08-16T00:00:00Z",
    )
    api = StubApi()
    api.handlers["/api/v1/tablet/signing-keys/2"] = lambda b, r: (200, payload, {})
    with pytest.raises(ClientError, match=message):
        _client(state, api).get_signing_key("2", {})


def test_complete_manifest_contract_fixture_uses_the_live_client_verifier(tmp_path: Path):
    fixture_path = (
        Path(__file__).resolve().parent.parent
        / "apps"
        / "publications"
        / "tests"
        / "fixtures"
        / "complete_manifest_contract.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    manifest = fixture["wire_manifest"]
    assert isinstance(manifest, dict)
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    client = _client(state, StubApi())

    canonical = client.canonical_manifest_payload(manifest)
    assert canonical == fixture["unsigned_canonical_manifest_ascii"].encode("ascii")
    assert hashlib.sha256(canonical).hexdigest() == fixture["unsigned_canonical_manifest_sha256"]
    client._verify_manifest_signature_with_key(
        manifest, ed25519_from_bytes(base64.b64decode(fixture["public_key"], validate=True))
    )
    response = HttpResponse(
        status=200,
        headers={"etag": fixture["expected_manifest_etag"]},
        body=json.dumps(manifest).encode("utf-8"),
        url="https://test.example/api/v1/tablet/manifest",
    )
    assert client.verify_manifest_etag(response, manifest) == fixture["expected_manifest_etag"]


def _signed_manifest_with_klgv_optional_dataset(*, required: bool):
    fixture_path = (
        Path(__file__).resolve().parent.parent
        / "apps"
        / "publications"
        / "tests"
        / "fixtures"
        / "complete_manifest_contract.json"
    )
    manifest = copy.deepcopy(json.loads(fixture_path.read_text(encoding="utf-8"))["wire_manifest"])
    dataset = copy.deepcopy(manifest["datasets"][0])
    publication_id = str(uuid.uuid4())
    dataset.update(
        {
            "publication_id": publication_id,
            "type": "department_klgv_plans",
            "scope": "department",
            "required": required,
            "artifact_format": "zip",
            "download_url": f"/api/v1/tablet/datasets/{publication_id}/download",
        }
    )
    manifest["datasets"].append(dataset)
    signer = ed25519.Ed25519PrivateKey.generate()
    manifest["signing_key_version"] = "1"
    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    manifest["signature"] = base64.b64encode(signer.sign(canonical_json_bytes(unsigned))).decode()
    return manifest, signer.public_key().public_bytes_raw()


def test_verified_manifest_ignores_unknown_optional_dataset_and_syncs_known_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manifest, public_key = _signed_manifest_with_klgv_optional_dataset(required=False)
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    state.store_installation(
        installation_id=manifest["configuration"]["installation_id"],
        tablet_id=manifest["configuration"]["tablet_id"],
        credential="test-credential",
        authorization_valid_until=manifest["authorization_valid_until"],
    )
    api = StubApi()
    api.handlers["/api/v1/tablet/signing-keys/1"] = lambda b, r: (
        200,
        {
            "algorithm": "Ed25519",
            "version": "1",
            "public_key": base64.b64encode(public_key).decode(),
        },
        {},
    )
    api.handlers["/api/v1/tablet/manifest"] = lambda b, r: (304, b"", {})
    client = _client(state, api)
    verified: list[str] = []
    monkeypatch.setattr(
        client,
        "verify_dataset",
        lambda dataset, config, crypto, cache: verified.append(dataset["type"]) or {},
    )

    etag_payload = copy.deepcopy(manifest)
    etag_payload.pop("generated_at")
    response = HttpResponse(
        status=200,
        headers={"etag": f'"{hashlib.sha256(canonical_json_bytes(etag_payload)).hexdigest()}"'},
        body=json.dumps(manifest).encode(),
        url="https://test.example/api/v1/tablet/manifest",
    )
    _, _, result = client.verify_manifest_and_datasets(response, manifest["configuration"])

    assert "department_klgv_plans" not in verified
    assert verified == [manifest["datasets"][0]["type"]]
    assert set(result) == set(verified)


def test_verified_manifest_rejects_unknown_required_dataset_before_activation(tmp_path: Path):
    manifest, public_key = _signed_manifest_with_klgv_optional_dataset(required=True)
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    state.store_installation(
        installation_id=manifest["configuration"]["installation_id"],
        tablet_id=manifest["configuration"]["tablet_id"],
        credential="test-credential",
        authorization_valid_until=manifest["authorization_valid_until"],
    )
    api = StubApi()
    api.handlers["/api/v1/tablet/signing-keys/1"] = lambda b, r: (
        200,
        {
            "algorithm": "Ed25519",
            "version": "1",
            "public_key": base64.b64encode(public_key).decode(),
        },
        {},
    )
    client = _client(state, api)
    client.verify_manifest_signature(manifest, {})
    client.validate_manifest_structure(manifest, manifest["configuration"])

    with pytest.raises(ClientError, match="required dataset is unsupported"):
        client.select_compatible_datasets(manifest)


def test_terminal_matrix_allows_status_only_and_denies_operational_endpoints(tmp_path: Path):
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    state.store_installation(
        installation_id=str(uuid.uuid4()),
        tablet_id=str(uuid.uuid4()),
        credential="replaced-credential",
        authorization_valid_until="2026-08-16T00:00:00Z",
    )
    api = StubApi()
    api.handlers["/api/v1/tablet/status"] = lambda b, r: (
        200,
        {
            "status": "replaced",
            "authorization_valid_until": "2026-08-16T00:00:00Z",
            "purge_provisioned_data": True,
            "server_time": "2026-08-09T00:00:00Z",
        },
        {},
    )
    for path in (
        "/api/v1/tablet/check-in",
        "/api/v1/tablet/refresh",
        "/api/v1/tablet/configuration",
        "/api/v1/tablet/signing-keys/1",
        "/api/v1/tablet/manifest",
    ):
        api.handlers[path] = lambda b, r: (403, {"code": "installation_replaced"}, {})
    result = _client(state, api).terminal_endpoint_matrix()
    assert result["status"] == "replaced"
    assert set(result["denied"]) == {
        "check-in",
        "refresh",
        "configuration",
        "signing-key",
        "manifest",
    }


# --- CLI ---------------------------------------------------------------------


def test_cli_reset_exits_zero(tmp_path: Path):
    state_dir = tmp_path / "state"
    DeviceState(state_dir).ensure_identity(server_url="https://test.example", app_version="1.0.0")
    assert main(["reset", "--state-dir", str(state_dir)]) == 0
    assert not (state_dir / "state.json").exists()


def test_cli_adopt_without_server_fails(tmp_path: Path):
    rc = main(["adopt", "--state-dir", str(tmp_path / "state"), "--token", "tok"])
    assert rc == 1


def test_get_token_reads_token_file(tmp_path: Path):
    token_file = tmp_path / "token.txt"
    token_file.write_text("file-token-value\n")
    args = SimpleNamespace(token=None, token_file=str(token_file))
    assert get_token(args, prompt="unused") == "file-token-value"


def test_get_token_prefers_explicit_argument(tmp_path: Path):
    args = SimpleNamespace(token="arg-token", token_file=None)
    assert get_token(args, prompt="unused") == "arg-token"


def test_get_token_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    args = SimpleNamespace(token=None, token_file="-")
    monkeypatch.setattr("sys.stdin", io.StringIO("stdin-token\n"))
    assert get_token(args, prompt="unused") == "stdin-token"


# --- dataset format validators (no network) ----------------------------------


def test_validate_hydrants_rejects_wrong_format(tmp_path: Path):
    plaintext = json.dumps({"type": "Feature", "schema_version": 1, "source_revision": 1}).encode()
    dataset = {"type": "department_hydrants", "schema_version": 1}
    with pytest.raises(ClientError):
        datasets.validate_hydrants(plaintext, dataset, Output())


def test_validate_plaintext_saves_artifact_when_requested(tmp_path: Path):
    plaintext = json.dumps(
        {"type": "FeatureCollection", "schema_version": 1, "source_revision": 1, "features": []}
    ).encode()
    dataset = {"type": "department_hydrants", "artifact_format": "geojson", "schema_version": 1}
    config = {"station_id": str(uuid.uuid4())}
    out = Output()
    datasets.validate_plaintext(
        plaintext, dataset, config, out, save_plaintext=True, state_dir=tmp_path / "state"
    )
    assert (tmp_path / "state" / "last-plaintext" / "department_hydrants.geojson").exists()
