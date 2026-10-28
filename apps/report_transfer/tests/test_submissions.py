import base64
import copy
import hashlib
import hmac
import io
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections
from django.test import Client
from django.utils import timezone
from PIL import Image

from apps.assignments.models import TabletVehicleAssignment
from apps.organizations.models import Department, Station, Vehicle
from apps.outbound_mail.models import DepartmentRecipientPolicy
from apps.outbound_mail.tests.test_report_delivery_requests import request_context  # noqa: F401
from apps.publications.manifests import canonical_manifest_payload
from apps.publications.worker_grants import process_next_signed_manifest
from apps.report_transfer import storage
from apps.report_transfer.contract import configuration
from apps.report_transfer.models import ReportSubmission
from apps.report_transfer.services import cleanup
from apps.tablets.models import AppInstallation, Tablet

URL = "/api/v1/tablet/report-submissions"


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture(scope="session")
def report_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


@pytest.fixture
def context(request_context, tmp_path, settings, report_key):  # noqa: F811
    installation, person, credential = request_context
    installation.tablet.status = Tablet.Status.ACTIVE
    installation.tablet.save()
    department = installation.tablet.department
    station = Station.objects.create(department=department, name="Station", short_code="S")
    vehicle = Vehicle.objects.create(department=department, station=station, display_name="Engine")
    TabletVehicleAssignment.objects.create(
        tablet=installation.tablet,
        vehicle=vehicle,
        valid_from=timezone.now() - timedelta(minutes=1),
        created_by=department.created_by,
    )
    pem = report_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    keyring = tmp_path / "keys.json"
    keyring.write_text(json.dumps({"keys": {"test-1": pem}}))
    settings.REPORT_TRANSFER_KEYRING_PATH = keyring
    settings.REPORT_TRANSFER_ACTIVE_KEY = "test-1"
    settings.REPORT_TRANSFER_ROOT = tmp_path / "spool"
    client = Client(HTTP_AUTHORIZATION=f"Bearer {credential}")
    header = {
        "typ": "firedash-report+jwe",
        "alg": "RSA-OAEP-256",
        "enc": "A256GCM",
        "kid": "test-1",
        "package_version": 1,
        "schema_version": "transport-json-v1",
        "submission_id": str(uuid4()),
        "report_id": str(uuid4()),
        "recipient_personnel_id": str(person.pk),
        "installation_id": str(installation.pk),
        "department_id": str(department.pk),
        "station_id": str(station.pk),
    }
    return installation, person, client, header


