"""Synthetic full tablet delivery: real Ed25519, HPKE, AES-KW and AES-GCM."""

import base64
import copy
import io
import json
import os
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.keywrap import aes_key_wrap

from tools.fake_ipad.client import FakeIPadClient
from tools.fake_ipad.crypto import HPKE_SUITE, HPKE_SUITE_NAME, canonical_json_bytes, sha256_hex
from tools.fake_ipad.errors import ClientError
from tools.fake_ipad.generations import RESPONSE_FIELDS, endpoint
from tools.fake_ipad.output import Output
from tools.fake_ipad.state import DeviceState
from tools.fake_ipad.transport import HttpResponse
from tools.test_fake_ipad import _dangerous_goods_document


def uid():
    return str(uuid.uuid4())


def b64(value):
    return base64.b64encode(value).decode("ascii")


class Delivery:
    """Authenticated simulated transport, with mutable signed server responses."""

    def __init__(self, tmp_path):
        self.state = DeviceState(tmp_path / "synthetic-installation")
        self.crypto = self.state.ensure_identity(
            server_url="https://tablet.example", app_version="1.0.0"
        )
        self.config = {
            k: uid()
            for k in ("installation_id", "tablet_id", "department_id", "station_id", "vehicle_id")
        }
        self.config["report_delivery_max_attachment_bytes"] = 20971520
        self.state.store_installation(
            installation_id=self.config["installation_id"],
            tablet_id=self.config["tablet_id"],
            credential="synthetic-credential",
            authorization_valid_until="2099-01-01T00:00:00Z",
        )
        self.signer = Ed25519PrivateKey.generate()
        self.manifest = {
            "configuration": self.config,
            "manifest_generation": 1,
            "generated_at": "2026-01-01T00:00:00Z",
            "authorization_valid_until": "2099-01-01T00:00:00Z",
            "datasets": [],
            "signature_algorithm": "Ed25519",
            "signing_key_version": "1",
        }
        self.generations = {}
        self.artifacts = {}
        self.calls = []
        self.pending = {}
        self.failures = {}
        self.stream = io.StringIO()
        self.client = FakeIPadClient(self.state, self, out=Output(stream=self.stream))

    def seal_main(self):
        self.manifest["signature"] = b64(
            self.signer.sign(
                canonical_json_bytes({k: v for k, v in self.manifest.items() if k != "signature"})
            )
        )
        etag_payload = {k: v for k, v in self.manifest.items() if k != "generated_at"}
        self.etag = f'"{sha256_hex(canonical_json_bytes(etag_payload))}"'

    def seal_generation(self, wire):
        wire["signature"] = b64(
            self.signer.sign(
                canonical_json_bytes({k: v for k, v in wire.items() if k not in RESPONSE_FIELDS})
            )
        )

    def descriptor(self, kind, schema, fmt, required=True):
        scope = (
            {"type": "SYSTEM"}
            if kind == "dangerous_goods"
            else {"type": "DEPARTMENT", "department_id": self.config["department_id"]}
        )
        if kind.startswith("station_"):
            scope.update(type="STATION", station_id=self.config["station_id"])
        return {
            "type": kind,
            "publication_id": uid(),
            "scope": scope,
            "version": 1,
            "schema_version": schema,
            "artifact_format": fmt,
            "required": required,
            "minimum_app_version": None,
        }

    def hpke(self, key, descriptor, protocol, **extra):
        info = {
            "protocol": protocol,
            "publication_id": descriptor["publication_id"],
            "installation_id": self.config["installation_id"],
            "tablet_id": self.config["tablet_id"],
            "scope": {
                "dataset_type_code": descriptor["type"],
                "department_id": descriptor["scope"].get("department_id"),
                "station_id": descriptor["scope"].get("station_id"),
            },
            "version_number": 1,
            "schema_version": descriptor["schema_version"],
            **extra,
        }
        encrypted = HPKE_SUITE.encrypt(
            key, self.crypto.private_key.public_key(), info=canonical_json_bytes(info)
        )
        return info, encrypted

    def add_v1(self, kind, plaintext, fmt="json"):
        descriptor = self.descriptor(kind, 1, fmt)
        cek, nonce = os.urandom(32), os.urandom(12)
        ciphertext = AESGCM(cek).encrypt(nonce, plaintext, None)
        digest = sha256_hex(ciphertext)
        descriptor.update(
            encrypted_size=len(ciphertext),
            ciphertext_sha256=digest,
            content_encryption_algorithm="AES-256-GCM",
            content_encryption_nonce=b64(nonce),
            content_key_wrapped_for_kek=b64(os.urandom(40)),
            content_key_wrapping_algorithm="AES-KW-RFC3394",
            content_key_kek_version="1",
            artifact_signature_algorithm="Ed25519",
            artifact_signing_key_version="1",
            download_url=f"/api/v1/tablet/datasets/{descriptor['publication_id']}/download",
        )
        _, encrypted = self.hpke(cek, descriptor, "firedash-hpke-v1", ciphertext_sha256=digest)
        descriptor["key_grant"] = {
            "scheme": "HPKE",
            "ciphersuite": HPKE_SUITE_NAME,
            "encapsulated_key": b64(encrypted[:65]),
            "wrapped_content_key": b64(encrypted[65:]),
        }
        artifact_payload = {
            "ciphertext_sha256": digest,
            "ciphertext_size": len(ciphertext),
            "encryption_algorithm": "AES-256-GCM",
            "kek_version": "1",
            "nonce": b64(nonce),
            "schema_version": 1,
            "scope": {"dataset_type_code": kind, **descriptor["scope"]},
            "version_number": 1,
            "wrapped_cek": descriptor["content_key_wrapped_for_kek"],
            "wrapping_algorithm": "AES-KW-RFC3394",
        }
        descriptor["artifact_signature"] = b64(
            self.signer.sign(canonical_json_bytes(artifact_payload))
        )
        self.artifacts[descriptor["download_url"]] = (ciphertext, digest, True)
        self.manifest["datasets"].append(descriptor)
        return descriptor

    def add_v2(self, kind, count=1, folders=None):
        descriptor = self.descriptor(kind, 2, "document-manifest-v2")
        descriptor["manifest_url"] = f"{endpoint(descriptor)}/manifest"
        fire = kind == "department_fire_plans"
        library = kind in {"department_files", "station_files"}
        generation_key = os.urandom(32)
        wire = {
            "format": "document-generation-v2",
            "publication_id": descriptor["publication_id"],
            "version": 1,
            "schema_version": 2,
            "documents": [],
        }
        wire["dataset_type"] = kind
        if library:
            wire.update(
                library={"id": uid(), "name": "Bibliothek ü", "scope": descriptor["scope"].copy()},
                folders=folders or [],
                source_revision=1,
            )
        for doc_id in sorted(uid() for _ in range(count)):
            pdf = b"%PDF-1.4\nsynthetic sanitized PDF " + doc_id.encode() + b"\n%%EOF\n"
            cek, nonce = os.urandom(32), os.urandom(12)
            ciphertext = AESGCM(cek).encrypt(nonce, pdf, None)
            artifact_id = uid()
            path = f"{endpoint(descriptor)}/artifacts/{artifact_id}/download"
            document = {
                "artifact_id": artifact_id,
                "sanitized_pdf_sha256": sha256_hex(pdf),
                "ciphertext_sha256": sha256_hex(ciphertext),
                "ciphertext_size": len(ciphertext),
                "nonce": b64(nonce),
                "encryption_algorithm": "AES-256-GCM",
                "wrapping_algorithm": "AES-KW-RFC3394",
                "kek_version": "1",
                "signature": b64(os.urandom(64)),
                "signature_algorithm": "Ed25519",
                "signing_key_version": "1",
                "generation_wrapped_cek": b64(aes_key_wrap(generation_key, cek)),
                "generation_key_wrapping_algorithm": "AES-KW-RFC3394",
                "download_path": path,
            }
            if library:
                document["file"] = {
                    "id": doc_id,
                    "name": "../display-only.pdf",
                    "folder_id": wire["folders"][0]["id"] if wire["folders"] else None,
                    "size_bytes": len(pdf),
                    "sha256": sha256_hex(pdf),
                }
            else:
                meta = {
                    "id": doc_id,
                    "external_identifier": "A-1",
                    "object_name": "Gebäude ü",
                    "address": None,
                    "postal_code": None,
                    "city": None,
                    "longitude": None,
                    "latitude": None,
                    "sha256": sha256_hex(pdf),
                    "page_count": 1,
                }
                if fire:
                    meta.update(
                        fsd_location=None,
                        bmz_location=None,
                        rwa_info=None,
                    )
                document["fire_plan" if fire else "klgv_plan"] = meta
            wire["documents"].append(document)
            self.artifacts[path] = (ciphertext, sha256_hex(ciphertext), False)
        info, encrypted = self.hpke(
            generation_key,
            descriptor,
            "firedash-document-generation-hpke-v2",
        )
        wire.update(
            signature_algorithm="Ed25519",
            signing_key_version="1",
            generation_key_grant={
                "scheme": "HPKE",
                "ciphersuite": HPKE_SUITE_NAME,
                "encapsulated_key": b64(encrypted[:65]),
                "wrapped_generation_key": b64(encrypted[65:]),
                "info": info,
            },
        )
        self.seal_generation(wire)
        self.generations[descriptor["manifest_url"]] = wire
        self.manifest["datasets"].append(descriptor)
        return descriptor, wire

    def call(self, method, path, *, bearer=None, headers=None, json_body=None):
        assert bearer == "synthetic-credential"
        assert json_body in (None, {})  # no capacity telemetry, including check-in/refresh
        headers = headers or {}
        self.calls.append((method, path, headers))
        status, body, response_headers = 200, {}, {}
        if path in self.failures:
            status = self.failures[path]
        elif self.pending.get(path, 0):
            self.pending[path] -= 1
            status, body, response_headers = 202, b"", {"retry-after": "5"}
        elif path == "/api/v1/tablet/configuration":
            body = self.config
        elif path == "/api/v1/tablet/status":
            body = {
                "status": "active",
                "purge_provisioned_data": False,
                "authorization_valid_until": "2099-01-01T00:00:00Z",
                "server_time": "2026-01-01T00:00:00Z",
            }
        elif path in {"/api/v1/tablet/check-in", "/api/v1/tablet/refresh"}:
            body = {
                "status": "active",
                "authorization_valid_until": "2099-01-01T00:00:00Z",
                "server_time": "2026-01-01T00:00:00Z",
            }
        elif path == "/api/v1/tablet/signing-keys/1":
            body = {
                "algorithm": "Ed25519",
                "version": "1",
                "public_key": b64(self.signer.public_key().public_bytes_raw()),
            }
        elif path == "/api/v1/tablet/manifest":
            status = 304 if headers.get("If-None-Match") == self.etag else 200
            body = b"" if status == 304 else self.manifest
            response_headers = {"etag": self.etag}
        elif path in self.generations:
            body = self.generations[path]
        elif path in self.artifacts:
            ciphertext, digest, ranges = self.artifacts[path]
            status = 304 if headers.get("If-None-Match") == f'"{digest}"' else 200
            body = b"" if status == 304 else ciphertext
            response_headers = {"content-type": "application/octet-stream", "etag": f'"{digest}"'}
            if ranges:
                response_headers["accept-ranges"] = "bytes"
        else:
            status = 404
        return HttpResponse(
            status,
            response_headers,
            body if isinstance(body, bytes) else json.dumps(body).encode(),
            path,
        )

    @staticmethod
    def expect(response, *ok, label):
        if response.status not in ok:
            raise ClientError(f"{label}: HTTP {response.status}")
        return response


