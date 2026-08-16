"""Fake iPad protocol client: adoption, reactivation, and authenticated ops.

This class sequences the real FireDash tablet protocol against the HTTPS API.
It owns no HTTP (see ``transport.ApiClient``), no key material of its own (see
``crypto.CryptoState``), and no persistence (see ``state.DeviceState``); it
orchestrates the three.
"""

from __future__ import annotations

import base64
import copy
import hmac
import re
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from tools.fake_ipad import datasets
from tools.fake_ipad.crypto import (
    ADOPTION_PROTOCOL,
    GRANT_PROTOCOL,
    HPKE_SUITE_NAME,
    CryptoState,
    aes_gcm_decrypt,
    canonical_json_bytes,
    challenge_proof,
    ed25519_from_bytes,
    sha256_hex,
    strict_b64,
)
from tools.fake_ipad.errors import fail
from tools.fake_ipad.output import Output
from tools.fake_ipad.state import DeviceState, secure_write_json
from tools.fake_ipad.transport import ApiClient, HttpResponse, problem_text
from tools.fake_ipad.validation import (
    require_app_build,
    require_app_version,
    require_keys,
    require_timestamp,
    require_uuid,
)

ADOPTION_PREVIEW_PATH = "/api/v1/adoption/preview"
ADOPTION_COMPLETE_PATH = "/api/v1/adoption/complete"
REACTIVATION_PREVIEW_PATH = "/api/v1/tablet/reactivation/preview"
REACTIVATION_COMPLETE_PATH = "/api/v1/tablet/reactivation/complete"
CHECK_IN_PATH = "/api/v1/tablet/check-in"
REFRESH_PATH = "/api/v1/tablet/refresh"
STATUS_PATH = "/api/v1/tablet/status"
CONFIGURATION_PATH = "/api/v1/tablet/configuration"
MANIFEST_PATH = "/api/v1/tablet/manifest"