def content(raw):
    return {"data": b64(raw), "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def package(header, jpeg=True):
    attachments = []
    if jpeg:
        image = io.BytesIO()
        Image.new("RGB", (4, 4), "red").save(image, format="JPEG")
        attachments.append(
            {
                **content(image.getvalue()),
                "id": str(uuid4()),
                "report_id": header["report_id"],
                "media_type": "image/jpeg",
            }
        )
    return {
        "report": {**content(b"{}"), "attachment_ids": [a["id"] for a in attachments]},
        "attachments": attachments,
    }


def encrypt(report_key, header, payload):
    # Independent client implementation of RFC 7516, not the server's decryptor.
    protected = b64(json.dumps(header, separators=(",", ":")).encode())
    cek, iv = os.urandom(32), os.urandom(12)
    ciphertext = AESGCM(cek).encrypt(iv, json.dumps(payload).encode(), protected.encode("ascii"))
    wrapped = report_key.public_key().encrypt(
        cek, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    )
    return {
        "protected": protected,
        "encrypted_key": b64(wrapped),
        "iv": b64(iv),
        "ciphertext": b64(ciphertext[:-16]),
        "tag": b64(ciphertext[-16:]),
    }


def post(client, envelope):
    return client.post(URL, data=json.dumps(envelope), content_type="application/json")


@pytest.mark.parametrize("jpeg", [False, True])
def test_accept_replay_status_and_ciphertext_only(context, report_key, jpeg, caplog):
    caplog.set_level(logging.DEBUG)
    installation, person, client, header = context
    body = package(header, jpeg)
    body["report"].update(content(b'{"opaque":"report-marker-not-for-logs"}'))
    envelope = encrypt(report_key, header, body)
    first = post(client, envelope)
    assert first.status_code == 201, first.content
    assert first.json()["state"] == "READY"
    replay = post(client, dict(reversed(list(envelope.items()))))
    assert replay.status_code == 200 and replay.json() == first.json()
    status = client.get(f"{URL}/{header['submission_id']}/status")
    assert status.status_code == 200 and status.json() == first.json()
    assert ReportSubmission.objects.count() == 1
    row = ReportSubmission.objects.get()
    raw = storage.payload_path(row.payload_key).read_bytes()
    assert json.loads(raw) == envelope
    assert b"report-marker" not in raw
    assert "report-marker" not in caplog.text and envelope["encrypted_key"] not in caplog.text
    assert "PRIVATE KEY" not in caplog.text
    assert "commander@example.test" not in json.dumps(first.json())


def test_rejected_package_never_logs_content_or_keys(context, report_key, caplog):
    caplog.set_level(logging.DEBUG)
    _, _, client, header = context
    body = package(header)
    body["report"].update(content(b'{"opaque":"sensitive-rejected-report-marker"}'))
    body["report"]["sha256"] = "0" * 64
    envelope = encrypt(report_key, header, body)
    assert post(client, envelope).status_code == 400
    assert "sensitive-rejected-report-marker" not in caplog.text
    assert envelope["ciphertext"] not in caplog.text
    assert envelope["encrypted_key"] not in caplog.text
    assert "PRIVATE KEY" not in caplog.text


def test_conflicting_submission_or_report_identity(context, report_key):
    _, _, client, header = context
    first = encrypt(report_key, header, package(header))
    assert post(client, first).status_code == 201
    assert post(client, encrypt(report_key, header, package(header))).status_code == 409
    header["submission_id"] = str(uuid4())
    assert post(client, encrypt(report_key, header, package(header))).status_code == 409
    assert ReportSubmission.objects.count() == 1


@pytest.mark.parametrize("field", ["installation_id", "department_id", "station_id"])
def test_foreign_scope_rejected(context, report_key, field):
    _, _, client, header = context
    header[field] = str(uuid4())
    assert post(client, encrypt(report_key, header, package(header))).status_code == 403
    assert not ReportSubmission.objects.exists()


@pytest.mark.parametrize("change", ["revoked", "lease", "inactive", "assignment", "station"])
def test_authorization_revocation_blocks_upload_and_status(context, report_key, change):
    installation, _, client, header = context
    envelope = encrypt(report_key, header, package(header))
    assert post(client, envelope).status_code == 201
    if change == "revoked":
        AppInstallation.objects.filter(pk=installation.pk).update(status="REVOKED")
    elif change == "lease":
        AppInstallation.objects.filter(pk=installation.pk).update(
            authorization_valid_until=timezone.now()
        )
    elif change == "inactive":
        Tablet.objects.filter(pk=installation.tablet_id).update(status="INACTIVE")
    elif change == "assignment":
        TabletVehicleAssignment.objects.filter(tablet=installation.tablet).update(
            ended_at=timezone.now()
        )
    else:
        Station.objects.filter(pk=header["station_id"]).update(active=False)
    assert post(client, envelope).status_code == 403
    assert client.get(f"{URL}/{header['submission_id']}/status").status_code == 403


def test_invalid_credential_and_nondisclosing_foreign_status(context, report_key, settings):
    installation, _, client, header = context
    envelope = encrypt(report_key, header, package(header))
    assert post(Client(HTTP_AUTHORIZATION="Bearer invalid"), envelope).status_code == 403
    assert post(client, envelope).status_code == 201
    # A second valid installation with the same department/assignment cannot see it.
    foreign = AppInstallation.objects.get(pk=installation.pk)
    foreign.pk = uuid4()
    foreign.installation_uuid = uuid4()
    foreign.tablet = Tablet.objects.create(
        department=installation.tablet.department, display_name="Second tablet", status="ACTIVE"
    )
    foreign.credential_hash = hmac.new(
        settings.SECRET_KEY.encode(), b"second-credential", hashlib.sha256
    ).hexdigest()
    foreign.save()
    assignment = TabletVehicleAssignment.objects.get(tablet=installation.tablet)
    TabletVehicleAssignment.objects.create(
        tablet=foreign.tablet,
        vehicle=assignment.vehicle,
        valid_from=assignment.valid_from,
        created_by=assignment.created_by,
    )
    second = Client(HTTP_AUTHORIZATION="Bearer second-credential")
    assert second.get(f"{URL}/{header['submission_id']}/status").status_code == 404
    assert second.get(f"{URL}/{uuid4()}/status").status_code == 404
    assert post(second, envelope).status_code == 403
    assert client.get(f"{URL}/{uuid4()}/status").status_code == 404


@pytest.mark.parametrize("change", ["missing", "unverified", "ineligible", "foreign", "policy"])
def test_recipient_authorization(context, report_key, change):
    installation, person, client, header = context
    if change == "missing":
        person.incident_commander_email = None
    elif change == "unverified":
        person.email_verified_at = None
    elif change == "ineligible":
        person.incident_commander_eligible = False
    elif change == "foreign":
        person.department = Department.objects.create(
            name="Other", short_code="O", created_by=installation.tablet.department.created_by
        )
    else:
        DepartmentRecipientPolicy.objects.create(
            department=installation.tablet.department, restriction_enabled=True
        )
    person.save()
    assert post(client, encrypt(report_key, header, package(header))).status_code == 403
    assert not ReportSubmission.objects.exists()


@pytest.mark.parametrize(
    "fault",
    [
        "hash",
        "size",
        "not_jpeg",
        "truncated_jpeg",
        "missing",
        "duplicate",
        "wrong_report",
        "unknown_reference",
        "json",
        "extra",
        "jpeg_type",
        "duplicate_json",
        "infinite_json",
        "bool_size",
    ],
)
def test_invalid_content(context, report_key, fault):
    _, _, client, header = context
    body = package(header)
    item = body["attachments"][0]
    if fault == "hash":
        item["sha256"] = "0" * 64
    elif fault == "size":
        item["size"] += 1
    elif fault in ("not_jpeg", "truncated_jpeg"):
        raw = b"not an image" if fault == "not_jpeg" else b"\xff\xd8\xff\xd9"
        item.update(content(raw))
    elif fault == "missing":
        body["attachments"] = []
    elif fault == "duplicate":
        body["attachments"].append(copy.deepcopy(item))
        body["report"]["attachment_ids"].append(item["id"])
    elif fault == "wrong_report":
        item["report_id"] = str(uuid4())
    elif fault == "unknown_reference":
        body["report"]["attachment_ids"] = [str(uuid4())]
    elif fault == "json":
        body["report"].update(content(b"[]"))
    elif fault == "extra":
        body["recipient_email"] = "attacker@example.test"
    elif fault == "jpeg_type":
        item["media_type"] = "image/png"
    elif fault == "duplicate_json":
        body["report"].update(content(b'{"x":1,"x":2}'))
    elif fault == "infinite_json":
        body["report"].update(content(b'{"x":1e999}'))
    else:
        item["size"] = True
    response = post(client, encrypt(report_key, header, body))
    assert response.status_code == 400, response.content
    assert not ReportSubmission.objects.exists()
    assert list(storage.root().glob("*.jwe")) == []


@pytest.mark.parametrize("field", ["tag", "iv", "ciphertext", "encrypted_key"])
def test_invalid_encryption(context, report_key, field):
    _, _, client, header = context
    envelope = encrypt(report_key, header, package(header))
    envelope[field] = b64(b"invalid")
    assert post(client, envelope).status_code == 400
    assert not ReportSubmission.objects.exists()


@pytest.mark.parametrize(
    "limit", ["JSON_BYTES", "JPEG_BYTES", "ATTACHMENTS", "IMAGE_PIXELS", "REQUEST_BYTES"]
)
def test_configured_limits(context, report_key, settings, limit):
    _, _, client, header = context
    setattr(settings, f"REPORT_TRANSFER_MAX_{limit}", 1 if limit != "ATTACHMENTS" else 0)
    response = post(client, encrypt(report_key, header, package(header)))
    assert response.status_code == (413 if limit == "REQUEST_BYTES" else 400)
    assert not ReportSubmission.objects.exists()


def test_interruption_and_database_failure_leave_no_accepted_state(
    context, report_key, monkeypatch
):
    _, _, client, header = context
    envelope = encrypt(report_key, header, package(header))
    truncated = json.dumps(envelope)[:-20]
    assert client.post(URL, data=truncated, content_type="application/json").status_code == 400
    with monkeypatch.context() as patch:

        def fail(**kwargs):
            raise OSError("simulated persistence failure")

        patch.setattr(ReportSubmission.objects, "create", fail)
        assert post(client, envelope).status_code == 503
    assert not ReportSubmission.objects.exists()
    assert list(storage.root().glob("*.jwe")) == []
    assert post(client, envelope).status_code == 201


def test_cleanup_retry_orphans_and_expired_replay(context, report_key, monkeypatch):
    _, _, client, header = context
    envelope = encrypt(report_key, header, package(header))
    assert post(client, envelope).status_code == 201
    ReportSubmission.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert client.get(f"{URL}/{header['submission_id']}/status").json()["state"] == "EXPIRED"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "remove_payload", lambda _: (_ for _ in ()).throw(OSError()))
        with pytest.raises(CommandError):
            call_command("expire_report_submissions")
    assert ReportSubmission.objects.get().payload_key
    orphan = storage.write_payload(b"incomplete ciphertext")
    call_command("expire_report_submissions")
    assert list(storage.root().glob("*.jwe")) == []
    assert not storage.payload_path(orphan).exists()
    row = ReportSubmission.objects.get()
    assert row.state == "EXPIRED" and not row.payload_key
    assert cleanup() == {"expired": 0, "orphans": 0, "failed": 0}
    assert post(client, envelope).status_code == 200
    assert post(client, envelope).json()["state"] == "EXPIRED"


