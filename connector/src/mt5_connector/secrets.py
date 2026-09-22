"""Credential providers that keep secret values out of connector configuration."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse


class SecretProviderError(RuntimeError):
    """Stable error that never contains the secret value."""


def protected_file_secret_provider(path: str):
    """Return a callback reading a non-empty protected file on demand."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise SecretProviderError("SECRET_FILE_MUST_BE_ABSOLUTE")

    def load() -> str:
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise SecretProviderError("SECRET_UNAVAILABLE") from exc
        if not value:
            raise SecretProviderError("SECRET_UNAVAILABLE")
        return value

    return load


def secret_provider_from_ref(secret_ref: str):
    """Build a provider for supported protected stores without exposing values."""
    parsed = urlparse(secret_ref)
    if parsed.scheme == "file":
        path = unquote(parsed.path)
        if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return protected_file_secret_provider(path)
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
