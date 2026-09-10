"""Transport-free canonical contract for outbound email providers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

_MIME_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$")


def _required_text(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError({field: "This field is required."})
    if value != value.strip():
        raise ValidationError({field: "Leading or trailing whitespace is not permitted."})
    return value


@dataclass(frozen=True)
class MailAddress:
    display_name: str
    email: str

    def __post_init__(self) -> None:
        _required_text(self.display_name, field="display_name")
        try:
            validate_email(self.email)
        except ValidationError:
            raise ValidationError({"email": "A valid email address is required."}) from None


@dataclass(frozen=True, repr=False)
class MailAttachment:
    filename: str
    mime_type: str
    content: bytes

    def __post_init__(self) -> None:
        filename = _required_text(self.filename, field="filename")
        if "/" in filename or "\\" in filename or filename in {".", ".."}:
            raise ValidationError({"filename": "Attachment filename must not be a path."})
        if not isinstance(self.mime_type, str) or not _MIME_TYPE_RE.fullmatch(self.mime_type):
            raise ValidationError({"mime_type": "A valid MIME type is required."})
        if not isinstance(self.content, bytes):
            raise ValidationError({"content": "Attachment content must be bytes."})

    def __repr__(self) -> str:
        return (
            f"MailAttachment(filename={self.filename!r}, mime_type={self.mime_type!r}, "
            f"content=<redacted {len(self.content)} bytes>)"
        )

    def __str__(self) -> str:
        return f"MailAttachment({self.filename!r}, <redacted {len(self.content)} bytes>)"


@dataclass(frozen=True, repr=False)
class OutboundMessage:
    sender: MailAddress
    recipient: MailAddress
    subject: str
    body: str
    attachments: tuple[MailAttachment, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.sender, MailAddress) or not isinstance(self.recipient, MailAddress):
            raise ValidationError("Sender and exactly one recipient are required.")
        _required_text(self.subject, field="subject")
        _required_text(self.body, field="body")
        if not isinstance(self.attachments, tuple) or not all(
            isinstance(attachment, MailAttachment) for attachment in self.attachments
        ):
            raise ValidationError(
                {"attachments": "Attachments must be an immutable attachment tuple."}
            )

    def __repr__(self) -> str:
        return (
            f"OutboundMessage(sender={self.sender!r}, recipient={self.recipient!r}, "
            f"subject={self.subject!r}, body=<redacted {len(self.body)} chars>, "
            f"attachments={self.attachments!r})"
        )

    def __str__(self) -> str:
        return f"OutboundMessage(to={self.recipient.email!r}, attachments={len(self.attachments)})"


@dataclass(frozen=True)
class MailSendResult:
    provider: str
    provider_message_id: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.provider, field="provider")
        if self.provider_message_id is not None and not isinstance(self.provider_message_id, str):
            raise ValidationError(
                {"provider_message_id": "Provider reference must be text or absent."}
            )


class MailProviderError(Exception):
    """Sanitized FireDash-level provider failure with no vendor response data."""

    code = "provider_failure"
    detail = "Mail provider operation failed."

    def __init__(self) -> None:
        super().__init__(self.detail)


class ProviderUnavailableError(MailProviderError):
    code = "provider_unavailable"
    detail = "Mail provider is temporarily unavailable."


class MessageRejectedError(MailProviderError):
    code = "message_rejected"
    detail = "Mail message was rejected."


class ProviderConfigurationError(MailProviderError):
    code = "provider_configuration"

    _DETAILS = {
        "not_configured": "Mail provider is not configured.",
        "unsupported": "Unsupported mail provider.",
        "identity_mismatch": "Mail provider identity does not match registration.",
        "already_registered": "Mail provider is already registered.",
        "unregistered": "Mail provider is not registered.",
    }

    def __init__(self, *, reason: str = "not_configured") -> None:
        self.detail = self._DETAILS.get(reason, self._DETAILS["not_configured"])
        super().__init__()


@runtime_checkable
class MailProvider(Protocol):
    """One provider-neutral synchronous delivery contract for future adapters."""

    provider_id: str

    def send(self, message: OutboundMessage) -> MailSendResult: ...