@pytest.fixture
def delivery(tmp_path):
    return Delivery(tmp_path)


@pytest.fixture
def nine(delivery):
    delivery.add_v1("dangerous_goods", json.dumps(_dangerous_goods_document()).encode())
    delivery.add_v1(
        "department_hydrants",
        json.dumps(
            {"type": "FeatureCollection", "schema_version": 1, "source_revision": 1, "features": []}
        ).encode(),
        "geojson",
    )
    delivery.add_v1(
        "station_personnel",
        json.dumps(
            {"station_id": delivery.config["station_id"], "source_revision": 1, "people": []}
        ).encode(),
    )
    for kind in ("department_phonebook", "station_phonebook"):
        delivery.add_v1(
            kind,
            json.dumps(
                {
                    "source_revision": 1,
                    "entries": [
                        {
                            "id": uid(),
                            "first_name": None,
                            "last_name": "Müller",
                            "organization_unit": None,
                            "function": None,
                            "phone_number": "112",
                        }
                    ],
                }
            ).encode(),
        )
    for kind in (
        "department_files",
        "station_files",
        "department_fire_plans",
        "department_klgv_plans",
    ):
        delivery.add_v2(kind, count=2)
    delivery.seal_main()
    return delivery


def test_all_nine_datasets_full_verify_and_conditional_reuse(nine):
    results = nine.client.verify()["datasets"]
    assert len(results) == 9
    assert all(results.values())
    assert "VERIFY RESULT: PASS" in nine.stream.getvalue()
    assert nine.client.verified_snapshot_etag(nine.config) == nine.etag
    before = len(nine.calls)
    assert nine.client.update_check(
        expect_changed=[], expect_unchanged=[], expect_version_increase=[]
    ) == {"changed": False}
    assert not any("/artifacts/" in path for _, path, _ in nine.calls[before:])


