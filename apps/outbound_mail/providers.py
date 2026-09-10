"""Provider-neutral API delivery registry.

Delivery mode is API; individual providers validate their typed configuration
behind this registry rather than becoming FireDash delivery modes.
"""

from __future__ import annotations

from collections.abc import Callable

from django.core.exceptions import ValidationError

from apps.outbound_mail.runtime import MailProvider, ProviderConfigurationError

BREVO = "BREVO"
SMTP = "SMTP"


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
RUNTIME_PROVIDER_FACTORY_REGISTRY: dict[str, Callable[[], MailProvider]] = {}


def _runtime_provider_is_supported(provider: str) -> bool:
    return provider == SMTP or provider in API_PROVIDER_REGISTRY


def validate_api_provider_configuration(*, provider: str, configuration) -> None:
    validator = API_PROVIDER_REGISTRY.get(provider)
    if validator is None:
        raise ValidationError("Unsupported API mail provider.")
    validator(configuration)


def register_runtime_provider(*, provider: str, implementation: MailProvider) -> None:
    """Register an adapter only for a configured API-provider identity."""
    if not _runtime_provider_is_supported(provider):
        raise ProviderConfigurationError(reason="unsupported")
    if implementation.provider_id != provider:
        raise ProviderConfigurationError(reason="identity_mismatch")
    if provider in RUNTIME_PROVIDER_REGISTRY:
        raise ProviderConfigurationError(reason="already_registered")
    RUNTIME_PROVIDER_REGISTRY[provider] = implementation


def register_runtime_provider_factory(
    *, provider: str, factory: Callable[[], MailProvider]
) -> None:
    """Register a built-in adapter factory for an existing provider identity."""
    if not _runtime_provider_is_supported(provider):
        raise ProviderConfigurationError(reason="unsupported")
    if provider in RUNTIME_PROVIDER_FACTORY_REGISTRY:
        raise ProviderConfigurationError(reason="already_registered")
    RUNTIME_PROVIDER_FACTORY_REGISTRY[provider] = factory


def resolve_runtime_provider(*, provider: str) -> MailProvider:
    """Resolve a registered adapter without vendor-specific branches in callers."""
    if not _runtime_provider_is_supported(provider):
        raise ProviderConfigurationError(reason="unsupported")
    implementation = RUNTIME_PROVIDER_REGISTRY.get(provider)
    if implementation is not None:
        return implementation
    factory = RUNTIME_PROVIDER_FACTORY_REGISTRY.get(provider)
    if factory is None:
        raise ProviderConfigurationError(reason="unregistered")
    return factory()


def resolve_effective_provider(*, delivery_mode: str, api_provider: str) -> MailProvider:
    """Resolve the active typed configuration without callers branching on vendors."""
    if delivery_mode == "API":
        return resolve_runtime_provider(provider=api_provider)
    if delivery_mode == "SMTP":
        return resolve_runtime_provider(provider=SMTP)
    raise ProviderConfigurationError()