class FakeIPadClient:
    def __init__(
        self,
        state: DeviceState,
        api: ApiClient,
        *,
        app_version: str | None = None,
        app_build: int | None = None,
        verbose: bool = False,
        save_plaintext: bool = False,
        out: Output | None = None,
    ) -> None:
        self.state = state
        self.api = api
        self.out = out or Output()
        self.app_version = require_app_version(
            app_version or state.app_version, label="app_version"
        )
        self.app_build = (
            require_app_build(app_build, label="app_build")
            if app_build is not None
            else state.app_build
        )
        self.verbose = verbose
        self.save_plaintext = save_plaintext

    # --- shared protocol helpers ------------------------------------------

    def telemetry_headers(self, mode: str = "configured") -> dict[str, str]:
        """Return optional lifecycle telemetry without sending a request body."""
        if mode == "none":
            return {}
        if mode not in {"configured", "version-only"}:
            fail(f"Unsupported telemetry mode: {mode!r}")
        headers = {"X-FireDash-App-Version": self.app_version}
        if mode == "configured" and self.app_build is not None:
            headers["X-FireDash-App-Build"] = str(self.app_build)
        return headers

    def _expect(self, response: HttpResponse, *ok: int, label: str) -> HttpResponse:
        """Report v1 problem codes deterministically, especially 426."""
        if response.status == 426:
            problem = response.json()
            if problem.get("code") != "client_update_required":
                fail(f"{label}: HTTP 426 without code=client_update_required")
            minimum = problem.get("minimum_app_version")
            if not isinstance(minimum, str):
                fail(f"{label}: HTTP 426 missing minimum_app_version")
            fail(
                f"{label}: client_update_required; fake app version {self.app_version}, "
                f"server minimum {minimum}. Update the fake app identity and retry."
            )
        return self.api.expect(response, *ok, label=label)

    @staticmethod
    def _require_server_time(data: dict[str, Any], *, label: str) -> str:
        require_keys(data, ["server_time"], label=label)
        return require_timestamp(data["server_time"], label=f"{label}.server_time")

    def _adoption_context(self, preview: dict[str, Any], *, mode: str) -> bytes:
        require_keys(
            preview,
            [
                "adoption_request_id",
                "expires_at",
                "hpke_ciphersuite",
                "hpke_public_key_fingerprint",
                "tablet_id",
                "mode",
                "protocol",
            ],
            label=f"{mode} preview",
        )
        if preview["mode"] != mode:
            fail(f"Preview mode mismatch: expected {mode!r}, got {preview['mode']!r}")
        if preview["protocol"] != ADOPTION_PROTOCOL:
            fail(f"Unexpected adoption protocol: {preview['protocol']!r}")
        if preview["hpke_ciphersuite"] != HPKE_SUITE_NAME:
            fail("Unexpected adoption HPKE ciphersuite")

        context = {
            "adoption_request_id": preview["adoption_request_id"],
            "expires_at": preview["expires_at"],
            "hpke_ciphersuite": preview["hpke_ciphersuite"],
            "hpke_public_key_fingerprint": preview["hpke_public_key_fingerprint"],
            "installation_uuid": self.state.installation_uuid,
            "mode": preview["mode"],
            "protocol": preview["protocol"],
            "tablet_id": preview["tablet_id"],
        }
        return canonical_json_bytes(context)

    def _preview_and_proof(
        self, *, token: str, mode: str, crypto: CryptoState
    ) -> tuple[dict[str, Any], str]:
        public_bytes = crypto.public_bytes()
        fingerprint = crypto.fingerprint()

        path = ADOPTION_PREVIEW_PATH if mode == "adoption" else REACTIVATION_PREVIEW_PATH
        body: dict[str, Any] = {
            "token": token,
            "installation_uuid": self.state.installation_uuid,
            "app_version": self.app_version,
            "hpke_public_key": base64.b64encode(public_bytes).decode("ascii"),
            "hpke_ciphersuite": HPKE_SUITE_NAME,
        }
        if self.app_build is not None:
            body["app_build"] = self.app_build

        response = self._expect(
            self.api.call("POST", path, json_body=body), 201, label=f"{mode} preview"
        )
        preview = response.json()
        require_uuid(preview.get("adoption_request_id"), label="adoption_request_id")
        require_uuid(preview.get("tablet_id"), label="tablet_id")

        if preview.get("hpke_public_key_fingerprint") != fingerprint:
            fail("Server HPKE public-key fingerprint does not match generated fake-iPad key")

        encrypted = strict_b64(preview.get("encrypted_challenge"), label="encrypted_challenge")
        if len(encrypted) <= 65:
            fail("encrypted_challenge is too short")

        context = self._adoption_context(preview, mode=mode)

        self.out.line(f"\n[{mode.upper()} CHALLENGE]")
        self.out.line(f"  Canonical context bytes: {len(context)}")
        self.out.line(f"  Context SHA-256:         {sha256_hex(context)}")
        self.out.line(f"  HPKE message bytes:      {len(encrypted)}")

        nonce = crypto.hpke_open(encrypted, info=context)
        if len(nonce) != 32:
            fail(f"{mode}: challenge plaintext must be 32 bytes, got {len(nonce)}")

        proof = challenge_proof(nonce, context)

        self.out.line("  HPKE challenge open:     PASS")
        self.out.line("  Challenge nonce length:  32 bytes")
        self.out.line("  HMAC proof generated:    PASS")

        return preview, base64.b64encode(proof).decode("ascii")

    def _complete(
        self,
        *,
        preview: dict[str, Any],
        proof: str,
        mode: str,
        bearer: str | None = None,
    ) -> dict[str, Any]:
        path = ADOPTION_COMPLETE_PATH if mode == "adoption" else REACTIVATION_COMPLETE_PATH
        body = {
            "adoption_request_id": preview["adoption_request_id"],
            "challenge_response": proof,
            "confirmed": True,
        }
        response = self._expect(
            self.api.call("POST", path, json_body=body, bearer=bearer),
            201,
            label=f"{mode} completion",
        )
        completed = response.json()
        require_keys(
            completed,
            ["installation_id", "credential", "authorization_valid_until", "server_time"],
            label=f"{mode} completion",
        )
        return completed

    # --- adoption / reactivation ------------------------------------------

    def adopt(
        self, token: str, *, verify: bool = True, simulate_lost_completion_response: bool = False
    ) -> dict[str, Any]:
        self.out.banner("FIREDASH FAKE IPAD — ADOPTION")
        if self.state.is_adopted:
            fail(f"{self.state.state_dir} is already adopted. Run 'reset' before a new adoption.")

        crypto = self.state.ensure_identity(
            server_url=self.api.server_url, app_version=self.app_version, app_build=self.app_build
        )
        self.out.line("[IDENTITY]")
        self.out.line(f"  Installation UUID: {self.state.installation_uuid}")
        self.out.line(f"  P-256 public key:  {len(crypto.public_bytes())} bytes")
        self.out.line(f"  SHA-256 fingerprint:{crypto.fingerprint()}")

        preview, proof = self._preview_and_proof(token=token, mode="adoption", crypto=crypto)
        completed = self._complete(preview=preview, proof=proof, mode="adoption")
        if simulate_lost_completion_response:
            first_credential = completed.get("credential")
            self.out.line(
                "  Simulating lost completion response: first credential deliberately discarded."
            )
            completed = self._complete(preview=preview, proof=proof, mode="adoption")
            if not isinstance(first_credential, str) or hmac.compare_digest(
                first_credential, completed.get("credential", "")
            ):
                fail("Adoption completion recovery did not rotate a fresh credential")
            self.out.line("  Exact completion recovery replay: PASS (fresh credential received)")

        installation_id = require_uuid(completed["installation_id"], label="installation_id")
        credential = completed["credential"]
        if not isinstance(credential, str) or not credential:
            fail("Adoption completion did not return a credential")

        self.state.store_installation(
            installation_id=installation_id,
            tablet_id=preview["tablet_id"],
            credential=credential,
            authorization_valid_until=completed["authorization_valid_until"],
            server_time=self._require_server_time(completed, label="adoption completion"),
        )

        self.out.line("\n[ADOPTION COMPLETE]")
        self.out.line(f"  Installation ID:     {installation_id}")
        self.out.line(f"  Tablet ID:           {preview['tablet_id']}")
        self.out.line(f"  Credential received: yes ({len(credential)} chars, not printed)")
        self.out.line(f"  Lease valid until:   {completed['authorization_valid_until']}")

        self.out.line("  Lost-response recovery: available for the exact proof for 10 minutes")

        if verify:
            self.full_active_verification(use_previous_etag=False, compare_previous=False)

        self.out.banner("ADOPTION RESULT: PASS")
        self.out.line(f"Fake-iPad state saved under: {self.state.state_dir}")
        self.out.line("Do not commit this directory; it contains a test credential/private key.")

        return {
            "installation_id": installation_id,
            "tablet_id": preview["tablet_id"],
            "authorization_valid_until": completed["authorization_valid_until"],
        }

    def reactivate(
        self, token: str, *, verify: bool = True, simulate_lost_completion_response: bool = False
    ) -> dict[str, Any]:
        self.out.banner("FIREDASH FAKE IPAD — REACTIVATION")

        before = self.get_status()
        if before["status"] != "stale":
            fail(
                f"Reactivation requires server installation state 'stale', got {before['status']!r}"
            )

        crypto = self.state.load_private_key()
        old_credential = self.state.credential
        preview, proof = self._preview_and_proof(token=token, mode="reactivation", crypto=crypto)
        completed = self._complete(
            preview=preview, proof=proof, mode="reactivation", bearer=old_credential
        )
        if simulate_lost_completion_response:
            first_credential = completed.get("credential")
            self.out.line(
                "  Simulating lost completion response: rotated credential deliberately discarded."
            )
            completed = self._complete(preview=preview, proof=proof, mode="reactivation")
            if not isinstance(first_credential, str) or hmac.compare_digest(
                first_credential, completed.get("credential", "")
            ):
                fail("Reactivation completion recovery did not rotate a fresh credential")
            self.out.line("  Exact completion recovery replay: PASS (fresh credential received)")

        if completed["installation_id"] != self.state.installation_id:
            fail("Reactivation returned a different installation_id")

        new_credential = completed["credential"]
        if not isinstance(new_credential, str) or not new_credential:
            fail("Reactivation did not return replacement credential")
        if hmac.compare_digest(new_credential, old_credential):
            fail("Reactivation credential was not rotated")

        old_status_response = self.api.call("GET", STATUS_PATH, bearer=old_credential)
        if old_status_response.status < 400:
            fail("Old credential still authenticated after successful reactivation")
        self.out.line(
            f"\n[OLD CREDENTIAL]\n  rejected after rotation: PASS "
            f"(HTTP {old_status_response.status})"
        )

        self.state.rotate_credential(
            credential=new_credential,
            authorization_valid_until=completed["authorization_valid_until"],
            server_time=self._require_server_time(completed, label="reactivation completion"),
        )

        after = self.get_status()
        if after["status"] != "active":
            fail("Reactivated installation did not return to active state")

        if verify:
            self.full_active_verification(use_previous_etag=False, compare_previous=False)

        self.out.banner("REACTIVATION RESULT: PASS")
        return {
            "installation_id": self.state.installation_id,
            "authorization_valid_until": completed["authorization_valid_until"],
        }

    # --- authenticated operations -----------------------------------------

    def get_status(self) -> dict[str, Any]:
        response = self._expect(
            self.api.call("GET", STATUS_PATH, bearer=self.state.credential),
            200,
            label="status",
        )
        data = response.json()
        require_keys(
            data,
            ["status", "authorization_valid_until", "purge_provisioned_data", "server_time"],
            label="status",
        )
        self.out.line("\n[STATUS]")
        self.out.line(f"  state:                  {data['status']}")
        self.out.line(f"  authorization valid to: {data['authorization_valid_until']}")
        self.out.line(f"  server time:            {data['server_time']}")
        self.out.line(f"  purge provisioned data: {data['purge_provisioned_data']}")
        self.state.update_lease(
            data["authorization_valid_until"],
            server_time=self._require_server_time(data, label="status"),
        )
        return data

    def check_in(self, *, telemetry: str = "configured") -> dict[str, Any]:
        response = self._expect(
            self.api.call(
                "POST",
                CHECK_IN_PATH,
                bearer=self.state.credential,
                headers=self.telemetry_headers(telemetry),
            ),
            200,
            label="check-in",
        )
        data = response.json()
        require_keys(data, ["status", "server_time", "authorization_valid_until"], label="check-in")
        if data["status"] != "active":
            fail(f"Check-in returned non-active state: {data['status']!r}")
        self.state.update_lease(data["authorization_valid_until"], server_time=data["server_time"])

        self.out.line("\n[CHECK-IN]")
        self.out.line(f"  state:       {data['status']}")
        self.out.line(f"  server time: {data['server_time']}")
        self.out.line(f"  lease until: {data['authorization_valid_until']}")
        return data

    def refresh(self, *, telemetry: str = "configured") -> dict[str, Any]:
        """One explicit user refresh: renew, configuration, then conditional manifest sync."""
        self.out.banner("FIREDASH FAKE IPAD â€” REFRESH")
        response = self._expect(
            self.api.call(
                "POST",
                REFRESH_PATH,
                bearer=self.state.credential,
                headers=self.telemetry_headers(telemetry),
            ),
            200,
            label="refresh",
        )
        data = response.json()
        require_keys(data, ["status", "server_time", "authorization_valid_until"], label="refresh")
        if data["status"] != "active":
            fail(f"Refresh returned non-active state: {data['status']!r}")
        self.state.update_lease(data["authorization_valid_until"], server_time=data["server_time"])
        self.out.line("\n[REFRESH]")
        self.out.line(f"  server time: {data['server_time']}")
        self.out.line(f"  lease until: {data['authorization_valid_until']}")
        sync = self.full_active_verification(
            use_previous_etag=True, compare_previous=True, check_in_first=False
        )
        self.out.banner("REFRESH RESULT: PASS")
        return {"refresh": data, "sync": sync}

    def get_configuration(self) -> dict[str, Any]:
        response = self._expect(
            self.api.call("GET", CONFIGURATION_PATH, bearer=self.state.credential),
            200,
            label="configuration",
        )
        config = response.json()
        require_keys(
            config,
            ["installation_id", "tablet_id", "department_id", "station_id", "vehicle_id"],
            label="configuration",
        )
        for key in ("installation_id", "tablet_id", "department_id", "station_id", "vehicle_id"):
            require_uuid(config[key], label=f"configuration.{key}")

        if config["installation_id"] != self.state.installation_id:
            fail("Configuration installation_id differs from adopted installation")
        if config["tablet_id"] != self.state.tablet_id:
            fail("Configuration tablet_id differs from adoption preview")

        self.out.line("\n[CONFIGURATION]")
        for key in ("installation_id", "tablet_id", "department_id", "station_id", "vehicle_id"):
            self.out.line(f"  {key}: {config[key]}")
        return config

    def get_signing_key(
        self, version: Any, cache: dict[str, ed25519.Ed25519PublicKey]
    ) -> ed25519.Ed25519PublicKey:
        version_string = str(version)
        if version_string in cache:
            return cache[version_string]

        cached = self.state.cached_signing_key(version_string)
        if cached is not None:
            key = ed25519_from_bytes(cached)
            cache[version_string] = key
            self.out.line(f"\n[SIGNING KEY {version_string}]\n  source:    durable cache")
            return key

        response = self._expect(
            self.api.call(
                "GET", f"/api/v1/tablet/signing-keys/{version_string}", bearer=self.state.credential
            ),
            200,
            label=f"signing-key {version_string}",
        )
        data = response.json()
        require_keys(data, ["algorithm", "version", "public_key"], label="signing key")
        if data["algorithm"] != "Ed25519":
            fail(f"Unexpected signing-key algorithm: {data['algorithm']!r}")
        if str(data["version"]) != version_string:
            fail(
                f"Signing-key response version mismatch: requested {version_string}, "
                f"got {data['version']}"
            )
        key = ed25519_from_bytes(strict_b64(data["public_key"], label="signing-key public_key"))
        cache[version_string] = key
        self.state.cache_signing_key(
            version_string, strict_b64(data["public_key"], label="signing-key public_key")
        )

        self.out.line(f"\n[SIGNING KEY {version_string}]")
        self.out.line("  algorithm: Ed25519")
        self.out.line("  raw key:   32 bytes")
        return key

    def inspect_signing_key(self, version: Any) -> dict[str, Any]:
        version_string = str(version)
        already_cached = self.state.cached_signing_key(version_string) is not None
        key = self.get_signing_key(version_string, {})
        raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {
            "requested_version": version_string,
            "algorithm": "Ed25519",
            "public_key_sha256": sha256_hex(raw),
            "source": "cached" if already_cached else "fetched",
        }

    def terminal_endpoint_matrix(self, *, signing_key_version: str = "1") -> dict[str, Any]:
        """Probe operations a REPLACED/REVOKED credential must not regain.

        The command deliberately makes only ordinary tablet requests. It never
        creates a replacement, revocation, invitation, or reactivation request.
        A reactivation completion cannot be meaningfully probed without a
        human-issued invitation and exact HPKE proof, so that operation is
        intentionally reported as invitation-gated rather than fabricated.
        """
        status = self.get_status()
        if status["status"] not in {"replaced", "revoked"}:
            fail(
                "Terminal endpoint matrix requires server status replaced or revoked, "
                f"got {status['status']!r}"
            )
        if status["purge_provisioned_data"] is not True:
            fail("Terminal status did not require purge_provisioned_data=true")

        probes: list[tuple[str, str, str]] = [
            ("check-in", "POST", CHECK_IN_PATH),
            ("refresh", "POST", REFRESH_PATH),
            ("configuration", "GET", CONFIGURATION_PATH),
            ("signing-key", "GET", f"/api/v1/tablet/signing-keys/{signing_key_version}"),
            ("manifest", "GET", MANIFEST_PATH),
        ]
        summaries = {"status": status["status"], "purge_provisioned_data": True, "denied": {}}
        for name, method, path in probes:
            response = self.api.call(method, path, bearer=self.state.credential)
            if response.status < 400:
                fail(f"Terminal credential unexpectedly accessed {name}: HTTP {response.status}")
            summaries["denied"][name] = {
                "status": response.status,
                "code": self._problem_code(response),
            }
            self.out.line(f"  {name}: denied as required (HTTP {response.status})")

        previous = self.state.state.get("last_manifest_summary")
        if isinstance(previous, dict) and previous:
            publication_id = next(iter(previous.values())).get("publication_id")
            if isinstance(publication_id, str):
                response = self.api.call(
                    "GET",
                    f"/api/v1/tablet/datasets/{publication_id}/download",
                    bearer=self.state.credential,
                    headers={"Accept": "application/octet-stream"},
                )
                if response.status < 400:
                    fail(
                        "Terminal credential unexpectedly downloaded dataset: "
                        f"HTTP {response.status}"
                    )
                summaries["denied"]["dataset"] = {
                    "status": response.status,
                    "code": self._problem_code(response),
                }
                self.out.line(f"  dataset: denied as required (HTTP {response.status})")
        else:
            summaries["dataset"] = "not probed: no previously verified publication in local state"
        summaries["reactivation"] = "not probed: requires human-issued invitation and exact proof"
        return summaries

    @staticmethod
    def _problem_code(response: HttpResponse) -> str | None:
        try:
            value = response.json().get("code")
        except Exception:
            return None
        return value if isinstance(value, str) else None

    # --- manifest ----------------------------------------------------------

    def fetch_manifest(
        self, *, if_none_match: str | None = None, max_attempts: int = 12
    ) -> HttpResponse:
        headers: dict[str, str] = {}
        if if_none_match:
            headers["If-None-Match"] = if_none_match

        for attempt in range(1, max_attempts + 1):
            response = self.api.call(
                "GET", MANIFEST_PATH, bearer=self.state.credential, headers=headers
            )
            if response.status != 202:
                return response

            problem = response.json()
            if problem.get("code") != "manifest_pending":
                fail("Manifest HTTP 202 did not contain code=manifest_pending")
            retry_after_raw = response.headers.get("retry-after", "5")
            try:
                retry_after = max(1, int(retry_after_raw))
            except ValueError:
                retry_after = 5

            self.out.line("\n[MANIFEST PENDING]")
            self.out.line(f"  attempt:     {attempt}/{max_attempts}")
            self.out.line(f"  request ID:  {problem.get('request_id', '')}")
            self.out.line(f"  manifest ID: {problem.get('manifest_request_id', '')}")
            self.out.line(f"  Retry-After: {retry_after}s")

            if attempt == max_attempts:
                fail("Manifest remained pending after maximum attempts")
            # `sleep` is a lower bound. Backgrounded clients may resume later,
            # but must never intentionally retry earlier than Retry-After.
            time.sleep(retry_after)

        raise AssertionError("unreachable")

    @staticmethod
    def canonical_manifest_payload(manifest: dict[str, Any]) -> bytes:
        payload = copy.deepcopy(manifest)
        try:
            del payload["signature"]
        except KeyError:
            fail("manifest: missing signature")
        return canonical_json_bytes(payload)

    def _verify_manifest_signature_with_key(
        self, manifest: dict[str, Any], key: ed25519.Ed25519PublicKey
    ) -> bytes:
        require_keys(
            manifest,
            ["signature", "signature_algorithm", "signing_key_version"],
            label="manifest",
        )
        if manifest["signature_algorithm"] != "Ed25519":
            fail(f"Unexpected manifest signature algorithm: {manifest['signature_algorithm']!r}")

        signature = strict_b64(manifest["signature"], label="manifest.signature")
        if len(signature) != 64:
            fail(f"Manifest Ed25519 signature must be 64 bytes, got {len(signature)}")

        canonical = self.canonical_manifest_payload(manifest)

        try:
            key.verify(signature, canonical)
        except InvalidSignature:
            fail("Manifest Ed25519 signature verification FAILED")

        self.out.line("\n[MANIFEST SIGNATURE]")
        self.out.line(f"  canonical payload bytes: {len(canonical)}")
        self.out.line(f"  payload SHA-256:         {sha256_hex(canonical)}")
        self.out.line("  Ed25519 verification:    PASS")
        return canonical

    def verify_manifest_signature(
        self, manifest: dict[str, Any], key_cache: dict[str, ed25519.Ed25519PublicKey]
    ) -> None:
        key = self.get_signing_key(manifest["signing_key_version"], key_cache)
        self._verify_manifest_signature_with_key(manifest, key)

    def verify_manifest_etag(self, response: HttpResponse, manifest: dict[str, Any]) -> str:
        etag = response.headers.get("etag")
        if not etag:
            fail("Manifest response missing ETag")

        etag_payload = copy.deepcopy(manifest)
        etag_payload.pop("generated_at", None)
        expected = f'"{sha256_hex(canonical_json_bytes(etag_payload))}"'
        if etag != expected:
            fail(
                "Manifest ETag mismatch against documented canonical payload "
                f"(expected {expected}, got {etag})"
            )
        self.out.line(f"  manifest ETag: {etag} (PASS)")
        return etag

    @staticmethod
    def manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        datasets = manifest.get("datasets", [])
        if not isinstance(datasets, list):
            fail("manifest.datasets is not an array")
        for dataset in datasets:
            if not isinstance(dataset, dict):
                fail("manifest.datasets contains non-object item")
            dataset_type = dataset.get("type")
            if not isinstance(dataset_type, str):
                fail("Manifest dataset missing string type")
            result[dataset_type] = {
                "publication_id": dataset.get("publication_id"),
                "version": dataset.get("version"),
                "schema_version": dataset.get("schema_version"),
                "ciphertext_sha256": dataset.get("ciphertext_sha256"),
                "encrypted_size": dataset.get("encrypted_size"),
            }
        return result

    def validate_manifest_structure(self, manifest: dict[str, Any], config: dict[str, Any]) -> None:
        require_keys(
            manifest,
            [
                "manifest_generation",
                "generated_at",
                "authorization_valid_until",
                "configuration",
                "datasets",
                "signature",
                "signature_algorithm",
                "signing_key_version",
            ],
            label="manifest",
        )

        if manifest["configuration"] != config:
            fail("Manifest configuration differs from GET /tablet/configuration response")

        if not isinstance(manifest["datasets"], list):
            fail("manifest.datasets must be an array")

        seen_types: set[str] = set()
        seen_publications: set[str] = set()

        for index, dataset in enumerate(manifest["datasets"]):
            if not isinstance(dataset, dict):
                fail(f"manifest.datasets[{index}] must be an object")
            require_keys(
                dataset,
                [
                    "publication_id",
                    "type",
                    "scope",
                    "version",
                    "schema_version",
                    "required",
                    "minimum_app_version",
                    "artifact_format",
                    "encrypted_size",
                    "ciphertext_sha256",
                    "content_encryption_algorithm",
                    "content_encryption_nonce",
                    "content_key_wrapped_for_kek",
                    "content_key_wrapping_algorithm",
                    "content_key_kek_version",
                    "artifact_signature",
                    "artifact_signature_algorithm",
                    "artifact_signing_key_version",
                    "download_url",
                    "key_grant",
                ],
                label=f"manifest.datasets[{index}]",
            )
            publication_id = require_uuid(
                dataset["publication_id"], label=f"manifest.datasets[{index}].publication_id"
            )
            dataset_type = dataset["type"]
            if not isinstance(dataset_type, str):
                fail(f"manifest.datasets[{index}].type must be string")
            if dataset_type in seen_types:
                fail(f"Duplicate dataset type in manifest: {dataset_type}")
            seen_types.add(dataset_type)
            if publication_id in seen_publications:
                fail(f"Duplicate publication ID in manifest: {publication_id}")
            seen_publications.add(publication_id)

            if dataset["scope"] not in {"department", "station"}:
                fail(f"{dataset_type}: invalid scope {dataset['scope']!r}")
            if not isinstance(dataset["version"], int) or dataset["version"] <= 0:
                fail(f"{dataset_type}: version must be positive integer")
            if not isinstance(dataset["schema_version"], int) or dataset["schema_version"] <= 0:
                fail(f"{dataset_type}: schema_version must be positive integer")
            if not isinstance(dataset["required"], bool):
                fail(f"{dataset_type}: required must be boolean")
            if dataset["minimum_app_version"] is not None and not isinstance(
                dataset["minimum_app_version"], str
            ):
                fail(f"{dataset_type}: minimum_app_version must be string or null")
            if not isinstance(dataset["artifact_format"], str) or not dataset["artifact_format"]:
                fail(f"{dataset_type}: artifact_format must be a non-empty string")
            if not isinstance(dataset["encrypted_size"], int) or dataset["encrypted_size"] <= 0:
                fail(f"{dataset_type}: encrypted_size must be positive integer")
            if not re.fullmatch(r"[0-9a-f]{64}", dataset["ciphertext_sha256"]):
                fail(f"{dataset_type}: ciphertext_sha256 malformed")
            if dataset["content_encryption_algorithm"] != "AES-256-GCM":
                fail(
                    f"{dataset_type}: unexpected content encryption algorithm "
                    f"{dataset['content_encryption_algorithm']!r}"
                )
            if dataset["content_key_wrapping_algorithm"] != "AES-KW-RFC3394":
                fail(
                    f"{dataset_type}: unexpected KEK wrapping algorithm "
                    f"{dataset['content_key_wrapping_algorithm']!r}"
                )
            nonce = strict_b64(
                dataset["content_encryption_nonce"],
                label=f"{dataset_type}.content_encryption_nonce",
            )
            if len(nonce) != 12:
                fail(f"{dataset_type}: AES-GCM nonce must be 12 bytes")

            expected_url = f"/api/v1/tablet/datasets/{publication_id}/download"
            if dataset["download_url"] != expected_url:
                fail(
                    f"{dataset_type}: download_url mismatch. "
                    f"Expected {expected_url!r}, got {dataset['download_url']!r}"
                )

            grant = dataset["key_grant"]
            if not isinstance(grant, dict):
                fail(f"{dataset_type}: key_grant must be object")
            require_keys(
                grant,
                ["scheme", "ciphersuite", "encapsulated_key", "wrapped_content_key"],
                label=f"{dataset_type}.key_grant",
            )
            if grant["scheme"] != "HPKE":
                fail(f"{dataset_type}: key_grant.scheme is not HPKE")
            if grant["ciphersuite"] != HPKE_SUITE_NAME:
                fail(f"{dataset_type}: key_grant ciphersuite mismatch")
            if (
                len(
                    strict_b64(
                        grant["encapsulated_key"],
                        label=f"{dataset_type}.key_grant.encapsulated_key",
                    )
                )
                != 65
            ):
                fail(f"{dataset_type}: encapsulated_key must be 65 bytes")
            strict_b64(
                grant["wrapped_content_key"], label=f"{dataset_type}.key_grant.wrapped_content_key"
            )

        self.out.line("\n[MANIFEST STRUCTURE]")
        self.out.line(f"  generation: {manifest['manifest_generation']}")
        self.out.line(f"  datasets:   {len(manifest['datasets'])}")
        for dataset in manifest["datasets"]:
            self.out.line(
                f"  - {dataset['type']}: "
                f"v{dataset['version']} / schema {dataset['schema_version']} / "
                f"{dataset['publication_id']} / {dataset['encrypted_size']} encrypted bytes"
            )

    def _unsupported_dataset_reason(self, dataset: dict[str, Any]) -> str | None:
        """Return why this fake client cannot activate a dataset entry, if any."""
        dataset_type = dataset["type"]
        supported = datasets.SUPPORTED_DATASETS.get(dataset_type)
        if supported is None:
            return "dataset type is not implemented by this client"
        artifact_format, schema_versions = supported
        if dataset["artifact_format"] != artifact_format:
            return "artifact format is not implemented by this client"
        if dataset["schema_version"] not in schema_versions:
            return "schema version is not implemented by this client"
        minimum = dataset["minimum_app_version"]
        if minimum is not None:
            try:
                required = tuple(
                    int(part)
                    for part in require_app_version(
                        minimum, label=f"{dataset_type}.minimum_app_version"
                    ).split(".")
                )
            except Exception:
                return "minimum_app_version is invalid"
            current = tuple(int(part) for part in self.app_version.split("."))
            if current < required:
                return f"requires app version {minimum}"
        return None

    def select_compatible_datasets(self, manifest: dict[str, Any]) -> set[str]:
        """Enforce required/optional manifest compatibility before downloading data."""
        supported: set[str] = set()
        for dataset in manifest["datasets"]:
            reason = self._unsupported_dataset_reason(dataset)
            if reason is None:
                supported.add(dataset["type"])
                continue
            if dataset["required"]:
                fail(f"{dataset['type']}: required dataset is unsupported: {reason}")
            self.out.line(f"  optional dataset ignored: {dataset['type']} ({reason})")
        return supported

    @staticmethod
    def scope_for_dataset(dataset: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        scope_name = dataset["scope"]
        if scope_name == "department":
            station_id = None
        elif scope_name == "station":
            station_id = config["station_id"]
        else:
            fail(f"Unsupported dataset scope: {scope_name!r}")

        return {
            "dataset_type_code": dataset["type"],
            "department_id": config["department_id"],
            "station_id": station_id,
        }

    def verify_artifact_signature(
        self,
        dataset: dict[str, Any],
        config: dict[str, Any],
        key_cache: dict[str, ed25519.Ed25519PublicKey],
    ) -> None:
        if dataset["artifact_signature_algorithm"] != "Ed25519":
            fail(
                f"{dataset['type']}: unexpected artifact signature algorithm "
                f"{dataset['artifact_signature_algorithm']!r}"
            )
        signature = strict_b64(
            dataset["artifact_signature"], label=f"{dataset['type']}.artifact_signature"
        )
        if len(signature) != 64:
            fail(f"{dataset['type']}: artifact signature must be 64 bytes")

        payload = {
            "ciphertext_sha256": dataset["ciphertext_sha256"],
            "ciphertext_size": dataset["encrypted_size"],
            "encryption_algorithm": dataset["content_encryption_algorithm"],
            "kek_version": dataset["content_key_kek_version"],
            "nonce": dataset["content_encryption_nonce"],
            "schema_version": dataset["schema_version"],
            "scope": self.scope_for_dataset(dataset, config),
            "version_number": dataset["version"],
            "wrapped_cek": dataset["content_key_wrapped_for_kek"],
            "wrapping_algorithm": dataset["content_key_wrapping_algorithm"],
        }
        canonical = canonical_json_bytes(payload)
        key = self.get_signing_key(dataset["artifact_signing_key_version"], key_cache)
        try:
            key.verify(signature, canonical)
        except InvalidSignature:
            fail(f"{dataset['type']}: artifact signature verification FAILED")

        self.out.line("  artifact Ed25519 signature: PASS")

    def unwrap_cek(
        self, dataset: dict[str, Any], config: dict[str, Any], crypto: CryptoState
    ) -> bytes:
        grant = dataset["key_grant"]
        enc = strict_b64(
            grant["encapsulated_key"], label=f"{dataset['type']}.key_grant.encapsulated_key"
        )
        ct = strict_b64(
            grant["wrapped_content_key"], label=f"{dataset['type']}.key_grant.wrapped_content_key"
        )

        info = {
            "ciphertext_sha256": dataset["ciphertext_sha256"],
            "installation_id": config["installation_id"],
            "protocol": GRANT_PROTOCOL,
            "publication_id": dataset["publication_id"],
            "schema_version": dataset["schema_version"],
            "scope": self.scope_for_dataset(dataset, config),
            "tablet_id": config["tablet_id"],
            "version_number": dataset["version"],
        }
        cek = crypto.hpke_open(enc + ct, info=canonical_json_bytes(info))
        if len(cek) != 32:
            fail(f"{dataset['type']}: HPKE grant must unwrap a 32-byte CEK, got {len(cek)}")

        self.out.line("  HPKE CEK unwrap:             PASS (32 bytes)")
        return cek

    def download_ciphertext(self, dataset: dict[str, Any]) -> bytes:
        response = self.api.expect(
            self.api.call(
                "GET",
                dataset["download_url"],
                bearer=self.state.credential,
                headers={"Accept": "application/octet-stream"},
            ),
            200,
            label=f"{dataset['type']} download",
        )

        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("application/octet-stream"):
            fail(f"{dataset['type']}: expected application/octet-stream, got {content_type!r}")

        expected_etag = f'"{dataset["ciphertext_sha256"]}"'
        actual_etag = response.headers.get("etag")
        if actual_etag != expected_etag:
            fail(
                f"{dataset['type']}: artifact ETag mismatch "
                f"(expected {expected_etag}, got {actual_etag})"
            )

        accept_ranges = response.headers.get("accept-ranges")
        if accept_ranges != "bytes":
            fail(f"{dataset['type']}: expected Accept-Ranges: bytes, got {accept_ranges!r}")

        ciphertext = response.body
        if len(ciphertext) != dataset["encrypted_size"]:
            fail(
                f"{dataset['type']}: encrypted size mismatch "
                f"(expected {dataset['encrypted_size']}, got {len(ciphertext)})"
            )
        actual_hash = sha256_hex(ciphertext)
        if actual_hash != dataset["ciphertext_sha256"]:
            fail(
                f"{dataset['type']}: ciphertext SHA-256 mismatch "
                f"(expected {dataset['ciphertext_sha256']}, got {actual_hash})"
            )

        self.out.line(f"  protected download:          PASS ({len(ciphertext)} bytes)")
        self.out.line("  ciphertext SHA-256:          PASS")
        self.out.line("  ciphertext ETag:             PASS")
        self.out.line("  Accept-Ranges header:        PASS")

        conditional = self.api.call(
            "GET",
            dataset["download_url"],
            bearer=self.state.credential,
            headers={"Accept": "application/octet-stream", "If-None-Match": expected_etag},
        )
        if conditional.status != 304:
            fail(
                f"{dataset['type']}: conditional download expected HTTP 304, "
                f"got {conditional.status}"
            )
        if conditional.body:
            fail(f"{dataset['type']}: HTTP 304 unexpectedly had a response body")
        self.out.line("  artifact If-None-Match 304:  PASS")

        return ciphertext

    def verify_dataset(
        self,
        dataset: dict[str, Any],
        config: dict[str, Any],
        crypto: CryptoState,
        key_cache: dict[str, ed25519.Ed25519PublicKey],
    ) -> dict[str, Any]:
        self.out.line("\n" + "-" * 72)
        self.out.line(
            f"DATASET {dataset['type']} — v{dataset['version']} — {dataset['publication_id']}"
        )
        self.out.line("-" * 72)

        self.verify_artifact_signature(dataset, config, key_cache)
        cek = self.unwrap_cek(dataset, config, crypto)
        ciphertext = self.download_ciphertext(dataset)
        plaintext = aes_gcm_decrypt(
            cek,
            strict_b64(
                dataset["content_encryption_nonce"],
                label=f"{dataset['type']}.content_encryption_nonce",
            ),
            ciphertext,
        )
        self.out.line("  AES-256-GCM authentication:  PASS")
        self.out.line(f"  plaintext bytes:             {len(plaintext)}")

        summary = datasets.validate_plaintext(
            plaintext,
            dataset,
            config,
            self.out,
            save_plaintext=self.save_plaintext,
            state_dir=self.state.state_dir,
        )
        self.out.line("\n  DATASET RESULT: PASS")
        return summary

    def verify_manifest_and_datasets(
        self,
        response: HttpResponse,
        config: dict[str, Any],
        *,
        dataset_types: list[str] | None = None,
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        if response.status != 200:
            fail(f"Manifest expected HTTP 200, got {problem_text(response)}")

        manifest = response.json()
        key_cache: dict[str, ed25519.Ed25519PublicKey] = {}

        self.verify_manifest_signature(manifest, key_cache)
        self.validate_manifest_structure(manifest, config)
        supported_dataset_types = self.select_compatible_datasets(manifest)
        etag = self.verify_manifest_etag(response, manifest)

        crypto = self.state.load_private_key()
        dataset_results: dict[str, Any] = {}

        for dataset in manifest["datasets"]:
            if dataset["type"] not in supported_dataset_types:
                continue
            if dataset_types is not None and dataset["type"] not in dataset_types:
                continue
            dataset_results[dataset["type"]] = self.verify_dataset(
                dataset, config, crypto, key_cache
            )

        conditional = self.fetch_manifest(if_none_match=etag, max_attempts=1)
        if conditional.status != 304:
            fail(f"Manifest If-None-Match expected HTTP 304, got {conditional.status}")
        if conditional.body:
            fail("Manifest HTTP 304 unexpectedly had a response body")
        self.out.line("\n[MANIFEST CONDITIONAL GET]")
        self.out.line("  If-None-Match → 304: PASS")

        secure_write_json(self.state.manifest_path, manifest)

        return manifest, etag, dataset_results

    def full_active_verification(
        self,
        *,
        use_previous_etag: bool,
        compare_previous: bool,
        check_in_first: bool = True,
        expect_changed: list[str] | None = None,
        expect_unchanged: list[str] | None = None,
        expect_version_increase: list[str] | None = None,
        dataset_types: list[str] | None = None,
    ) -> dict[str, Any]:
        expect_changed = expect_changed or []
        expect_unchanged = expect_unchanged or []
        expect_version_increase = expect_version_increase or []

        status = self.get_status()
        if status["status"] != "active":
            fail(f"Full verification requires ACTIVE installation, got {status['status']!r}")
        if status["purge_provisioned_data"]:
            fail("ACTIVE installation unexpectedly has purge_provisioned_data=true")

        if check_in_first:
            self.check_in()
        config = self.get_configuration()

        previous_summary = self.state.state.get("last_manifest_summary", {})
        previous_etag = self.state.state.get("last_manifest_etag") if use_previous_etag else None

        response = self.fetch_manifest(if_none_match=previous_etag)

        if response.status == 304:
            self.out.line("\n[SERVER PUBLICATION CHANGE REPORT]")
            self.out.line(
                "  Manifest ETag unchanged: server reports no authorized publication change."
            )
            if expect_changed or expect_version_increase:
                fail(
                    "Expected a dataset update, but the backend returned HTTP 304 "
                    "for the previous manifest ETag"
                )
            self.out.banner("CURRENT BACKEND STATE: UNCHANGED / PASS")
            return {"changed": False}

        manifest, etag, dataset_results = self.verify_manifest_and_datasets(
            response, config, dataset_types=dataset_types
        )
        current_summary = self.manifest_summary(manifest)

        if compare_previous and previous_summary:
            changes = self.compare_manifest_summaries(previous_summary, current_summary)
            self.assert_expected_changes(
                changes,
                expect_changed=expect_changed,
                expect_unchanged=expect_unchanged,
                previous=previous_summary,
                current=current_summary,
                expect_version_increase=expect_version_increase,
            )
        elif expect_changed or expect_unchanged or expect_version_increase:
            fail("No previous verified manifest summary exists; cannot assert update expectations")

        self.state.state["last_manifest_etag"] = etag
        self.state.state["last_manifest_summary"] = current_summary
        self.state.state["last_dataset_validation"] = dataset_results
        self.state.save()

        return {"changed": True, "manifest_generation": manifest["manifest_generation"]}

    def compare_manifest_summaries(
        self, previous: dict[str, Any], current: dict[str, Any]
    ) -> dict[str, str]:
        self.out.line("\n[SERVER PUBLICATION CHANGE REPORT]")
        all_types = sorted(set(previous) | set(current))
        changes: dict[str, str] = {}

        if not all_types:
            self.out.line("  no datasets in either manifest")
            return changes

        for dataset_type in all_types:
            old = previous.get(dataset_type)
            new = current.get(dataset_type)
            if old is None:
                if new is None:
                    fail(f"internal: dataset {dataset_type} missing from both manifests")
                changes[dataset_type] = "ADDED"
                self.out.line(
                    f"  {dataset_type}: ADDED → v{new['version']} {new['publication_id']}"
                )
                continue
            if new is None:
                changes[dataset_type] = "REMOVED"
                self.out.line(
                    f"  {dataset_type}: REMOVED ← v{old['version']} {old['publication_id']}"
                )
                continue
            changed = (
                old["publication_id"] != new["publication_id"]
                or old["version"] != new["version"]
                or old["ciphertext_sha256"] != new["ciphertext_sha256"]
            )
            if changed:
                changes[dataset_type] = "CHANGED"
                self.out.line(
                    f"  {dataset_type}: CHANGED "
                    f"v{old['version']} {old['publication_id']} "
                    f"→ v{new['version']} {new['publication_id']}"
                )
            else:
                changes[dataset_type] = "UNCHANGED"
                self.out.line(
                    f"  {dataset_type}: UNCHANGED v{new['version']} {new['publication_id']}"
                )
        return changes

    def assert_expected_changes(
        self,
        changes: dict[str, str],
        *,
        expect_changed: list[str],
        expect_unchanged: list[str],
        previous: dict[str, Any],
        current: dict[str, Any],
        expect_version_increase: list[str],
    ) -> None:
        for dataset_type in expect_changed:
            actual = changes.get(dataset_type)
            if actual not in {"CHANGED", "ADDED", "REMOVED"}:
                fail(
                    f"Expected {dataset_type} to change, but change report says "
                    f"{actual or 'MISSING'}"
                )
            self.out.line(f"  expectation: {dataset_type} changed: PASS")

        for dataset_type in expect_unchanged:
            actual = changes.get(dataset_type)
            if actual != "UNCHANGED":
                fail(
                    f"Expected {dataset_type} to remain unchanged, but change report "
                    f"says {actual or 'MISSING'}"
                )
            self.out.line(f"  expectation: {dataset_type} unchanged: PASS")

        for dataset_type in expect_version_increase:
            old = previous.get(dataset_type)
            new = current.get(dataset_type)
            if not old or not new:
                fail(
                    f"Cannot assert version increase for {dataset_type}: "
                    "dataset missing from old/new manifest"
                )
            old_version = old.get("version")
            new_version = new.get("version")
            if not isinstance(old_version, int) or not isinstance(new_version, int):
                fail(f"{dataset_type}: version is not integer")
            if new_version <= old_version:
                fail(
                    f"{dataset_type}: expected version increase, got {old_version} → {new_version}"
                )
            self.out.line(
                f"  expectation: {dataset_type} version increased "
                f"{old_version} → {new_version}: PASS"
            )

    # --- high-level commands ----------------------------------------------

    def verify(self) -> dict[str, Any]:
        self.out.banner("FIREDASH FAKE IPAD — CURRENT BACKEND VERIFICATION")
        result = self.full_active_verification(use_previous_etag=False, compare_previous=False)
        self.out.banner("VERIFY RESULT: PASS")
        return result

    def update_check(
        self,
        *,
        expect_changed: list[str],
        expect_unchanged: list[str],
        expect_version_increase: list[str],
    ) -> dict[str, Any]:
        self.out.banner("FIREDASH FAKE IPAD — UPDATE CHECK")
        if not self.state.state.get("last_manifest_summary"):
            fail("No verified manifest is stored. Run 'adopt' or 'verify' first.")
        result = self.full_active_verification(
            use_previous_etag=True,
            compare_previous=True,
            expect_changed=expect_changed,
            expect_unchanged=expect_unchanged,
            expect_version_increase=expect_version_increase,
        )
        self.out.banner("UPDATE CHECK RESULT: PASS")
        return result

    def manifest(
        self, *, if_none_match: str | None = None, download: bool = False
    ) -> dict[str, Any]:
        self.out.banner("FIREDASH FAKE IPAD — MANIFEST")
        config = self.get_configuration()
        response = self.fetch_manifest(if_none_match=if_none_match)
        if response.status == 304:
            self.out.line("\n[MANIFEST] ETag unchanged: server reports no authorized change.")
            return {"changed": False}
        if download:
            manifest, etag, _ = self.verify_manifest_and_datasets(response, config)
        else:
            if response.status != 200:
                fail(f"Manifest expected HTTP 200, got {problem_text(response)}")
            manifest = response.json()
            key_cache: dict[str, ed25519.Ed25519PublicKey] = {}
            self.verify_manifest_signature(manifest, key_cache)
            self.validate_manifest_structure(manifest, config)
            etag = self.verify_manifest_etag(response, manifest)
        self.out.banner("MANIFEST RESULT: PASS")
        return {"changed": True, "etag": etag, "generation": manifest["manifest_generation"]}

    def download(self, *, dataset_types: list[str] | None = None) -> dict[str, Any]:
        self.out.banner("FIREDASH FAKE IPAD — DATASET DOWNLOAD")
        config = self.get_configuration()
        response = self.fetch_manifest(if_none_match=self.state.state.get("last_manifest_etag"))
        if response.status == 304:
            self.out.line(
                "\n[DATASET DOWNLOAD] Manifest ETag unchanged; using previously verified cache."
            )
            return {"changed": False}
        _, etag, results = self.verify_manifest_and_datasets(
            response, config, dataset_types=dataset_types
        )
        self.state.state["last_manifest_etag"] = etag
        self.state.save()
        self.out.banner("DATASET DOWNLOAD RESULT: PASS")
        return {"changed": True, "datasets": list(results)}
