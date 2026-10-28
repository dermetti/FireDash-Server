"""Report-only RFC 7516 flattened JWE: RSA-OAEP-256 + A256GCM."""

import base64
import binascii
import hashlib
import io
import json
import math
import re
import warnings
from pathlib import Path
from uuid import UUID

from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.views.decorators.debug import sensitive_variables
from PIL import Image


class TransferError(Exception):
    def __init__(self, code="invalid_package", status=400):
        self.code = code
        self.status = status
        super().__init__(code)


def require(condition):
    if not condition:
        raise TransferError()


@sensitive_variables()
def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def _float(value):
    number = float(value)
    require(math.isfinite(number))
    return number


@sensitive_variables()
def parse_json(raw):
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_float=_float,
            parse_constant=lambda _: require(False),
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise TransferError() from None


def encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode(value):
    require(isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]+", value)))
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        require(encode(raw) == value)
        return raw
    except (ValueError, binascii.Error):
        raise TransferError() from None


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def identifier(value):
    try:
        require(isinstance(value, str) and str(UUID(value)) == value)
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise TransferError() from None


@sensitive_variables()
def private_keys():
    """Dedicated deployment credential; never use publication/application keys."""
    try:
        document = parse_json(Path(settings.REPORT_TRANSFER_KEYRING_PATH).read_bytes())
        require(isinstance(document, dict) and set(document) == {"keys"})
        keys = {}
        for version, pem in document["keys"].items():
            require(bool(re.fullmatch(r"[A-Za-z0-9._-]{1,64}", version)))
            key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
            require(isinstance(key, rsa.RSAPrivateKey) and key.key_size >= 3072)
            keys[version] = key
        require(settings.REPORT_TRANSFER_ACTIVE_KEY in keys)
        return keys
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        UnsupportedAlgorithm,
        TransferError,
    ):
        raise TransferError("report_keys_unavailable", 503) from None


def configuration():
    try:
        key = private_keys()[settings.REPORT_TRANSFER_ACTIVE_KEY].public_key()
    except TransferError:
        return {"version": 1, "enabled": False}
    return {
        "version": 1,
        "enabled": True,
        "kid": settings.REPORT_TRANSFER_ACTIVE_KEY,
        "alg": "RSA-OAEP-256",
        "enc": "A256GCM",
        "public_key_pem": key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("ascii"),
        "max_request_bytes": settings.REPORT_TRANSFER_MAX_REQUEST_BYTES,
        "max_json_bytes": settings.REPORT_TRANSFER_MAX_JSON_BYTES,
        "max_jpeg_bytes": settings.REPORT_TRANSFER_MAX_JPEG_BYTES,
        "max_attachments": settings.REPORT_TRANSFER_MAX_ATTACHMENTS,
        "max_image_pixels": settings.REPORT_TRANSFER_MAX_IMAGE_PIXELS,
    }


HEADER_FIELDS = {
    "typ",
    "alg",
    "enc",
    "kid",
    "package_version",
    "schema_version",
    "submission_id",
    "report_id",
    "recipient_personnel_id",
    "installation_id",
    "department_id",
    "station_id",
}
JWE_FIELDS = {"protected", "encrypted_key", "iv", "ciphertext", "tag"}


def inspect_envelope(envelope):
    require(isinstance(envelope, dict) and set(envelope) == JWE_FIELDS)
    require(all(isinstance(v, str) for v in envelope.values()))
    require(len(envelope["protected"]) <= 4096)
    header = parse_json(decode(envelope["protected"]))
    require(isinstance(header, dict) and set(header) == HEADER_FIELDS)
    require(
        header["typ"] == "firedash-report+jwe"
        and header["alg"] == "RSA-OAEP-256"
        and header["enc"] == "A256GCM"
    )
    require(type(header["package_version"]) is int and header["package_version"] == 1)
    require(header["schema_version"] == "transport-json-v1")
    require(
        isinstance(header["kid"], str)
        and bool(re.fullmatch(r"[A-Za-z0-9._-]{1,64}", header["kid"]))
    )
    for field in (
        "submission_id",
        "report_id",
        "recipient_personnel_id",
        "installation_id",
        "department_id",
        "station_id",
    ):
        identifier(header[field])
    return header


@sensitive_variables()
def decrypt_and_validate(envelope, header):
    key = private_keys().get(header["kid"])
    if key is None:
        raise TransferError("unknown_report_key", 400)
    wrapped, iv, ciphertext, tag = (
        decode(envelope[f]) for f in ("encrypted_key", "iv", "ciphertext", "tag")
    )
    require(len(wrapped) == key.key_size // 8 and len(iv) == 12 and len(tag) == 16)
    try:
        cek = key.decrypt(
            wrapped,
            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        require(len(cek) == 32)
        plaintext = AESGCM(cek).decrypt(iv, ciphertext + tag, envelope["protected"].encode("ascii"))
    except (ValueError, InvalidTag):
        raise TransferError() from None
    package = parse_json(plaintext)
    require(isinstance(package, dict) and set(package) == {"report", "attachments"})
    report = _content(
        package["report"],
        settings.REPORT_TRANSFER_MAX_JSON_BYTES,
        {"data", "size", "sha256", "attachment_ids"},
    )
    require(isinstance(parse_json(report), dict))
    attachments = package["attachments"]
    require(
        isinstance(attachments, list)
        and len(attachments) <= settings.REPORT_TRANSFER_MAX_ATTACHMENTS
    )
    references = package["report"]["attachment_ids"]
    require(isinstance(references, list) and len(references) == len(attachments))
    seen = set()
    for item in attachments:
        raw = _content(
            item,
            settings.REPORT_TRANSFER_MAX_JPEG_BYTES,
            {"id", "report_id", "media_type", "size", "sha256", "data"},
        )
        identity = identifier(item["id"])
        require(
            identity not in seen
            and item["report_id"] == header["report_id"]
            and item["media_type"] == "image/jpeg"
        )
        seen.add(identity)
        _jpeg(raw)
    require(len(set(identifier(v) for v in references)) == len(references))
    require(set(identifier(v) for v in references) == seen)


@sensitive_variables()
def _content(item, limit, fields):
    require(isinstance(item, dict) and set(item) == fields)
    require(type(item["size"]) is int and 0 < item["size"] <= limit)
    require(isinstance(item["data"], str) and len(item["data"]) <= (limit + 2) // 3 * 4)
    raw = decode(item["data"])
    require(len(raw) == item["size"] and hashlib.sha256(raw).hexdigest() == item["sha256"])
    return raw


@sensitive_variables()
def _jpeg(raw):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                require(
                    image.format == "JPEG"
                    and image.width * image.height <= settings.REPORT_TRANSFER_MAX_IMAGE_PIXELS
                )
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
        require(raw.startswith(b"\xff\xd8") and raw.endswith(b"\xff\xd9"))
    except (
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise TransferError() from None
