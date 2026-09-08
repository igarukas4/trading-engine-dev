"""Account identity and connector security primitives for the V0 foundation.

This module deliberately contains no broker calls. It owns the invariants that
must be true before a later execution adapter can be attached.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

AccountErrorCode = Literal[
    "ACCOUNT_CONTEXT_MISMATCH", "DUPLICATE_IDENTITY", "STALE_GENERATION",
    "WRONG_ACCOUNT", "NOT_READY", "INVALID_KEY",
]


class AccountError(ValueError):
    def __init__(self, code: AccountErrorCode, message: str):
        super().__init__(message)
        self.code = code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_connector_secret(secret: str, salt: bytes | None = None) -> tuple[str, str]:
    if not secret or len(secret) < 32:
        raise AccountError("INVALID_KEY", "connector key must contain at least 32 characters")
    key_salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(secret.encode(), salt=key_salt, n=2**14, r=8, p=1, dklen=32)
    return key_salt.hex(), derived.hex()


def verify_connector_secret(secret: str, salt_hex: str, digest_hex: str) -> bool:
    try:
        candidate = hashlib.scrypt(secret.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


@dataclass
class BrokerAccount:
    provider: str
    broker_server: str
    external_account_id: str
    display_name: str
    environment: Literal["DEMO", "LIVE"] = "DEMO"
    id: str = field(default_factory=lambda: str(uuid4()))
    lifecycle_status: Literal["DISABLED", "ENABLED", "ARCHIVED"] = "DISABLED"
    bot_state: Literal["STOPPED", "RUNNING", "EMERGENCY_STOP"] = "STOPPED"
    execution_mode: Literal["MANUAL", "SEMI_AUTO", "FULL_AUTO"] = "MANUAL"
    live_execution_enabled: bool = False
    execution_epoch: int = 0
    connector_generation: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    connector_bound: bool = False
    connector_healthy: bool = False
    reconciliation_complete: bool = False
    risk_limits_active: bool = False
    mappings_valid: bool = False
    version: int = 1

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.provider, self.broker_server, self.external_account_id

    @property
    def can_enable(self) -> bool:
        return self.connector_bound and self.connector_healthy and self.reconciliation_complete and self.risk_limits_active and self.mappings_valid and (self.environment == "DEMO" or self.live_execution_enabled)

    def enable(self) -> None:
        if not self.can_enable:
            raise AccountError("NOT_READY", "account readiness gates are not healthy")
        self.lifecycle_status = "ENABLED"
        self.version += 1


@dataclass
class ConnectorBinding:
    account_id: str
    provider: str
    broker_server: str
    external_account_id: str
    key_id: str
    salt_hex: str
    secret_hash: str
    revoked: bool = False


class AccountRegistry:
    def __init__(self) -> None:
        self.accounts: dict[str, BrokerAccount] = {}
        self.bindings: dict[str, ConnectorBinding] = {}

    def register(self, **values: Any) -> BrokerAccount:
        identity = (values["provider"], values["broker_server"], values["external_account_id"])
        if any(account.identity == identity for account in self.accounts.values()):
            raise AccountError("DUPLICATE_IDENTITY", "BrokerAccount identity already exists")
        account = BrokerAccount(**values)
        self.accounts[account.id] = account
        return account

    def bind_connector(self, account_id: str, secret: str) -> str:
        account = self.accounts[account_id]
        salt_hex, digest = hash_connector_secret(secret)
        key_id = secrets.token_urlsafe(12)
        self.bindings[account_id] = ConnectorBinding(account.id, *account.identity, key_id, salt_hex, digest)
        account.connector_bound = True
        return key_id

    def authenticate(self, account_id: str, key_id: str, secret: str, generation: int) -> BrokerAccount:
        account = self.accounts.get(account_id)
        binding = self.bindings.get(account_id)
        if not account or not binding or binding.revoked or not hmac.compare_digest(binding.key_id, key_id) or not verify_connector_secret(secret, binding.salt_hex, binding.secret_hash):
            raise AccountError("WRONG_ACCOUNT", "connector authentication failed")
        if generation != account.connector_generation:
            raise AccountError("STALE_GENERATION", "connector generation is not current")
        return account

    def heartbeat(self, account_id: str, generation: int, owner: str, lease_seconds: int = 30) -> BrokerAccount:
        account = self.accounts.get(account_id)
        if not account or generation != account.connector_generation:
            raise AccountError("STALE_GENERATION", "connector generation is not current")
        now = _utcnow()
        if account.lease_owner not in (None, owner) and account.lease_expires_at and account.lease_expires_at > now:
            raise AccountError("STALE_GENERATION", "connector lease belongs to another session")
        account.lease_owner = owner
        account.lease_expires_at = now + timedelta(seconds=lease_seconds)
        account.last_heartbeat_at = now
        return account

    def read_only_snapshot(self, account_id: str) -> dict[str, Any]:
        account = self.accounts[account_id]
        return {"account_id": account.id, "identity": {"provider": account.provider, "broker_server": account.broker_server, "external_account_id": account.external_account_id}, "lifecycle_status": account.lifecycle_status, "bot_state": account.bot_state, "execution_mode": account.execution_mode, "live_execution_enabled": account.live_execution_enabled, "connector": {"bound": account.connector_bound, "healthy": account.connector_healthy, "generation": account.connector_generation}, "reconciliation": {"complete": account.reconciliation_complete}, "readiness": {"can_enable": account.can_enable}, "execution_locked": True}


def assert_account_scope(account_id: str, referenced_account_id: str) -> None:
    if account_id != referenced_account_id:
        raise AccountError("ACCOUNT_CONTEXT_MISMATCH", "referenced record belongs to another BrokerAccount")