@pytest.mark.parametrize("command", ["manifest", "download", "refresh"])
def test_normal_commands_verify_all_nine(nine, command):
    if command == "manifest":
        nine.client.manifest(download=True)
    elif command == "download":
        nine.client.download(dataset_types=["dangerous_goods"])
    else:
        nine.client.refresh()
    assert len(nine.state.state["last_dataset_validation"]) == 9


@pytest.mark.parametrize("count,with_folder", [(0, False), (0, True), (2, True)])
@pytest.mark.parametrize("kind", ["department_files", "station_files"])
def test_empty_and_nonempty_file_libraries(delivery, count, with_folder, kind):
    folders = [{"id": uid(), "name": "Empty", "parent_id": None}] if with_folder else []
    descriptor, wire = delivery.add_v2(kind, count, folders)
    original = copy.deepcopy(wire)
    delivery.seal_main()
    result = delivery.client.verify()["datasets"][kind]
    assert result["documents"] == count
    assert wire == original  # display names, including scope, are never rewritten
    assert all(not path.startswith("../") for _, path, _ in delivery.calls)
    assert descriptor["scope"]["type"] == ("STATION" if kind == "station_files" else "DEPARTMENT")


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol", "firedash-fire-plan-generation-hpke-v2"),
        ("installation_id", uid()),
        ("tablet_id", uid()),
        ("publication_id", uid()),
        ("version_number", 2),
        ("schema_version", 1),
        (
            "scope",
            {"dataset_type_code": "station_files", "department_id": None, "station_id": None},
        ),
    ],
)
@pytest.mark.parametrize("kind", ["department_fire_plans", "department_files"])
def test_generation_rejects_wrong_complete_hpke_binding(delivery, field, value, kind):
    _, wire = delivery.add_v2(kind)
    wire["generation_key_grant"]["info"][field] = value
    delivery.seal_main()
    with pytest.raises(ClientError, match="HPKE binding"):
        delivery.client.verify()
    assert not delivery.state.manifest_path.exists()


