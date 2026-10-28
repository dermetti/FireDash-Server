"""ReportDocument v1: presentation structure, never a report-type field catalog."""

import hashlib
import re
import unicodedata
from datetime import date, datetime

from django.views.decorators.debug import sensitive_variables

from apps.report_transfer.contract import canonical, identifier, require


def shape(value, required, optional=()):
    require(
        isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional)
    )


def text(value, maximum=200, *, empty=False):
    require(isinstance(value, str) and (0 if empty else 1) <= len(value) <= maximum)
    # Plain text only. Reject markup, control characters, surrogates and bidi controls.
    require("<" not in value and ">" not in value)
    require(all(c in "\n\t" or not unicodedata.category(c).startswith("C") for c in value))


def key(value):
    require(isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value)))


def sequence(value, maximum, minimum=0):
    require(isinstance(value, list) and minimum <= len(value) <= maximum)


def unique(items, field):
    values = [item[field] for item in items]
    require(len(set(values)) == len(values))


def timestamp(value):
    text(value, 35)
    require(
        bool(
            re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})", value
            )
        )
    )
    try:
        require(datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None)
    except ValueError:
        require(False)


def typed_value(item):
    shape(item, {"type", "value"})
    kind, value = item["type"], item["value"]
    require(isinstance(kind, str))
    if kind == "text":
        text(value, 4000, empty=True)
    elif kind == "integer":
        require(type(value) is int and abs(value) <= 9007199254740991)
    elif kind == "decimal":
        # Decimal strings avoid binary floating-point and cross-language hash ambiguity.
        require(isinstance(value, str) and len(value) <= 32)
        require(bool(re.fullmatch(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?", value)))
    elif kind == "boolean":
        require(type(value) is bool)
    elif kind == "date":
        text(value, 10)
        try:
            require(date.fromisoformat(value).isoformat() == value)
        except ValueError:
            require(False)
    elif kind == "datetime":
        timestamp(value)
    else:
        require(False)


def fields(items):
    sequence(items, 100)
    for item in items:
        shape(item, {"key", "label", "value"})
        key(item["key"])
        text(item["label"])
        typed_value(item["value"])
    unique(items, "key")


@sensitive_variables()
def validate_document(document, *, report_id, attachment_hashes):
    """Validate without normalizing/mutating client-declared finalized content."""
    shape(
        document,
        {
            "report_id",
            "schema_version",
            "report_type",
            "title",
            "header",
            "sections",
            "attachments",
            "finalization",
        },
    )
    require(document["report_id"] == str(identifier(report_id)))
    require(type(document["schema_version"]) is int and document["schema_version"] == 1)
    text(document["report_type"], 80)
    text(document["title"], 200)
    fields(document["header"])
    sequence(document["attachments"], 32)
    ids = set()
    for attachment in document["attachments"]:
        shape(attachment, {"id", "title", "caption"})
        identifier(attachment["id"])
        require(attachment["id"] not in ids)
        ids.add(attachment["id"])
        text(attachment["title"])
        text(attachment["caption"], 2000, empty=True)
    require(ids == set(attachment_hashes))
    for digest in attachment_hashes.values():
        require(isinstance(digest, str) and bool(re.fullmatch("[0-9a-f]{64}", digest)))
    sequence(document["sections"], 50)
    keys = set()
    total_blocks = total_cells = 0
    for section in document["sections"]:
        shape(section, {"key", "title", "blocks"})
        key(section["key"])
        text(section["title"])
        require(section["key"] not in keys)
        keys.add(section["key"])
        sequence(section["blocks"], 50)
        total_blocks += len(section["blocks"])
        require(total_blocks <= 200)
        for block in section["blocks"]:
            require(isinstance(block, dict))
            key(block.get("key"))
            require(block["key"] not in keys)
            keys.add(block["key"])
            kind = block.get("kind")
            require(isinstance(kind, str))
            if kind == "text":
                shape(block, {"key", "kind", "text"})
                text(block["text"], 20000, empty=True)
            elif kind == "fields":
                shape(block, {"key", "kind", "fields"})
                fields(block["fields"])
            elif kind == "table":
                shape(block, {"key", "kind", "columns", "rows"})
                sequence(block["columns"], 8, 1)
                for column in block["columns"]:
                    shape(column, {"key", "label"})
                    key(column["key"])
                    text(column["label"], 80)
                unique(block["columns"], "key")
                sequence(block["rows"], 500)
                for row in block["rows"]:
                    sequence(row, len(block["columns"]), len(block["columns"]))
                    total_cells += len(row)
                    require(total_cells <= 4000)
                    for cell in row:
                        typed_value(cell)
            elif kind == "attachment_refs":
                shape(block, {"key", "kind", "attachment_ids"})
                sequence(block["attachment_ids"], 32)
                for identity in block["attachment_ids"]:
                    identifier(identity)
                    require(identity in ids)
                require(len(set(block["attachment_ids"])) == len(block["attachment_ids"]))
            else:
                require(False)
    finalization = document["finalization"]
    shape(finalization, {"author", "finalized_at"}, {"signature_attachment_id"})
    shape(finalization["author"], {"id", "display_name"})
    text(finalization["author"]["id"], 128)
    text(finalization["author"]["display_name"], 160)
    timestamp(finalization["finalized_at"])
    if "signature_attachment_id" in finalization:
        identifier(finalization["signature_attachment_id"])
        require(finalization["signature_attachment_id"] in ids)
    # Bound aggregate work even when this validator is called outside the transport.
    require(len(canonical(document)) <= 1024 * 1024)
    return document


@sensitive_variables()
def document_digest(document, attachment_hashes):
    require(isinstance(document, dict))
    validate_document(
        document, report_id=document.get("report_id"), attachment_hashes=attachment_hashes
    )
    value = {
        "profile": "firedash-report-document-sha256-v1",
        "document": document,
        "attachments": [
            {"id": k, "sha256": attachment_hashes[k]} for k in sorted(attachment_hashes)
        ],
    }
    return hashlib.sha256(canonical(value)).hexdigest()
