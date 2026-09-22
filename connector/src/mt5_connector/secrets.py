"""Credential providers that keep secret values out of connector configuration."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess
from urllib.parse import unquote, urlparse


class SecretProviderError(RuntimeError):
    """Stable error that never contains the secret value."""


def _check_secret_file_acl(candidate: Path) -> None:
    """Reject a secret file that is readable by a broad principal.

    POSIX mode bits are the fallback equivalent for non-Windows test hosts. On
    Windows, ``icacls`` is the supported system ACL inspection tool. The
    provider never repairs ACLs implicitly because doing so could hide an
    operator mistake or race with another process.
    """
    try:
        info = candidate.lstat()
    except (OSError, ValueError) as exc:
        raise SecretProviderError("SECRET_UNAVAILABLE") from exc
    if not stat.S_ISREG(info.st_mode) or candidate.is_symlink():
        raise SecretProviderError("SECRET_FILE_UNSAFE")
    if os.name != "nt":
        if info.st_mode & 0o077:
            raise SecretProviderError("SECRET_FILE_UNSAFE")
        return
    try:
        result = subprocess.run(
            ["icacls", str(candidate)],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError) as exc:
        raise SecretProviderError("SECRET_FILE_ACL_UNAVAILABLE") from exc
    if result.returncode != 0:
        raise SecretProviderError("SECRET_FILE_ACL_UNAVAILABLE")
    # The first icacls line is the file path, not an ACE. Inspect ACE lines
    # only so a path such as ``C:\\Users\\operator`` cannot trip a principal
    # check.
    acl = "\n".join((result.stdout or "").splitlines()[1:]).casefold()
    broad_principals = (
        "everyone",
        "authenticated users",
        "builtin\\users",
        "guests",
        "anonymous logon",
        "s-1-1-0",       # Everyone
        "s-1-5-11",      # Authenticated Users
        "s-1-5-32-545",  # Builtin Users
        "s-1-5-32-546",  # Builtin Guests
        "s-1-5-7",       # Anonymous Logon
    )
    if any(principal in acl for principal in broad_principals) or re.search(
        r"(?:^|\s)[^\s:]+\\users\s*:", acl
    ):
        raise SecretProviderError("SECRET_FILE_UNSAFE")


def protected_file_secret_provider(path: str):
    """Return a callback reading a non-empty protected file on demand."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise SecretProviderError("SECRET_FILE_MUST_BE_ABSOLUTE")
    _check_secret_file_acl(candidate)

    def load() -> str:
        try:
            _check_secret_file_acl(candidate)
            value = candidate.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError, SecretProviderError) as exc:
            raise SecretProviderError("SECRET_UNAVAILABLE") from exc
        if not value:
            raise SecretProviderError("SECRET_UNAVAILABLE")
        return value

    # The runtime uses this marker to keep a read-only file callback out of
    # command-capable sessions. ACL checks and a later path read are not an
    # atomic proof of the same file.
    load.read_only_file_provider = True
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