@pytest.mark.parametrize(
    "kind", ["department_fire_plans", "department_klgv_plans", "department_files", "station_files"]
)
def test_grant_open_uses_actual_hpke_context(delivery, kind):
    _, wire = delivery.add_v2(kind)
    raw = bytearray(base64.b64decode(wire["generation_key_grant"]["wrapped_generation_key"]))
    raw[-1] ^= 1
    wire["generation_key_grant"]["wrapped_generation_key"] = b64(raw)
    delivery.seal_main()
    with pytest.raises(ClientError, match="HPKE open failed"):
        delivery.client.verify()


@pytest.mark.parametrize(
    "kind,fault",
    [
        (kind, fault)
        for kind in ("department_fire_plans", "department_files")
        for fault in (
            "signature",
            "key-version",
            "publication",
            "version",
            "schema",
            "dataset-type",
            "scope",
            "nonce",
            "algorithm",
            "wrapped-cek",
            "gcm",
            "plaintext-hash",
            "plaintext-size",
            "ciphertext",
            "ciphertext-size",
            "download-origin",
        )
        if kind == "department_files" or fault != "plaintext-size"
    ],
)
def test_v2_crypto_and_binding_fail_closed(delivery, fault, kind):
    descriptor, wire = delivery.add_v2(kind)
    document = wire["documents"][0]
    if fault == "signature":
        wire["signature"] = b64(os.urandom(64))
    elif fault == "key-version":
        wire["signing_key_version"] = "missing"
    elif fault in {"publication", "version", "schema", "dataset-type"}:
        key = {
            "publication": "publication_id",
            "version": "version",
            "schema": "schema_version",
            "dataset-type": "dataset_type",
        }[fault]
        wire[key] = (
            uid() if fault == "publication" else "station_files" if fault == "dataset-type" else 3
        )
    elif fault == "scope":
        wire["generation_key_grant"]["info"]["scope"] = {"type": "SYSTEM"}
    elif fault == "nonce":
        document["nonce"] = b64(b"short")
    elif fault == "algorithm":
        document["generation_key_wrapping_algorithm"] = "none"
    elif fault == "wrapped-cek":
        document["generation_wrapped_cek"] = b64(os.urandom(40))
    elif fault == "gcm":
        document["nonce"] = b64(os.urandom(12))
    elif fault == "plaintext-hash":
        document["fire_plan" if kind == "department_fire_plans" else "file"]["sha256"] = document[
            "sanitized_pdf_sha256"
        ] = "0" * 64
    elif fault == "plaintext-size":
        document["file"]["size_bytes"] += 1
    elif fault == "ciphertext":
        ciphertext, digest, ranges = delivery.artifacts[document["download_path"]]
        delivery.artifacts[document["download_path"]] = (
            bytes([ciphertext[0] ^ 1]) + ciphertext[1:],
            digest,
            ranges,
        )
    elif fault == "ciphertext-size":
        document["ciphertext_size"] += 1
    else:
        document["download_path"] = "https://evil.example/download"
    if fault not in {"signature", "key-version"}:
        delivery.seal_generation(wire)
    delivery.seal_main()
    with pytest.raises(ClientError):
        delivery.client.verify()
    assert "verified_snapshot" not in delivery.state.state
    assert not delivery.state.manifest_path.exists()
    assert all("evil.example" not in path for _, path, _ in delivery.calls)


@pytest.mark.parametrize(
    "fault",
    [
        "missing-parent",
        "cycle",
        "depth",
        "duplicate-folder",
        "duplicate-document",
        "folder-document-collision",
        "missing-folder",
        "uppercase-id",
    ],
)
def test_folder_and_document_identity_validation(delivery, fault):
    folders = [
        {"id": uid(), "name": "Folder", "parent_id": None}
        for _ in range(6 if fault == "depth" else 2)
    ]
    for i in range(1, len(folders)):
        folders[i]["parent_id"] = folders[i - 1]["id"]
    descriptor, wire = delivery.add_v2(
        "department_files", count=2, folders=sorted(folders, key=lambda f: f["id"])
    )
    if fault == "missing-parent":
        wire["folders"][0]["parent_id"] = uid()
    elif fault == "cycle":
        folders[0]["parent_id"] = folders[-1]["id"]
    elif fault == "duplicate-folder":
        wire["folders"].append(wire["folders"][0].copy())
    elif fault == "duplicate-document":
        wire["documents"][1]["file"]["id"] = wire["documents"][0]["file"]["id"]
    elif fault == "folder-document-collision":
        wire["documents"][0]["file"]["id"] = folders[0]["id"]
    elif fault == "missing-folder":
        wire["documents"][0]["file"]["folder_id"] = uid()
    elif fault == "uppercase-id":
        wire["folders"][0]["id"] = wire["folders"][0]["id"].upper()
    delivery.seal_generation(wire)
    delivery.seal_main()
    with pytest.raises(ClientError):
        delivery.client.verify()
    assert not any("/artifacts/" in path for _, path, _ in delivery.calls)


