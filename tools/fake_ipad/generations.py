"""Tablet document-v2 validation, independent of Django and server key material."""

from __future__ import annotations

import math
import re
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap

from tools.fake_ipad.crypto import (
    HPKE_SUITE_NAME,
    aes_gcm_decrypt,
    canonical_json_bytes,
    sha256_hex,
    strict_b64,
)
from tools.fake_ipad.errors import fail
from tools.fake_ipad.validation import require_string, require_uuid

RESPONSE_FIELDS = {
    "signature",
    "signature_algorithm",
    "signing_key_version",
    "generation_key_grant",
}
ENVELOPE = {
    "artifact_id",
    "sanitized_pdf_sha256",
    "ciphertext_sha256",
    "ciphertext_size",
    "nonce",
    "encryption_algorithm",
    "wrapping_algorithm",
    "kek_version",
    "signature",
    "signature_algorithm",
    "signing_key_version",
    "generation_wrapped_cek",
    "generation_key_wrapping_algorithm",
    "download_path",
}
PLAN_FIELDS = {
    "id",
    "external_identifier",
    "object_name",
    "address",
    "postal_code",
    "city",
    "longitude",
    "latitude",
    "sha256",
    "page_count",
}


def exact(value: Any, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        fail(f"{label}: incorrect object fields")
    return value


def integer(value: Any, label: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        fail(f"{label}: expected integer >= {minimum}")


def identity(value: Any, label: str) -> str:
    canonical = require_uuid(value, label=label)
    if value != canonical:
        fail(f"{label}: UUID must be canonical lowercase")
    return canonical


def digest(value: Any) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        fail("generation: malformed SHA-256")


def binary(value: Any, size: int, label: str) -> bytes:
    raw = strict_b64(value, label=label)
    if len(raw) != size:
        fail(f"{label}: expected {size} bytes")
    return raw


def endpoint(dataset: dict) -> str:
    family = (
        "fire-plan-generations"
        if dataset["type"] == "department_fire_plans"
        else "document-generations"
    )
    return f"/api/v1/tablet/{family}/{dataset['publication_id']}"


def validate_generation(payload: dict, dataset: dict) -> list[dict]:
    """Validate the entire signed snapshot before fetching any document."""
    fire = dataset["type"] == "department_fire_plans"
    library = dataset["type"] in {"department_files", "station_files"}
    fields = {"format", "publication_id", "version", "schema_version", "documents"}
    if not fire:
        fields.add("dataset_type")
    if library:
        fields.update({"library", "folders", "source_revision"})
    exact(payload, fields, "generation")
    if payload["format"] != ("fire-plan-generation-v2" if fire else "document-generation-v2"):
        fail("generation: format mismatch")
    for key in ("publication_id", "version", "schema_version"):
        if payload[key] != dataset[key] or type(payload[key]) is not type(dataset[key]):
            fail(f"generation: {key} mismatch")
    if not fire and payload["dataset_type"] != dataset["type"]:
        fail("generation: dataset_type mismatch")
    if payload["schema_version"] != 2:
        fail("generation: schema must be 2")
    folders = {}
    identities = set()
    if library:
        integer(payload["source_revision"], "source_revision")
        lib = exact(payload["library"], {"id", "name", "scope"}, "library")
        identity(lib["id"], "library.id")
        require_string(lib["name"], label="library.name")
        if lib["scope"] != dataset["scope"]:
            fail("library: scope mismatch")
        if not isinstance(payload["folders"], list):
            fail("folders: expected array")
        folder_order = []
        for folder in payload["folders"]:
            exact(folder, {"id", "name", "parent_id"}, "folder")
            folder_id = identity(folder["id"], "folder.id")
            require_string(folder["name"], label="folder.name")
            if folder_id in identities:
                fail("folder: duplicate identity")
            identities.add(folder_id)
            folder_order.append(folder_id)
            if folder["parent_id"] is not None:
                identity(folder["parent_id"], "folder.parent_id")
            folders[folder_id] = folder["parent_id"]
        if folder_order != sorted(folder_order):
            fail("folders: UUID order mismatch")
        for folder_id in folders:
            seen = set()
            current = folder_id
            while current is not None:
                if current not in folders or current in seen or len(seen) >= 5:
                    fail("folders: missing parent, cycle or more than five levels")
                seen.add(current)
                current = folders[current]
    documents = payload["documents"]
    if not isinstance(documents, list):
        fail("documents: expected array")
    artifact_ids = set()
    document_order = []
    metadata_key = "file" if library else "fire_plan" if fire else "klgv_plan"
    for document in documents:
        exact(document, ENVELOPE | {metadata_key}, "document")
        artifact_id = identity(document["artifact_id"], "artifact_id")
        if artifact_id in artifact_ids:
            fail("document: duplicate artifact identity")
        artifact_ids.add(artifact_id)
        meta_fields = (
            {"id", "name", "folder_id", "size_bytes", "sha256"} if library else PLAN_FIELDS
        )
        if fire:
            meta_fields = meta_fields | {"fsd_location", "bmz_location", "rwa_info", "path"}
        meta = exact(document[metadata_key], meta_fields, metadata_key)
        doc_id = identity(meta["id"], f"{metadata_key}.id")
        if doc_id in identities:
            fail("document: duplicate identity")
        identities.add(doc_id)
        document_order.append(doc_id)
        if library:
            require_string(meta["name"], label="file.name")
            integer(meta["size_bytes"], "file.size_bytes", 1)
            if meta["folder_id"] is not None:
                identity(meta["folder_id"], "file.folder_id")
                if meta["folder_id"] not in folders:
                    fail("file: missing folder")
        else:
            for field in meta_fields - {
                "id",
                "sha256",
                "page_count",
                "longitude",
                "latitude",
                "path",
            }:
                if meta[field] is not None and not isinstance(meta[field], str):
                    fail(f"{metadata_key}.{field}: expected string or null")
            integer(meta["page_count"], "page_count", 1)
            lon, lat = meta["longitude"], meta["latitude"]
            if (lon is None) != (lat is None):
                fail("plan: coordinates must be paired")
            if lon is not None and (
                any(type(v) not in (int, float) or not math.isfinite(v) for v in (lon, lat))
                or not (-180 <= lon <= 180 and -90 <= lat <= 90)
            ):
                fail("plan: invalid coordinates")
            if fire and meta["path"] != f"plans/{doc_id}.pdf":
                fail("fire_plan: invalid legacy metadata path")
        for field in ("sanitized_pdf_sha256", "ciphertext_sha256"):
            digest(document[field])
        digest(meta["sha256"])
        if meta["sha256"] != document["sanitized_pdf_sha256"]:
            fail("document: plaintext metadata hash mismatch")
        integer(document["ciphertext_size"], "ciphertext_size", 17)
        for field, expected in (
            ("encryption_algorithm", "AES-256-GCM"),
            ("wrapping_algorithm", "AES-KW-RFC3394"),
            ("generation_key_wrapping_algorithm", "AES-KW-RFC3394"),
            ("signature_algorithm", "Ed25519"),
        ):
            if document[field] != expected:
                fail(f"document: unsupported {field}")
        for field in ("kek_version", "signing_key_version"):
            require_string(document[field], label=field)
        binary(document["nonce"], 12, "nonce")
        binary(document["signature"], 64, "artifact audit signature")
        binary(document["generation_wrapped_cek"], 40, "generation_wrapped_cek")
        if document["download_path"] != f"{endpoint(dataset)}/artifacts/{artifact_id}/download":
            fail("document: download_path mismatch")
    if document_order != sorted(document_order):
        fail("documents: UUID order mismatch")
    return documents


def verify_generation(client, dataset, config, crypto, key_cache) -> dict:
    """Authenticate, decrypt and validate every PDF; never persist partial results."""
    client.verification_stage("generation_manifest")
    scope = client.scope_for_dataset(dataset, config)
    if dataset["manifest_url"] != f"{endpoint(dataset)}/manifest":
        fail("generation: manifest_url mismatch")
    for attempt in range(12):
        response = client.api.call("GET", dataset["manifest_url"], bearer=client.state.credential)
        client.verification_stage("generation_manifest", response)
        if response.status != 202:
            break
        retry = response.headers.get("retry-after", "")
        if response.body or not re.fullmatch(r"[0-9]+", retry):
            fail("generation HTTP 202: expected empty body and Retry-After seconds")
        if attempt == 11:
            fail("generation remained pending after maximum attempts")
        client.out.line(f"  generation pending; Retry-After: {retry}s")
        time.sleep(int(retry))
    client.verification_stage("generation_manifest", response)
    client.api.expect(response, 200, label="generation manifest")
    wire = response.json()
    if not RESPONSE_FIELDS <= wire.keys():
        fail("generation: missing signature or grant fields")
    if wire["signature_algorithm"] != "Ed25519":
        fail("generation: unsupported signature algorithm")
    version = require_string(wire["signing_key_version"], label="generation.signing_key_version")
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", version):
        fail("generation: invalid signing-key version")
    client.verification_stage("generation_signature")
    key = client.get_signing_key(version, key_cache)
    payload = {k: v for k, v in wire.items() if k not in RESPONSE_FIELDS}
    try:
        key.verify(
            binary(wire["signature"], 64, "generation signature"), canonical_json_bytes(payload)
        )
    except InvalidSignature:
        fail("generation: Ed25519 signature verification FAILED")
    client.verification_stage("generation_structure")
    documents = validate_generation(payload, dataset)
    grant = exact(
        wire["generation_key_grant"],
        {
            "scheme",
            "ciphersuite",
            "encapsulated_key",
            "wrapped_generation_key",
            "info",
        },
        "generation_key_grant",
    )
    client.verification_stage("generation_hpke")
    info = {
        "protocol": "firedash-fire-plan-generation-hpke-v2"
        if dataset["type"] == "department_fire_plans"
        else "firedash-document-generation-hpke-v2",
        "publication_id": dataset["publication_id"],
        "installation_id": config["installation_id"],
        "tablet_id": config["tablet_id"],
        "scope": scope,
        "version_number": dataset["version"],
        "schema_version": 2,
    }
    if (
        grant["scheme"] != "HPKE"
        or grant["ciphersuite"] != HPKE_SUITE_NAME
        or canonical_json_bytes(grant["info"]) != canonical_json_bytes(info)
    ):
        fail("generation: HPKE binding mismatch")
    enc = binary(grant["encapsulated_key"], 65, "generation encapsulated_key")
    wrapped = binary(grant["wrapped_generation_key"], 48, "wrapped_generation_key")
    generation_key = crypto.hpke_open(enc + wrapped, info=canonical_json_bytes(info))
    if len(generation_key) != 32:
        fail("generation: HPKE must open a 32-byte key")
    total = 0
    client.verification_context.update(documents_total=len(documents), documents_verified=0)
    for index, document in enumerate(documents):
        metadata = next(
            (document[k] for k in ("file", "fire_plan", "klgv_plan") if k in document), {}
        )
        client.verification_context.update(
            artifact=document["artifact_id"],
            document=metadata.get("id"),
            documents_verified=index,
        )
        client.verification_context.pop("http_status", None)
        client.verification_context.pop("request_id", None)
        client.verification_stage("aes_kw")
        try:
            cek = aes_key_unwrap(
                generation_key,
                binary(document["generation_wrapped_cek"], 40, "generation_wrapped_cek"),
            )
        except InvalidUnwrap:
            fail("generation: AES-KW unwrap failed")
        if len(cek) != 32:
            fail("generation: CEK must be 32 bytes")
        ciphertext = client.download_ciphertext(
            {
                "type": dataset["type"],
                "download_url": document["download_path"],
                "ciphertext_sha256": document["ciphertext_sha256"],
                "encrypted_size": document["ciphertext_size"],
            },
            require_ranges=False,
        )
        client.verification_stage("aes_gcm")
        plaintext = aes_gcm_decrypt(cek, binary(document["nonce"], 12, "nonce"), ciphertext)
        client.verification_stage("plaintext_sha256")
        if sha256_hex(plaintext) != document["sanitized_pdf_sha256"]:
            fail("generation: plaintext SHA-256 mismatch")
        client.verification_stage("plaintext_size")
        if "file" in document and len(plaintext) != document["file"]["size_bytes"]:
            fail("generation: file.size_bytes mismatch")
        total += len(plaintext)
        client.verification_context["documents_verified"] = index + 1
        client.out.detail(
            f"  artifact {document['artifact_id']}: AES-KW, AES-GCM, PDF hash/size PASS"
        )
    client.out.line(
        "  signed generation, HPKE, AES-KW, AES-GCM and PDF hashes: "
        f"PASS ({len(documents)} documents)"
    )
    return {
        "documents": len(documents),
        "plaintext_bytes": total,
        "publication_id": dataset["publication_id"],
    }
