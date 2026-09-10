"""Provider-neutral API delivery registry.

Delivery mode is API; individual providers validate their typed configuration
behind this registry rather than becoming FireDash delivery modes.
"""

from __future__ import annotations

from collections.abc import Callable

from django.core.exceptions import ValidationError

from apps.outbound_mail.runtime import MailProvider, ProviderConfigurationError

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
RUNTIME_PROVIDER_REGISTRY: dict[str, MailProvider] = {}


def validate_api_provider_configuration(*, provider: str, configuration) -> None:
    validator = API_PROVIDER_REGISTRY.get(provider)
    if validator is None:
        raise ValidationError("Unsupported API mail provider.")
    validator(configuration)


def register_runtime_provider(*, provider: str, implementation: MailProvider) -> None:
    """Register an adapter only for a configured API-provider identity."""
    if provider not in API_PROVIDER_REGISTRY:
        raise ProviderConfigurationError(reason="unsupported")
    if implementation.provider_id != provider:
        raise ProviderConfigurationError(reason="identity_mismatch")
    if provider in RUNTIME_PROVIDER_REGISTRY:
        raise ProviderConfigurationError(reason="already_registered")
    RUNTIME_PROVIDER_REGISTRY[provider] = implementation


def resolve_runtime_provider(*, provider: str) -> MailProvider:
    """Resolve a registered adapter without vendor-specific branches in callers."""
    if provider not in API_PROVIDER_REGISTRY:
        raise ProviderConfigurationError(reason="unsupported")
    implementation = RUNTIME_PROVIDER_REGISTRY.get(provider)
    if implementation is None:
        raise ProviderConfigurationError(reason="unregistered")
    return implementation