def test_generation_pending_empty_body_and_retry_after(delivery, monkeypatch):
    descriptor, _ = delivery.add_v2("department_fire_plans")
    delivery.pending[descriptor["manifest_url"]] = 2
    delivery.seal_main()
    sleeps = []
    monkeypatch.setattr("tools.fake_ipad.generations.time.sleep", sleeps.append)
    delivery.client.verify()
    assert sleeps == [5, 5]


@pytest.mark.parametrize("status", [403, 404, 426])
@pytest.mark.parametrize("where", ["generation", "artifact", "configuration"])
def test_authorization_loss_never_reuses_or_commits_incomplete_result(delivery, status, where):
    descriptor, wire = delivery.add_v2("station_files", count=2)
    delivery.seal_main()
    delivery.client.verify()
    previous = delivery.state.manifest_path.read_bytes()
    snapshot = copy.deepcopy(delivery.state.state["verified_snapshot"])
    delivery.manifest["manifest_generation"] += 1
    delivery.seal_main()
    path = (
        descriptor["manifest_url"]
        if where == "generation"
        else wire["documents"][-1]["download_path"]
        if where == "artifact"
        else "/api/v1/tablet/configuration"
    )
    delivery.failures[path] = status
    with pytest.raises(ClientError):
        delivery.client.verify()
    assert delivery.state.manifest_path.read_bytes() == previous
    assert delivery.state.state["verified_snapshot"] == snapshot


@pytest.mark.parametrize(
    "damage",
    [
        "no-marker",
        "no-manifest",
        "incomplete",
        "modified-result",
        "manifest-tampered",
        "configuration-changed",
    ],
)
def test_304_requires_complete_current_verified_snapshot(nine, damage):
    nine.client.verify()
    if damage == "no-marker":
        nine.state.state.pop("verified_snapshot")
    elif damage == "no-manifest":
        nine.state.manifest_path.unlink()
    elif damage == "incomplete":
        nine.state.state["last_dataset_validation"].pop("department_files")
    elif damage == "modified-result":
        nine.state.state["last_dataset_validation"]["department_files"] = {}
    elif damage == "manifest-tampered":
        nine.state.manifest_path.write_text("{}")
    else:
        nine.state.state["verified_snapshot"]["configuration"] = {}
    before = len(nine.calls)
    nine.client.download()
    manifest_calls = [
        headers for _, path, headers in nine.calls[before:] if path == "/api/v1/tablet/manifest"
    ]
    assert not manifest_calls[0].get("If-None-Match")
    assert nine.client.verified_snapshot_etag(nine.config) == nine.etag


def test_fire_plan_zip_is_rejected_even_if_optional(delivery):
    descriptor = delivery.add_v1("department_fire_plans", b"obsolete ZIP", "zip")
    descriptor["required"] = False
    delivery.seal_main()
    with pytest.raises(ClientError, match="requires document-manifest-v2 schema 2"):
        delivery.client.verify()
    assert "verified_snapshot" not in delivery.state.state


def test_adoption_runs_complete_nine_dataset_verification(nine, monkeypatch):
    # Existing adoption tests cover the challenge/proof exchange separately.
    nine.state.state.pop("credential")
    nine.server_url = "https://tablet.example"
    monkeypatch.setattr(
        nine.client,
        "_preview_and_proof",
        lambda **kw: ({"tablet_id": nine.config["tablet_id"]}, b"synthetic-proof"),
    )
    monkeypatch.setattr(
        nine.client,
        "_complete",
        lambda **kw: {
            "installation_id": nine.config["installation_id"],
            "credential": "synthetic-credential",
            "authorization_valid_until": "2099-01-01T00:00:00Z",
            "server_time": "2026-01-01T00:00:00Z",
        },
    )
    nine.client.adopt("synthetic-token")
    assert len(nine.state.state["last_dataset_validation"]) == 9
    assert "DOWNLOAD SUMMARY" in nine.stream.getvalue()
    assert "Fire Plans" in nine.stream.getvalue()
    assert "ciphertext SHA-256" not in nine.stream.getvalue()


@pytest.mark.parametrize("kind", ["department_phonebook", "station_phonebook"])
@pytest.mark.parametrize(
    "fault",
    ["duplicate", "identity", "missing-field", "phone", "nullable-field", "revision", "scope"],
)
def test_phonebook_contract_rejects_invalid_entries_and_scope(delivery, kind, fault):
    entry = {
        "id": uid(),
        "first_name": None,
        "last_name": None,
        "organization_unit": "Control",
        "function": None,
        "phone_number": "112",
    }
    doc = {"entries": [entry], "source_revision": 1}
    if fault == "duplicate":
        doc["entries"].append(entry.copy())
    elif fault == "identity":
        entry["id"] = "invalid"
    elif fault == "missing-field":
        entry.pop("function")
    elif fault == "phone":
        entry["phone_number"] = None
    elif fault == "nullable-field":
        entry["last_name"] = 12
    elif fault == "revision":
        doc["source_revision"] = True
    descriptor = delivery.add_v1(kind, json.dumps(doc).encode())
    if fault == "scope":
        descriptor["scope"] = {"type": "SYSTEM"}
    delivery.seal_main()
    with pytest.raises(ClientError):
        delivery.client.verify()
    assert not delivery.state.manifest_path.exists()


