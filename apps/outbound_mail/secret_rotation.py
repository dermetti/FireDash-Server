"""Deliberate rotation support for the outbound-mail secret consumers only."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from django.db import transaction

from apps.application_secrets.crypto import (
    ApplicationSecretError,
    application_secret_key_version,
    configured_application_secret_cipher,
)
from apps.outbound_mail.models import DepartmentMailConfiguration, SystemMailConfiguration
from apps.outbound_mail.services import (
    BREVO_API_KEY_CONTEXT,
    SMTP_PASSWORD_CONTEXT,
    department_smtp_password_context,
)

_MALFORMED_VERSION = "malformed"


class OutboundMailSecretRotationError(Exception):
    """Safe operator error: no envelope, plaintext, or key material is retained."""

    def __init__(self, *, reference: str, code: str) -> None:
        self.reference = reference
        self.code = code
        super().__init__(f"Outbound-mail secret rotation failed for {reference} ({code}).")


@dataclass(frozen=True)
class OutboundMailSecretRotationStatus:
    active_version: str
    version_counts: dict[str, int]

    @property
    def all_live_credentials_active(self) -> bool:
        return not any(
            version != self.active_version and count
            for version, count in self.version_counts.items()
        )

    def can_retire(self, version: str) -> bool:
        """Fail closed when malformed envelopes make usage indeterminate."""
        return not self.version_counts.get(version, 0) and not self.version_counts.get(
            _MALFORMED_VERSION, 0
        )


@dataclass(frozen=True)
class OutboundMailSecretRotationResult:
    active_version: str
    rotated_count: int
    skipped_active_count: int
    skipped_empty_count: int


@dataclass(frozen=True)
class _SecretReference:
    kind: str
    object_id: str
    field: str
    context: str

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.object_id}"


def _live_secret_references() -> list[_SecretReference]:
    """The explicit, extensible inventory of persisted outbound-mail secrets."""
    references: list[_SecretReference] = []
    system = SystemMailConfiguration.objects.filter(singleton=True).first()
    if system is not None:
        references.extend(
            (
                _SecretReference(
                    "system_api_key",
                    str(system.pk),
                    "brevo_api_key_encrypted",
                    BREVO_API_KEY_CONTEXT,
                ),
                _SecretReference(
                    "system_smtp_password",
                    str(system.pk),
                    "smtp_password_encrypted",
                    SMTP_PASSWORD_CONTEXT,
                ),
            )
        )
    configurations = DepartmentMailConfiguration.objects.select_related("department").order_by("pk")
    for department_configuration in configurations:
        references.append(
            _SecretReference(
                "department_smtp_password",
                str(department_configuration.pk),
                "smtp_password_encrypted",
                department_smtp_password_context(department=department_configuration.department),
            )
        )
    return references


def outbound_mail_secret_rotation_status() -> OutboundMailSecretRotationStatus:
    """Inspect only envelope metadata; this never decrypts or writes credentials."""
    cipher = configured_application_secret_cipher()
    counts: Counter[str] = Counter()
    for reference in _live_secret_references():
        if not (value := _secret_value(reference)):
            continue
        try:
            counts[application_secret_key_version(value)] += 1
        except ApplicationSecretError:
            counts[_MALFORMED_VERSION] += 1
    return OutboundMailSecretRotationStatus(
        active_version=cipher.active_version,
        version_counts=dict(sorted(counts.items())),
    )


def _secret_value(reference: _SecretReference) -> str:
    if reference.kind.startswith("system_"):
        configuration = SystemMailConfiguration.objects.filter(pk=reference.object_id).first()
    else:
        configuration = DepartmentMailConfiguration.objects.filter(pk=reference.object_id).first()
    return "" if configuration is None else getattr(configuration, reference.field)


def _locked_secret_value(reference: _SecretReference):
    if reference.kind.startswith("system_"):
        configuration = (
            SystemMailConfiguration.objects.select_for_update()
            .filter(pk=reference.object_id)
            .first()
        )
    else:
        configuration = (
            DepartmentMailConfiguration.objects.select_for_update()
            .filter(pk=reference.object_id)
            .first()
        )
    return configuration, "" if configuration is None else getattr(configuration, reference.field)


def rotate_outbound_mail_secrets() -> OutboundMailSecretRotationResult:
    """Re-encrypt one row at a time, allowing safe reruns after interruption/error."""
    cipher = configured_application_secret_cipher()
    rotated = skipped_active = skipped_empty = 0
    for reference in _live_secret_references():
        with transaction.atomic():
            configuration, value = _locked_secret_value(reference)
            if configuration is None or not value:
                skipped_empty += 1
                continue
            try:
                if application_secret_key_version(value) == cipher.active_version:
                    skipped_active += 1
                    continue
                plaintext = cipher.decrypt(value, context=reference.context)
                replacement = cipher.encrypt(plaintext, context=reference.context).serialized
            except ApplicationSecretError as error:
                raise OutboundMailSecretRotationError(
                    reference=reference.label, code=error.code
                ) from None
            setattr(configuration, reference.field, replacement)
            # Do not update timestamps, verification state, mode, sender fields, or
            # audit credential-replacement semantics: plaintext is unchanged.
            configuration.save(update_fields=(reference.field,))
            rotated += 1
    return OutboundMailSecretRotationResult(
        active_version=cipher.active_version,
        rotated_count=rotated,
        skipped_active_count=skipped_active,
        skipped_empty_count=skipped_empty,
    )
