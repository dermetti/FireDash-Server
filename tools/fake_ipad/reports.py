"""Independent synthetic ReportDocument/JWE producer and durable delivery queue.

No Django imports or server builders. Only encrypted packages reach disk.
"""

import base64
import hashlib
import io
import json
import os
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PIL import Image, ImageDraw

from tools.fake_ipad.errors import ClientError, fail
from tools.fake_ipad.state import secure_write_json

ROOT = "/api/v1/tablet"
INCIDENTS = (
    ("14:08", "Hauptstrasse", "Baum blockiert die Fahrbahn"),
    ("14:47", "Eichenweg", "Baum versperrt eine Einfahrt"),
    ("15:32", "Bahnhofstrasse", "Sturmgeschaedigter Baum"),
    ("16:16", "Kirchstrasse", "Ast auf geparktem Fahrzeug"),
    ("17:05", "Waldweg", "Baum versperrt eine Zufahrt"),
)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def identity(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        fail("Expected a UUID")


def field(key, label, value, kind="text"):
    return {"key": key, "label": label, "value": {"type": kind, "value": value}}


def synthetic(index, *, day=None):
    """Return one fictional finalized document and three genuine in-memory JPEGs."""
    clock, location, situation = INCIDENTS[index]
    day = day or datetime.now(UTC).date()
    incident_at = datetime.fromisoformat(f"{day.isoformat()}T{clock}:00+00:00")
    incident = f"TEST-{index + 1:03d}"
    report_id = str(uuid4())
    attachments, photos = [], []
    for number in range(1, 4):
        attachment_id = str(uuid4())
        output = io.BytesIO()
        image = Image.new("RGB", (480, 320), (40 + index * 25, 90, 120 + number * 20))
        ImageDraw.Draw(image).text(
            (25, 140), f"TEST / SIMULATED {incident} PHOTO {number}", fill="white"
        )
        image.save(output, "JPEG", quality=80)
        photos.append((attachment_id, output.getvalue()))
        attachments.append(
            {
                "id": attachment_id,
                "title": f"TEST Foto {number}",
                "caption": f"TEST / SIMULATED: {incident}, Ansicht {number}",
            }
        )
    document = {
        "report_id": report_id,
        "schema_version": 1,
        "report_type": "incident",
        "title": f"TEST / SIMULATED - Sturmeinsatz {incident}",
        "header": [
            field("incident", "Test-Einsatznummer", incident),
            field("time", "Einsatzzeit (Client-Angabe)", incident_at.isoformat(), "datetime"),
            field("location", "Fiktiver Einsatzort", f"TEST / SIMULATED - {location}"),
        ],
        "sections": [
            {
                "key": "incident",
                "title": "TEST / SIMULATED Einsatzbericht",
                "blocks": [
                    {
                        "key": "situation",
                        "kind": "text",
                        "text": f"TEST / SIMULATED: {situation}. Keine realen Einsatzdaten.",
                    },
                    {
                        "key": "measures",
                        "kind": "fields",
                        "fields": [
                            field(
                                "action",
                                "Massnahmen",
                                "TEST: Bereich abgesichert, Baum zerkleinert "
                                "und Fahrbahn geraeumt.",
                            )
                        ],
                    },
                    {
                        "key": "crew",
                        "kind": "table",
                        "columns": [
                            {"key": "vehicle", "label": "Testfahrzeug"},
                            {"key": "crew", "label": "Fiktive Besatzung"},
                        ],
                        "rows": [
                            [
                                {"type": "text", "value": "TEST HLF 1"},
                                {"type": "integer", "value": 6},
                            ]
                        ],
                    },
                    {
                        "key": "photos",
                        "kind": "attachment_refs",
                        "attachment_ids": [a["id"] for a in attachments],
                    },
                ],
            }
        ],
        "attachments": attachments,
        "finalization": {
            "author": {"id": "simulated-author", "display_name": "TEST / SIMULATED Einsatzleiter"},
            "finalized_at": (incident_at + timedelta(minutes=25)).isoformat(),
        },
    }
    return incident, document, photos


def envelope(config, recipient, document, photos, submission_id):
    """Build the flattened five-member RFC 7516 report envelope from public config."""
    if not isinstance(config, dict):
        fail("Invalid report configuration")
    public = config.get("report_transfer", {})
    if (
        not isinstance(public, dict)
        or public.get("enabled") is not True
        or public.get("version") != 1
        or public.get("alg") != "RSA-OAEP-256"
        or public.get("enc") != "A256GCM"
        or not isinstance(public.get("document_schema_versions"), list)
        or 1 not in public.get("document_schema_versions", [])
    ):
        fail("Report Transfer v1 is unavailable")
    try:
        key = serialization.load_pem_public_key(public["public_key_pem"].encode("ascii"))
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
            fail("Invalid report public key")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", public["kid"]):
            fail("Invalid report key version")
        for name in (
            "max_attachments",
            "max_json_bytes",
            "max_jpeg_bytes",
            "max_image_pixels",
            "max_request_bytes",
        ):
            if type(public[name]) is not int or public[name] <= 0:
                fail("Invalid report limits")
    except (KeyError, ValueError, TypeError, AttributeError, UnsupportedAlgorithm):
        fail("Invalid report configuration")
    if len(photos) > public["max_attachments"]:
        fail("Photograph count exceeds advertised limit")

    def content(raw, limit):
        if not 0 < len(raw) <= limit:
            fail("Synthetic report exceeds advertised size limit")
        return {"data": b64(raw), "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    attachments = []
    for photo_id, raw in photos:
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > public["max_image_pixels"]:
                fail("Synthetic photo exceeds pixel limit")
        attachments.append(
            {
                **content(raw, public["max_jpeg_bytes"]),
                "id": photo_id,
                "report_id": document["report_id"],
                "media_type": "image/jpeg",
            }
        )
    package = {
        "report": {
            **content(canonical(document), public["max_json_bytes"]),
            "attachment_ids": [a["id"] for a in attachments],
        },
        "attachments": attachments,
    }
    header = {
        "typ": "firedash-report+jwe",
        "alg": "RSA-OAEP-256",
        "enc": "A256GCM",
        "kid": public["kid"],
        "package_version": 1,
        "schema_version": "transport-json-v1",
        "submission_id": identity(submission_id),
        "report_id": identity(document["report_id"]),
        "recipient_personnel_id": identity(recipient),
        **{k: identity(config[k]) for k in ("installation_id", "department_id", "station_id")},
    }
    protected = b64(canonical(header))
    cek, iv = os.urandom(32), os.urandom(12)
    encrypted = AESGCM(cek).encrypt(iv, canonical(package), protected.encode("ascii"))
    result = {
        "protected": protected,
        "encrypted_key": b64(
            key.encrypt(
                cek,
                padding.OAEP(
                    mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
                ),
            )
        ),
        "iv": b64(iv),
        "ciphertext": b64(encrypted[:-16]),
        "tag": b64(encrypted[-16:]),
    }
    if len(canonical(result)) > public["max_request_bytes"]:
        fail("Encrypted package exceeds advertised request limit")
    return result


class ReportTest:
    def __init__(self, client):
        self.client = client
        self.root = client.state.state_dir / "report-test"

    def call(self, method, path, body=None, expected=(200,)):
        response = self.client.api.call(
            method, ROOT + path, json_body=body, bearer=self.client.state.credential
        )
        if response.status not in expected:
            # Never echo arbitrary provider/problem detail or response bodies.
            code = (
                response.json().get("code", "request_failed") if response.body else "request_failed"
            )
            if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code):
                code = "request_failed"
            fail(f"Report request: HTTP {response.status} code={code}")
        return response.status, response.json()

    def save(self, record):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        secure_write_json(self.root / (identity(record["submission_id"]) + ".json"), record)

    def load(self, submission_id):
        try:
            record = json.loads(
                (self.root / (identity(submission_id) + ".json")).read_text("utf-8")
            )
        except (OSError, ValueError):
            fail("No readable encrypted local report for this submission")
        if (
            record["installation_id"] != self.client.state.installation_id
            or record["server"] != self.client.api.server_url
        ):
            fail("Local report belongs to a different installation or server")
        return record

    def status(self, submission_id):
        record = self.load(submission_id)
        _, data = self.call("GET", f"/report-submissions/{record['submission_id']}/status")
        self.validate_status(record, data)
        record["state"] = data["state"]
        record["expires_at"] = data["expires_at"]
        self.save(record)
        return {
            k: record[k] for k in ("submission_id", "report_id", "incident", "state", "expires_at")
        }

    def validate_status(self, record, data):
        if (
            data.get("submission_id") != record["submission_id"]
            or data.get("report_id") != record["report_id"]
            or data.get("state") not in ("READY", "EXPIRED")
            or not isinstance(data.get("expires_at"), str)
        ):
            fail("Unexpected report status response")

    def retry(self, submission_id):
        record = self.load(submission_id)
        status, data = self.call("POST", "/report-submissions", record["envelope"], (200, 201))
        self.validate_status(record, data)
        result = self.status(submission_id)
        if result["state"] != "READY":
            fail("Report is not READY; a link was not requested")
        return {**result, "http_status": status, "attachment_count": 3}

    def send(self, recipient, *, storm=False):
        recipient = identity(recipient)
        config = self.client.get_configuration()
        results = []
        # Persist the whole finalized batch before the first network attempt.
        # Connectivity failure leaves every finalized report recoverable by retry.
        for index in range(5 if storm else 1):
            incident, document, photos = synthetic(index)
            submission_id = str(uuid4())
            record = {
                "submission_id": submission_id,
                "report_id": document["report_id"],
                "recipient_personnel_id": recipient,
                "installation_id": config["installation_id"],
                "server": self.client.api.server_url,
                "incident": incident,
                "state": "PENDING",
                "envelope": envelope(config, recipient, document, photos, submission_id),
            }
            self.save(record)
            results.append(submission_id)
        uploaded, pending = [], []
        for submission_id in results:
            try:
                uploaded.append(self.retry(submission_id))
            except ClientError:
                pending.append(submission_id)
        if pending:
            fail(
                f"{len(pending)} finalized reports pending; encrypted queue retained. "
                "Use report-test retry --submission-id UUID "
                "for: " + ", ".join(pending)
            )
        return {"reports": uploaded}

    def link(self, recipient, *, request_id=None):
        recipient = identity(recipient)
        if request_id is not None:
            request_id = identity(request_id)
            try:
                saved = json.loads(
                    (self.root / "requests" / (request_id + ".json")).read_text("utf-8")
                )
            except (OSError, ValueError):
                fail("No stored link request to recover")
            if saved != {
                "transfer_request_id": request_id,
                "recipient_personnel_id": recipient,
                "installation_id": self.client.state.installation_id,
                "server": self.client.api.server_url,
            }:
                fail("Stored link request scope differs")
            _, data = self.call(
                "POST",
                "/report-transfers",
                {"transfer_request_id": request_id, "recipient_personnel_id": recipient},
                (200, 201),
            )
            return self.transfer_summary(data)
        pending = 0
        for path in sorted(self.root.glob("*.json")):
            record = self.load(path.stem)
            if record["recipient_personnel_id"] != recipient:
                continue
            try:
                if self.status(record["submission_id"])["state"] != "READY":
                    pending += 1
            except ClientError:
                pending += 1
        if pending:
            fail(
                f"{pending} finalized local reports are not confirmed READY; "
                "retry synchronization before requesting access. No link requested."
            )
        # An intentional invocation always uses a new UUID. Persist it so an
        # uncertain response can be recovered without a duplicate mail attempt.
        request_id = str(uuid4())
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "requests").mkdir(exist_ok=True, mode=0o700)
        secure_write_json(
            self.root / "requests" / (request_id + ".json"),
            {
                "transfer_request_id": request_id,
                "recipient_personnel_id": recipient,
                "installation_id": self.client.state.installation_id,
                "server": self.client.api.server_url,
            },
        )
        _, data = self.call(
            "POST",
            "/report-transfers",
            {"transfer_request_id": request_id, "recipient_personnel_id": recipient},
            (200, 201),
        )
        return self.transfer_summary(data)

    def transfer_summary(self, data):
        transfer_id = identity(data.get("transfer_id"))
        if data.get("status") not in ("PROCESSING", "SENT", "FAILED", "UNKNOWN", "REVOKED"):
            fail("Unexpected transfer status")
        return {"transfer_id": transfer_id, "status": data["status"]}

    def revoke(self, transfer_id):
        _, data = self.call("POST", f"/report-transfers/{identity(transfer_id)}/revoke")
        return self.transfer_summary(data)