def test_phonebooks_are_separate_snapshots_even_with_same_entry_identity(delivery):
    entry_id = uid()
    for index, kind in enumerate(("department_phonebook", "station_phonebook")):
        entries = [
            {
                "id": entry_id,
                "first_name": None,
                "last_name": None,
                "organization_unit": None,
                "function": None,
                "phone_number": str(index),
            }
        ]
        delivery.add_v1(kind, json.dumps({"entries": entries, "source_revision": index}).encode())
    delivery.seal_main()
    results = delivery.client.verify()["datasets"]
    assert results["department_phonebook"] == {"entries": 1, "source_revision": 0}
    assert results["station_phonebook"] == {"entries": 1, "source_revision": 1}


@pytest.mark.parametrize("required", [True, False])
def test_unknown_required_fails_optional_is_not_validated(delivery, required):
    descriptor, _ = delivery.add_v2("department_files", count=0)
    unknown = copy.deepcopy(descriptor)
    unknown.update(type="future_documents", publication_id=uid(), required=required)
    unknown["manifest_url"] = f"{endpoint(unknown)}/manifest"
    delivery.manifest["datasets"].append(unknown)
    delivery.seal_main()
    if required:
        with pytest.raises(ClientError, match="required dataset is unsupported"):
            delivery.client.verify()
    else:
        assert set(delivery.client.verify()["datasets"]) == {"department_files"}
        assert delivery.state.state["verified_snapshot"]["datasets"] == ["department_files"]
    assert not any(path == unknown["manifest_url"] for _, path, _ in delivery.calls)


@pytest.mark.parametrize("fault", ["body", "missing-retry", "negative-retry", "exhausted"])
def test_generation_202_is_not_a_main_manifest_pending_response(delivery, monkeypatch, fault):
    descriptor, _ = delivery.add_v2("department_files")
    delivery.seal_main()
    original = delivery.call
    sleeps = []
    monkeypatch.setattr("tools.fake_ipad.generations.time.sleep", sleeps.append)

    def pending(method, path, **kwargs):
        if path == descriptor["manifest_url"]:
            return HttpResponse(
                202,
                {}
                if fault == "missing-retry"
                else {"retry-after": "-1" if fault == "negative-retry" else "5"},
                b'{"code":"manifest_pending"}' if fault == "body" else b"",
                path,
            )
        return original(method, path, **kwargs)

    monkeypatch.setattr(delivery, "call", pending)
    with pytest.raises(ClientError, match="generation"):
        delivery.client.verify()
    assert len(sleeps) == (11 if fault == "exhausted" else 0)


@pytest.mark.parametrize(
    "fault", ["etag", "conditional-status", "conditional-body", "unauthorized-conditional"]
)
def test_document_download_etag_and_conditional_authorization(delivery, monkeypatch, fault):
    _, wire = delivery.add_v2("department_klgv_plans")
    delivery.seal_main()
    path = wire["documents"][0]["download_path"]
    original = delivery.call

    def damaged(method, request_path, **kwargs):
        response = original(method, request_path, **kwargs)
        if request_path == path:
            if fault == "etag":
                response.headers["etag"] = '"wrong"'
            elif kwargs.get("headers", {}).get("If-None-Match"):
                if fault == "conditional-body":
                    response.body = b"unexpected"
                else:
                    response.status = 403 if fault == "unauthorized-conditional" else 200
        return response

    monkeypatch.setattr(delivery, "call", damaged)
    with pytest.raises(ClientError):
        delivery.client.verify()


@pytest.mark.parametrize("kind", ["department_fire_plans", "department_klgv_plans"])
@pytest.mark.parametrize(
    "fault",
    [
        "missing-metadata",
        "page-count",
        "unpaired-coordinates",
        "invalid-coordinates",
        "metadata-hash",
        "extra-metadata",
    ],
)
def test_plan_specific_schema_two_metadata(delivery, kind, fault):
    _, wire = delivery.add_v2(kind)
    meta = wire["documents"][0]["fire_plan" if kind == "department_fire_plans" else "klgv_plan"]
    if fault == "missing-metadata":
        meta.pop("external_identifier")
    elif fault == "page-count":
        meta["page_count"] = True
    elif fault == "unpaired-coordinates":
        meta["longitude"] = 8.1
    elif fault == "invalid-coordinates":
        meta.update(longitude=181, latitude=0)
    elif fault == "metadata-hash":
        meta["sha256"] = "0" * 64
    else:
        meta["internal_path"] = "not part of tablet wire format"
    delivery.seal_generation(wire)
    delivery.seal_main()
    with pytest.raises(ClientError):
        delivery.client.verify()
    assert not any("/artifacts/" in path for _, path, _ in delivery.calls)


