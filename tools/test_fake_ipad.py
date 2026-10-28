"""Unit tests for the FireDash fake iPad client (no network, no Django, no DB).

These tests exercise protocol boundaries: canonical crypto, local state,
adoption sequencing against a stubbed transport, authentication
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
import zipfile
from argparse import Namespace
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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
from tools.fake_ipad.transport import (  # noqa: E402
    ApiClient,
    HttpResponse,
    parse_problem,
    problem_text,
)

# --- stubbed transport -------------------------------------------------------


class StubApi:
    """A recording stand-in for ``ApiClient`` that serves canned responses."""

    def __init__(self, *, server_url: str = "https://test.example"):
        self.server_url = server_url
        self.out = Output()
        self.calls: list[dict[str, Any]] = []
        self.handlers: dict[
            str, Callable[..., tuple[int, dict[str, Any] | bytes, dict[str, str]]]
        ] = {}

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
        FakeIPadClient(
            DeviceState(tmp_path / "state"), cast(ApiClient, StubApi()), app_version=version
        )


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
    body = check_in_calls[0]["json_body"]
    assert body == {}
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
    publication_id = str(uuid.uuid4())
    dataset = {
        "publication_id": publication_id,
        "type": "department_klgv_plans",
        "scope": {
            "type": "DEPARTMENT",
            "department_id": manifest["configuration"]["department_id"],
        },
        "version": 1,
        "schema_version": 2,
        "required": required,
        "minimum_app_version": None,
        "artifact_format": "document-manifest-v2",
        "manifest_url": f"/api/v1/tablet/document-generations/{publication_id}/manifest",
    }
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

    def record_verification(dataset, config, crypto, cache):
        verified.append(dataset["type"])
        return {}

    monkeypatch.setattr(client, "verify_dataset", record_verification)

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


@pytest.mark.parametrize(
    "scope",
    [
        "station",
        {"type": "STATION"},
        {"type": "STATION", "department_id": str(uuid.uuid4()), "station_id": str(uuid.uuid4())},
        {"type": "DEPARTMENT", "department_id": str(uuid.uuid4())},
        {"type": "SYSTEM", "department_id": str(uuid.uuid4())},
    ],
)
def test_explicit_scope_objects_reject_missing_or_cross_tenant_identity(scope):
    config = {"department_id": str(uuid.uuid4()), "station_id": str(uuid.uuid4())}
    with pytest.raises(ClientError):
        FakeIPadClient.scope_for_dataset({"scope": scope, "type": "station_files"}, config)


def test_explicit_scope_reconstructs_unchanged_hpke_identity():
    department, station = str(uuid.uuid4()), str(uuid.uuid4())
    config = {"department_id": department, "station_id": station}
    descriptor = {
        "type": "station_personnel",
        "scope": {"type": "STATION", "department_id": department, "station_id": station},
    }
    assert FakeIPadClient.scope_for_dataset(descriptor, config) == {
        "dataset_type_code": "station_personnel",
        "department_id": department,
        "station_id": station,
    }


def test_current_artifact_signature_scope_uses_explicit_type(tmp_path: Path, monkeypatch):
    fixture_path = (
        Path(__file__).resolve().parent.parent
        / "apps"
        / "publications"
        / "tests"
        / "fixtures"
        / "complete_manifest_contract.json"
    )
    manifest = json.loads(fixture_path.read_text(encoding="utf-8"))["wire_manifest"]
    dataset = manifest["datasets"][0]
    signer = ed25519.Ed25519PrivateKey.generate()
    payload = {
        "ciphertext_sha256": dataset["ciphertext_sha256"],
        "ciphertext_size": dataset["encrypted_size"],
        "encryption_algorithm": dataset["content_encryption_algorithm"],
        "kek_version": dataset["content_key_kek_version"],
        "nonce": dataset["content_encryption_nonce"],
        "schema_version": dataset["schema_version"],
        "scope": {"dataset_type_code": dataset["type"], **dataset["scope"]},
        "version_number": dataset["version"],
        "wrapped_cek": dataset["content_key_wrapped_for_kek"],
        "wrapping_algorithm": dataset["content_key_wrapping_algorithm"],
    }
    dataset["artifact_signature"] = base64.b64encode(
        signer.sign(canonical_json_bytes(payload))
    ).decode()
    client = _client(DeviceState(tmp_path / "state"), StubApi())
    monkeypatch.setattr(client, "get_signing_key", lambda *_: signer.public_key())
    client.verify_artifact_signature(dataset, manifest["configuration"], {})
    config = dict(manifest["configuration"], station_id=str(uuid.uuid4()))
    with pytest.raises(ClientError, match="scope differs"):
        client.verify_artifact_signature(dataset, config, {})


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
    args = Namespace(token=None, token_file=str(token_file))
    assert get_token(args, prompt="unused") == "file-token-value"


def test_get_token_prefers_explicit_argument(tmp_path: Path):
    args = Namespace(token="arg-token", token_file=None)
    assert get_token(args, prompt="unused") == "arg-token"


def test_get_token_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    args = Namespace(token=None, token_file="-")
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


def _fire_plan_entry(**overrides: Any) -> dict[str, Any]:
    plan_id = str(uuid.uuid4())
    entry: dict[str, Any] = {
        "id": plan_id,
        "external_identifier": "SITE-A",
        "object_name": "Das Rauhe Haus",
        "address": "Am Stadtrand 56 und 56 a",
        "postal_code": "22047",
        "city": "Hamburg",
        "longitude": 10.09873774,
        "latitude": 53.59229519,
        "page_count": 12,
        "path": f"plans/{plan_id}.pdf",
    }
    entry.update(overrides)
    return entry


def _fire_plan_archive(entries: list[dict[str, Any]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry in entries:
            pdf = b"%PDF-1.4\n%%EOF\n" + json.dumps(entry, sort_keys=True).encode()
            entry["sha256"] = hashlib.sha256(pdf).hexdigest()
            archive.writestr(entry["path"], pdf)
        archive.writestr(
            "manifest.json",
            json.dumps({"source_revision": 42, "fire_plans": entries}, sort_keys=True).encode(),
        )
    return output.getvalue()


def test_validate_fire_plans_accepts_extended_manifest():
    archive = _fire_plan_archive([_fire_plan_entry()])
    dataset = {"type": "department_fire_plans", "schema_version": 1}
    result = datasets.validate_fire_plans(archive, dataset, Output())
    assert result["fire_plans"] == 1
    assert result["with_address"] == 1
    assert result["with_location"] == 1


def test_validate_fire_plans_accepts_nullable_fields():
    entry = _fire_plan_entry(
        external_identifier=None, object_name=None, longitude=None, latitude=None
    )
    archive = _fire_plan_archive([entry])
    dataset = {"type": "department_fire_plans", "schema_version": 1}
    result = datasets.validate_fire_plans(archive, dataset, Output())
    assert result["with_address"] == 1
    assert result["with_location"] == 0


def test_validate_fire_plans_requires_new_metadata():
    entry = _fire_plan_entry()
    del entry["longitude"]
    archive = _fire_plan_archive([entry])
    dataset = {"type": "department_fire_plans", "schema_version": 1}
    with pytest.raises(ClientError):
        datasets.validate_fire_plans(archive, dataset, Output())


# --- SYSTEM Dangerous Goods --------------------------------------------------


def _dangerous_goods_document():
    return {
        "dataset_type": "dangerous_goods",
        "schema_version": 1,
        "metadata": {
            "publication_profile": "compact",
            "record_count": 1,
            "eri_card_count": 1,
            "adr_edition": "2025",
            "ericards_version": "2025.1",
            "placard_catalog": {
                "available_assets": {code: "bundled.svg" for code in ("3", "7A", "7B", "7C")},
                "special_values": {
                    "7X": {
                        "kind": "variable",
                        "candidate_codes": ["7A", "7B", "7C"],
                        "selection_basis": "transport_index_and_dose_rate",
                    }
                },
            },
        },
        "goods": [
            {
                "id": "bam-example",
                "un_number": "1203",
                "names": {
                    "official": {"de": "BENZIN", "es": "GASOLINA"},
                    "aliases": {"de": ["MOTORBENZIN"]},
                },
                "adr": {"class": "3", "placards": ["3"]},
                "eri": ["3-01"],
            }
        ],
        "eri_cards": {"3-01": [["title", "BENZIN"], ["heading", "Gefahr"], ["item", "Text"]]},
        "eri_defaults": {"1203": "3-01"},
        "sources": [
            {
                "id": source_id,
                "provider": source_id,
                "dataset": source_id,
                "source_file": "example.json",
                "sha256": "example",
                "source_url": "https://example.test",
                "legal": (
                    {"legal_url": "", "license": {}, "attribution": {}, "processing": {}}
                    if source_id == "bam"
                    else {
                        "terms_url": "",
                        "guidance_url": "",
                        "disclaimer_url": "",
                        "attribution": {},
                        "reproduction": {},
                    }
                ),
            }
            for source_id in ("bam", "ericards")
        ],
    }


def _dangerous_goods_descriptor():
    return {
        "type": "dangerous_goods",
        "scope": {"type": "SYSTEM"},
        "schema_version": 1,
        "required": True,
        "artifact_format": "json",
        "minimum_app_version": None,
    }


def test_validate_dangerous_goods_preserves_plaintext_and_reports_summary(tmp_path):
    plaintext = json.dumps(_dangerous_goods_document(), indent=2, ensure_ascii=False).encode()
    summary = datasets.validate_plaintext(
        plaintext,
        _dangerous_goods_descriptor(),
        {},
        Output(),
        save_plaintext=True,
        state_dir=tmp_path,
    )
    assert summary == {
        "record_count": 1,
        "eri_card_count": 1,
        "adr_edition": "2025",
        "ericards_version": "2025.1",
        "sha256": hashlib.sha256(plaintext).hexdigest(),
    }
    assert (tmp_path / "last-plaintext" / "dangerous_goods.json").read_bytes() == plaintext


@pytest.mark.parametrize("plaintext", [b"{", b"\xff", b"[]", b"null", b'{"extra":NaN}'])
def test_dangerous_goods_rejects_invalid_json(plaintext):
    with pytest.raises(ClientError, match="dangerous_goods"):
        datasets.validate_dangerous_goods(plaintext, _dangerous_goods_descriptor(), Output())


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("dataset_type",), "other"),
        (("schema_version",), 2),
        (("schema_version",), True),
        (("metadata", "publication_profile"), "full"),
        (("metadata", "record_count"), 2),
        (("metadata", "eri_card_count"), 0),
        (("metadata", "record_count"), True),
        (("metadata", "eri_card_count"), 1.0),
        (("goods",), {}),
        (("eri_cards",), []),
        (("eri_defaults",), []),
        (("goods", 0, "id"), None),
        (("goods", 0, "un_number"), 1203),
        (("goods", 0, "names", "official"), {}),
        (("goods", 0, "names", "aliases"), {"de": "alias"}),
        (("goods", 0, "adr", "class"), 3),
        (("goods", 0, "adr", "placards"), ["7D"]),
        (
            ("goods", 0, "adr", "placards"),
            [
                {
                    "kind": "variable",
                    "candidate_codes": ["7D"],
                    "selection_basis": "transport_index_and_dose_rate",
                }
            ],
        ),
        (("goods", 0, "eri"), ["missing"]),
        (("eri_defaults", "1203"), "missing"),
        (("eri_cards", "3-01"), [["item", "missing title"]]),
        (("eri_cards", "3-01"), [[[], "invalid kind"]]),
        (("sources",), []),
        (("sources", 0, "id"), []),
    ],
)
def test_dangerous_goods_rejects_invalid_schema(path, value):
    document = _dangerous_goods_document()
    target = document
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ClientError, match="dangerous_goods"):
        datasets.validate_dangerous_goods(
            json.dumps(document).encode(),
            _dangerous_goods_descriptor(),
            Output(),
        )


@pytest.mark.parametrize(
    "placard",
    [
        "3",
        {"kind": "conditional", "code": "3"},
        {
            "kind": "variable",
            "candidate_codes": ["7A", "7B", "7C"],
            "selection_basis": "transport_index_and_dose_rate",
        },
        {"kind": "none"},
        {"kind": "reference", "reference": "ADR 5.2.2.1.12"},
    ],
)
@pytest.mark.parametrize("eri", [None, [], ["3-01"]])
def test_dangerous_goods_accepts_documented_record_representations(placard, eri):
    document = _dangerous_goods_document()
    document["goods"][0]["adr"]["placards"] = [placard]
    document["goods"][0]["eri"] = eri
    summary = datasets.validate_dangerous_goods(
        json.dumps(document).encode(),
        _dangerous_goods_descriptor(),
        Output(),
    )
    assert summary["record_count"] == 1


@pytest.mark.parametrize(
    "scope",
    [
        {"type": "DEPARTMENT", "department_id": "department"},
        {"type": "STATION", "department_id": "department", "station_id": "station"},
        {"type": "SYSTEM", "department_id": "department"},
        {"type": "SYSTEM", "station_id": "station"},
        {"type": "SYSTEM", "department_id": None, "station_id": None},
    ],
)
def test_dangerous_goods_requires_exact_system_scope(scope):
    descriptor = dict(_dangerous_goods_descriptor(), scope=scope)
    with pytest.raises(ClientError, match="exactly SYSTEM"):
        FakeIPadClient.scope_for_dataset(
            descriptor,
            {"department_id": "department", "station_id": "station"},
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"artifact_format": "zip"},
        {"minimum_app_version": "99.0.0"},
    ],
)
def test_required_dangerous_goods_cannot_be_skipped(tmp_path, overrides):
    descriptor = dict(_dangerous_goods_descriptor(), **overrides)
    with pytest.raises(ClientError, match="dangerous_goods: required dataset is unsupported"):
        _client(DeviceState(tmp_path), StubApi()).select_compatible_datasets(
            {"datasets": [descriptor]}
        )


def _dangerous_goods_delivery(tmp_path, monkeypatch, *, invalid_plaintext=False):
    """Real signatures, HPKE and AES-GCM over synthetic data; only transport is stubbed."""
    fixture_path = (
        Path(__file__).resolve().parent.parent
        / "apps"
        / "publications"
        / "tests"
        / "fixtures"
        / "complete_manifest_contract.json"
    )
    manifest = json.loads(fixture_path.read_text(encoding="utf-8"))["wire_manifest"]
    descriptor = dict(manifest["datasets"][0], **_dangerous_goods_descriptor())
    manifest["datasets"] = [descriptor]
    config = manifest["configuration"]
    state = DeviceState(tmp_path / "state")
    state.ensure_identity(server_url="https://test.example", app_version="1.0.0")
    state.store_installation(
        installation_id=config["installation_id"],
        tablet_id=config["tablet_id"],
        credential="test-credential",
        authorization_valid_until=manifest["authorization_valid_until"],
    )
    plaintext = b"{" if invalid_plaintext else json.dumps(_dangerous_goods_document()).encode()
    cek, nonce = os.urandom(32), os.urandom(12)
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext, None)
    descriptor.update(
        encrypted_size=len(ciphertext),
        ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest(),
        content_encryption_nonce=base64.b64encode(nonce).decode(),
    )
    info = {
        "ciphertext_sha256": descriptor["ciphertext_sha256"],
        "installation_id": config["installation_id"],
        "protocol": "firedash-hpke-v1",
        "publication_id": descriptor["publication_id"],
        "schema_version": 1,
        "scope": {
            "dataset_type_code": "dangerous_goods",
            "department_id": None,
            "station_id": None,
        },
        "tablet_id": config["tablet_id"],
        "version_number": descriptor["version"],
    }
    encrypted = HPKE_SUITE.encrypt(
        cek, state.load_private_key().private_key.public_key(), info=canonical_json_bytes(info)
    )
    descriptor["key_grant"].update(
        encapsulated_key=base64.b64encode(encrypted[:65]).decode(),
        wrapped_content_key=base64.b64encode(encrypted[65:]).decode(),
    )
    signer = ed25519.Ed25519PrivateKey.generate()
    payload = {
        "ciphertext_sha256": descriptor["ciphertext_sha256"],
        "ciphertext_size": len(ciphertext),
        "encryption_algorithm": descriptor["content_encryption_algorithm"],
        "kek_version": descriptor["content_key_kek_version"],
        "nonce": descriptor["content_encryption_nonce"],
        "schema_version": 1,
        "scope": {"dataset_type_code": "dangerous_goods", "type": "SYSTEM"},
        "version_number": descriptor["version"],
        "wrapped_cek": descriptor["content_key_wrapped_for_kek"],
        "wrapping_algorithm": descriptor["content_key_wrapping_algorithm"],
    }
    descriptor["artifact_signature"] = base64.b64encode(
        signer.sign(canonical_json_bytes(payload))
    ).decode()
    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    manifest["signature"] = base64.b64encode(signer.sign(canonical_json_bytes(unsigned))).decode()
    etag_payload = {key: value for key, value in manifest.items() if key != "generated_at"}
    etag = f'"{hashlib.sha256(canonical_json_bytes(etag_payload)).hexdigest()}"'
    response = HttpResponse(
        status=200,
        headers={"etag": etag},
        body=json.dumps(manifest).encode(),
        url="https://test.example/api/v1/tablet/manifest",
    )
    api = StubApi()
    api.handlers[descriptor["download_url"]] = lambda b, r: (
        (
            200,
            ciphertext,
            {
                "content-type": "application/octet-stream",
                "accept-ranges": "bytes",
                "etag": f'"{descriptor["ciphertext_sha256"]}"',
            },
        )
        if sum(c["path"] == descriptor["download_url"] for c in api.calls) == 1
        else (304, b"", {})
    )
    stream = io.StringIO()
    client = _client(state, api, out=Output(stream=stream))
    monkeypatch.setattr(client, "get_signing_key", lambda *_: signer.public_key())
    monkeypatch.setattr(
        client, "get_status", lambda: {"status": "active", "purge_provisioned_data": False}
    )
    monkeypatch.setattr(client, "check_in", lambda: {})
    monkeypatch.setattr(client, "get_configuration", lambda: config)
    monkeypatch.setattr(
        client,
        "fetch_manifest",
        lambda **kw: HttpResponse(status=304, headers={}, body=b"", url=response.url)
        if kw.get("if_none_match")
        else response,
    )
    return client, descriptor, config, stream


def test_verify_reports_validated_system_dangerous_goods(tmp_path, monkeypatch):
    client, descriptor, config, stream = _dangerous_goods_delivery(tmp_path, monkeypatch)
    assert client.scope_for_dataset(descriptor, config) == {
        "dataset_type_code": "dangerous_goods",
        "department_id": None,
        "station_id": None,
    }
    result = client.verify()
    assert result["datasets"]["dangerous_goods"]["record_count"] == 1
    assert client.state.state["last_dataset_validation"] == result["datasets"]
    assert "DATASET dangerous_goods" in stream.getvalue()
    assert "VERIFY RESULT: PASS" in stream.getvalue()


def test_verify_fails_when_required_dangerous_goods_is_malformed(tmp_path, monkeypatch):
    client, _, _, stream = _dangerous_goods_delivery(tmp_path, monkeypatch, invalid_plaintext=True)
    with pytest.raises(ClientError, match="UTF-8 JSON"):
        client.verify()
    assert "VERIFY RESULT: PASS" not in stream.getvalue()
    assert "last_dataset_validation" not in client.state.state


def test_dangerous_goods_descriptor_must_be_required(tmp_path, monkeypatch):
    client, _, config, _ = _dangerous_goods_delivery(tmp_path, monkeypatch)
    with pytest.raises(ClientError, match="required=true"):
        manifest = client.fetch_manifest().json()
        manifest["datasets"][0]["required"] = False
        client.validate_manifest_structure(manifest, config)


@pytest.mark.parametrize("owner", ["department_id", "station_id"])
def test_dangerous_goods_hpke_rejects_installation_owners_in_context(tmp_path, monkeypatch, owner):
    client, descriptor, config, _ = _dangerous_goods_delivery(tmp_path, monkeypatch)
    crypto = client.state.load_private_key()
    # A correctly bound grant is reusable even if the receiving owner's IDs change.
    assert (
        len(
            client.unwrap_cek(
                descriptor,
                dict(config, department_id=str(uuid.uuid4()), station_id=str(uuid.uuid4())),
                crypto,
            )
        )
        == 32
    )
    info = {
        "ciphertext_sha256": descriptor["ciphertext_sha256"],
        "installation_id": config["installation_id"],
        "protocol": "firedash-hpke-v1",
        "publication_id": descriptor["publication_id"],
        "schema_version": 1,
        "scope": {
            "dataset_type_code": "dangerous_goods",
            "department_id": None,
            "station_id": None,
        },
        "tablet_id": config["tablet_id"],
        "version_number": descriptor["version"],
    }
    info["scope"][owner] = config[owner]
    encrypted = HPKE_SUITE.encrypt(
        os.urandom(32), crypto.private_key.public_key(), info=canonical_json_bytes(info)
    )
    descriptor["key_grant"].update(
        encapsulated_key=base64.b64encode(encrypted[:65]).decode(),
        wrapped_content_key=base64.b64encode(encrypted[65:]).decode(),
    )
    with pytest.raises(ClientError, match="HPKE open failed"):
        client.unwrap_cek(descriptor, config, crypto)
