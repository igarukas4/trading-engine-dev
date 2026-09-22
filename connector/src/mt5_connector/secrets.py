"""Credential providers that keep secret values out of connector configuration."""
from __future__ import annotations

from urllib.parse import urlparse


class SecretProviderError(RuntimeError):
    """Stable error that never contains the secret value."""


def secret_provider_from_ref(secret_ref: str):
    """Build a provider for supported protected stores without exposing values."""
    parsed = urlparse(secret_ref)
    if parsed.scheme == "file":
        raise SecretProviderError("SECRET_REFERENCE_UNSUPPORTED")
    if parsed.scheme == "credential-manager":
        target = parsed.netloc or parsed.path.lstrip("/")
        if not target:
            raise SecretProviderError("SECRET_REFERENCE_INVALID")

        def load() -> str:
            try:
                import keyring
                value = keyring.get_password("mt5-connector", target)
            except Exception as exc:
                raise SecretProviderError("SECRET_UNAVAILABLE") from exc
            if not value:
                raise SecretProviderError("SECRET_UNAVAILABLE")
            return value

        return load
    raise SecretProviderError("SECRET_REFERENCE_UNSUPPORTED")