def test_five_folder_levels_are_valid(delivery):
    folders = []
    for i in range(5):
        folders.append(
            {"id": uid(), "name": str(i), "parent_id": folders[-1]["id"] if folders else None}
        )
    delivery.add_v2("department_files", folders=sorted(folders, key=lambda folder: folder["id"]))
    delivery.seal_main()
    delivery.client.verify()


def test_partial_optional_download_cannot_establish_complete_snapshot(nine):
    for descriptor in nine.manifest["datasets"]:
        if descriptor["type"].endswith("phonebook"):
            descriptor["required"] = False
    nine.seal_main()
    results = nine.client.download(dataset_types=["dangerous_goods"])
    assert len(results["datasets"]) == 7  # every required dataset still verifies
    assert nine.client.verified_snapshot_etag(nine.config) is None
    assert not nine.state.manifest_path.exists()
    nine.client.download()
    assert nine.client.verified_snapshot_etag(nine.config) == nine.etag


@pytest.mark.parametrize("command", ["download", "manifest", "refresh"])
def test_unconditional_304_never_claims_missing_snapshot_is_valid(nine, monkeypatch, command):
    monkeypatch.setattr(
        nine.client,
        "fetch_manifest",
        lambda **kw: HttpResponse(304, {}, b"", "/api/v1/tablet/manifest"),
    )
    with pytest.raises(ClientError, match="complete verified snapshot"):
        if command == "manifest":
            nine.client.manifest(download=True, if_none_match='"old-etag-only"')
        else:
            getattr(nine.client, command)()
    assert "verified_snapshot" not in nine.state.state


def test_server_authoritative_empty_library_replaces_previous_snapshot(delivery):
    delivery.add_v2("department_files", count=2)
    delivery.seal_main()
    assert delivery.client.verify()["datasets"]["department_files"]["documents"] == 2
    delivery.manifest["datasets"] = []
    delivery.add_v2("department_files", count=0)
    delivery.manifest["manifest_generation"] += 1
    delivery.seal_main()
    delivery.client.refresh()
    assert delivery.state.state["last_dataset_validation"]["department_files"]["documents"] == 0


def test_complete_snapshot_reuse_survives_process_restart(nine):
    nine.client.verify()
    restored = DeviceState(nine.state.state_dir)
    client = FakeIPadClient(restored, nine, out=Output(stream=io.StringIO()))
    assert client.verified_snapshot_etag(nine.config) == nine.etag
    assert client.download() == {"changed": False}


def test_generation_loads_its_own_exact_signing_key_version(delivery, monkeypatch):
    _, wire = delivery.add_v2("department_fire_plans")
    generation_signer = Ed25519PrivateKey.generate()
    wire["signing_key_version"] = "generation-2"
    wire["signature"] = b64(
        generation_signer.sign(
            canonical_json_bytes({k: v for k, v in wire.items() if k not in RESPONSE_FIELDS})
        )
    )
    delivery.seal_main()
    original = delivery.call
    loaded = []

    def rotated(method, path, **kwargs):
        if path == "/api/v1/tablet/signing-keys/generation-2":
            loaded.append(path)
            assert kwargs["bearer"] == "synthetic-credential"
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "algorithm": "Ed25519",
                        "version": "generation-2",
                        "public_key": b64(generation_signer.public_key().public_bytes_raw()),
                    }
                ).encode(),
                path,
            )
        return original(method, path, **kwargs)

    monkeypatch.setattr(delivery, "call", rotated)
    delivery.client.verify()
    assert len(loaded) == 1
    assert (
        delivery.state.cached_signing_key("generation-2")
        == generation_signer.public_key().public_bytes_raw()
    )


@pytest.mark.parametrize("count", [0, 2])
def test_canonical_fire_plan_generation_metadata_and_empty(delivery, count):
    descriptor, wire = delivery.add_v2("department_fire_plans", count=count)
    assert descriptor["manifest_url"] == (
        f"/api/v1/tablet/document-generations/{descriptor['publication_id']}/manifest"
    )
    assert wire["format"] == "document-generation-v2"
    assert wire["dataset_type"] == descriptor["type"]
    for document in wire["documents"]:
        assert set(document["fire_plan"]) == {
            "id",
            "external_identifier",
            "object_name",
            "address",
            "postal_code",
            "city",
            "fsd_location",
            "bmz_location",
            "rwa_info",
            "longitude",
            "latitude",
            "sha256",
            "page_count",
        }
        document["fire_plan"].update(
            fsd_location="FSD", bmz_location="BMZ", rwa_info="RWA", longitude=8.1, latitude=50.2
        )
        assert document["download_path"] == (
            f"/api/v1/tablet/document-generations/{descriptor['publication_id']}"
            f"/artifacts/{document['artifact_id']}/download"
        )
    delivery.seal_generation(wire)
    delivery.seal_main()
    assert delivery.client.verify()["datasets"][descriptor["type"]]["documents"] == count
    assert not (delivery.state.state_dir / "last-plaintext").exists()


