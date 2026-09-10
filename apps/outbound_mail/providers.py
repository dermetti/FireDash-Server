"""Provider-neutral API delivery registry.

Delivery mode is API; individual providers validate their typed configuration
behind this registry rather than becoming FireDash delivery modes.
"""

from __future__ import annotations

from collections.abc import Callable

from django.core.exceptions import ValidationError

BREVO = "BREVO"


def _validate_brevo(configuration) -> None:
    required = (
        configuration.brevo_sender_name,
        configuration.brevo_sender_email,
        configuration.brevo_api_key_encrypted,
    )
    if not all(required):
        raise ValidationError("Brevo API delivery configuration is incomplete.")


API_PROVIDER_REGISTRY: dict[str, Callable[[object], None]] = {BREVO: _validate_brevo}


def validate_api_provider_configuration(*, provider: str, configuration) -> None:
    validator = API_PROVIDER_REGISTRY.get(provider)
    if validator is None:
        raise ValidationError("Unsupported API mail provider.")
    validator(configuration)