def test_configuration_key_rotation_and_unavailability(context, report_key, settings, tmp_path):
    _, _, client, header = context
    response = client.get("/api/v1/tablet/configuration")
    config = response.json()["report_transfer"]
    assert config == configuration() and config["enabled"] and config["kid"] == "test-1"
    assert "PRIVATE KEY" not in response.content.decode()
    assert (
        config["public_key_pem"]
        == report_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    settings.REPORT_TRANSFER_KEYRING_PATH = tmp_path / "missing"
    assert configuration() == {"version": 1, "enabled": False}
    assert post(client, encrypt(report_key, header, package(header))).status_code == 503


def test_key_rotation_updates_signed_configuration_and_retires_old_key(
    context, report_key, settings, tmp_path
):
    _, _, client, header = context
    signing_path = tmp_path / "signing"
    signing_path.write_bytes(b"s" * 32)
    settings.PUBLICATION_SIGNING_KEY_CREDENTIAL_PATH = signing_path
    signing_public = Ed25519PrivateKey.from_private_bytes(b"s" * 32).public_key()

    def manifest():
        assert client.get("/api/v1/tablet/manifest").status_code == 202
        assert process_next_signed_manifest() is not None
        response = client.get("/api/v1/tablet/manifest")
        assert response.status_code == 200
        payload = response.json()
        signing_public.verify(
            base64.b64decode(payload.pop("signature")), canonical_manifest_payload(payload)
        )
        assert payload["configuration"] == client.get("/api/v1/tablet/configuration").json()
        return response

    before = manifest()
    old_envelope = encrypt(report_key, header, package(header))
    second_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    ring = json.loads(settings.REPORT_TRANSFER_KEYRING_PATH.read_text())
    ring["keys"]["test-2"] = second_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    settings.REPORT_TRANSFER_KEYRING_PATH.write_text(json.dumps(ring))
    settings.REPORT_TRANSFER_ACTIVE_KEY = "test-2"
    settings.REPORT_TRANSFER_MAX_JSON_BYTES = 12345
    after = manifest()
    assert before["ETag"] != after["ETag"]
    assert after.json()["configuration"]["report_transfer"]["kid"] == "test-2"
    assert after.json()["configuration"]["report_transfer"]["max_json_bytes"] == 12345
    assert post(client, old_envelope).status_code == 201  # controlled overlap
    del ring["keys"]["test-1"]
    settings.REPORT_TRANSFER_KEYRING_PATH.write_text(json.dumps(ring))
    assert post(client, old_envelope).status_code == 200  # replay needs no retired private key
    header["submission_id"], header["report_id"] = str(uuid4()), str(uuid4())
    assert post(client, encrypt(report_key, header, package(header))).status_code == 400
    header["kid"] = "test-2"
    assert post(client, encrypt(second_key, header, package(header))).status_code == 201


@pytest.mark.parametrize("fault", ["schema", "uuid", "alg", "extra", "protected", "key"])
def test_invalid_envelope_metadata(context, report_key, fault):
    _, _, client, header = context
    if fault == "schema":
        header["schema_version"] = "future-business-schema"
    elif fault == "uuid":
        header["report_id"] = "not-a-uuid"
    elif fault == "alg":
        header["alg"] = "RSA1_5"
    elif fault == "extra":
        header["recipient_email"] = "attacker@example.test"
    elif fault == "key":
        header["kid"] = "unknown"
    envelope = encrypt(report_key, header, package(header))
    if fault == "protected":
        envelope["protected"] += "="
    assert post(client, envelope).status_code == 400
    assert not ReportSubmission.objects.exists()


def test_partial_file_write_and_missing_file_cleanup(context, report_key, monkeypatch):
    _, _, client, header = context
    envelope = encrypt(report_key, header, package(header))
    with monkeypatch.context() as patch:

        def fail(*args):
            raise OSError("interrupted write")

        patch.setattr(storage.os, "fsync", fail)
        assert post(client, envelope).status_code == 503
    assert not ReportSubmission.objects.exists()
    assert list(storage.root().glob("*.jwe")) == []
    assert post(client, envelope).status_code == 201
    row = ReportSubmission.objects.get()
    storage.remove_payload(row.payload_key)  # crash after unlink, before DB update
    ReportSubmission.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert cleanup() == {"expired": 1, "orphans": 0, "failed": 0}


def test_public_storage_overlap_fails_closed(context, report_key, settings):
    _, _, client, header = context
    settings.REPORT_TRANSFER_ROOT = settings.PUBLICATION_ARTIFACT_ROOT
    assert post(client, encrypt(report_key, header, package(header))).status_code == 503
    assert not ReportSubmission.objects.exists()


def test_recipient_revocation_also_blocks_replay(context, report_key):
    _, person, client, header = context
    envelope = encrypt(report_key, header, package(header))
    assert post(client, envelope).status_code == 201
    person.email_verified_at = None
    person.save()
    assert post(client, envelope).status_code == 403
    assert ReportSubmission.objects.count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("conflict", [False, True])
def test_concurrent_duplicate_requests(context, report_key, conflict):
    _, _, client, header = context
    envelope = encrypt(report_key, header, package(header))
    other = encrypt(report_key, header, package(header)) if conflict else envelope
    barrier = Barrier(2)

    def run(request_envelope):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            response = post(Client(**client.defaults), request_envelope)
            return response.status_code, response.json()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, [envelope, other]))
    assert sorted(code for code, _ in outcomes) == ([201, 409] if conflict else [200, 201])
    if not conflict:
        assert outcomes[0][1] == outcomes[1][1]
    assert ReportSubmission.objects.count() == 1
    assert len(list(storage.root().glob("*.jwe"))) == 1