@pytest.mark.parametrize(
    "fault",
    [
        "endpoint",
        "artifact-endpoint",
        "format",
        "missing-type",
        "wrong-type",
        "path",
        "missing-document-id",
        "duplicate-document",
        "duplicate-artifact",
        "boolean-version",
        "boolean-size",
        "boolean-longitude",
        "boolean-page-count",
        "missing-fsd",
    ],
)
def test_fire_plan_obsolete_or_malformed_generation_never_commits(delivery, fault):
    descriptor, wire = delivery.add_v2("department_fire_plans", count=2)
    document = wire["documents"][0]
    meta = document["fire_plan"]
    if fault == "endpoint":
        descriptor["manifest_url"] = descriptor["manifest_url"].replace(
            "document-generations", "fire-plan-generations"
        )
    elif fault == "artifact-endpoint":
        document["download_path"] = document["download_path"].replace(
            "document-generations", "fire-plan-generations"
        )
    elif fault == "format":
        wire["format"] = "fire-plan-generation-v2"
    elif fault == "missing-type":
        wire.pop("dataset_type")
    elif fault == "wrong-type":
        wire["dataset_type"] = "department_klgv_plans"
    elif fault == "path":
        meta["path"] = f"plans/{meta['id']}.pdf"
    elif fault == "missing-document-id":
        meta.pop("id")
    elif fault == "duplicate-document":
        wire["documents"][1]["fire_plan"]["id"] = meta["id"]
    elif fault == "duplicate-artifact":
        wire["documents"][1]["artifact_id"] = document["artifact_id"]
    elif fault == "boolean-version":
        wire["version"] = True
    elif fault == "boolean-size":
        document["ciphertext_size"] = True
    elif fault == "boolean-longitude":
        meta.update(longitude=True, latitude=50)
    elif fault == "boolean-page-count":
        meta["page_count"] = True
    else:
        meta.pop("fsd_location")
    delivery.seal_generation(wire)
    delivery.seal_main()
    with pytest.raises(ClientError):
        delivery.client.verify()
    assert "verified_snapshot" not in delivery.state.state
    assert not delivery.state.manifest_path.exists()
    assert not any("/artifacts/" in path for _, path, _ in delivery.calls)


@pytest.mark.parametrize("where", ["generation", "ciphertext"])
def test_unconditional_304_cannot_supply_document_content(delivery, where):
    descriptor, wire = delivery.add_v2("department_fire_plans")
    delivery.seal_main()
    path = (
        descriptor["manifest_url"]
        if where == "generation"
        else wire["documents"][0]["download_path"]
    )
    delivery.failures[path] = 304
    with pytest.raises(ClientError, match="304"):
        delivery.client.verify()
    assert "verified_snapshot" not in delivery.state.state
    assert not delivery.state.manifest_path.exists()


@pytest.mark.parametrize("diameter,valid", [(0, True), (-1, False), (True, False), (1.5, False)])
def test_hydrant_zero_diameter_remains_valid(delivery, diameter, valid):
    delivery.add_v1(
        "department_hydrants",
        json.dumps(
            {
                "type": "FeatureCollection",
                "schema_version": 1,
                "source_revision": 1,
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [8, 50]},
                        "properties": {
                            "external_identifier": "A",
                            "hydrant_type": "underground",
                            "diameter_mm": diameter,
                            "status": "active",
                        },
                    }
                ],
            }
        ).encode(),
        "geojson",
    )
    delivery.seal_main()
    if valid:
        assert delivery.client.verify()["datasets"]["department_hydrants"]["features"] == 1
    else:
        with pytest.raises(ClientError, match="diameter"):
            delivery.client.verify()


@pytest.mark.parametrize(
    "kind",
    [
        "department_fire_plans",
        "department_klgv_plans",
        "department_files",
        "station_files",
    ],
)
def test_document_hpke_uses_exact_canonical_authenticated_info(delivery, monkeypatch, kind):
    descriptor, _ = delivery.add_v2(kind)
    expected = {
        "protocol": "firedash-document-generation-hpke-v2",
        "publication_id": descriptor["publication_id"],
        "installation_id": delivery.config["installation_id"],
        "tablet_id": delivery.config["tablet_id"],
        "scope": {
            "dataset_type_code": kind,
            "department_id": delivery.config["department_id"],
            "station_id": delivery.config["station_id"] if kind == "station_files" else None,
        },
        "version_number": 1,
        "schema_version": 2,
    }
    original = delivery.crypto.hpke_open
    calls = []

    def checked_open(encrypted, *, info):
        calls.append(info)
        assert len(encrypted) == 65 + 48
        assert info == json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return original(encrypted, info=info)

    monkeypatch.setattr(delivery.crypto, "hpke_open", checked_open)
    # DeviceState reloads the same persisted identity for verification.
    monkeypatch.setattr(delivery.state, "load_private_key", lambda: delivery.crypto)
    delivery.seal_main()
    delivery.client.verify()
    assert len(calls) == 1
