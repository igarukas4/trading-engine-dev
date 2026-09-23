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
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from psycopg import connect
from psycopg.errors import UniqueViolation

AccountErrorCode = Literal[
    "ACCOUNT_CONTEXT_MISMATCH",
    "DUPLICATE_IDENTITY",
    "STALE_GENERATION",
    "WRONG_ACCOUNT",
    "NOT_READY",
    "INVALID_KEY",
    "INCOMPLETE_MARKET_DATA",
    "OPEN_CANDLE_INPUT",
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
    derived = hashlib.scrypt(
        secret.encode(), salt=key_salt, n=2**14, r=8, p=1, dklen=32
    )
    return key_salt.hex(), derived.hex()


def verify_connector_secret(secret: str, salt_hex: str, digest_hex: str) -> bool:
    try:
        candidate = hashlib.scrypt(
            secret.encode(),
            salt=bytes.fromhex(salt_hex),
            n=2**14,
            r=8,
            p=1,
            dklen=32,
        )
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
    execution_epoch: int = 1
    connector_generation: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    connector_bound: bool = False
    connector_healthy: bool = False
    reconciliation_complete: bool = False
    reconciliation_watermark: str | None = None
    reconciliation_observed_at: datetime | None = None
    risk_limits_active: bool = False
    mappings_valid: bool = False
    version: int = 1
    execution_mode_revision: int = 1
    mode_changed_at: datetime = field(default_factory=_utcnow)

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.provider, self.broker_server, self.external_account_id

    @property
    def can_enable(self) -> bool:
        readiness_gates = (
            self.connector_bound,
            self.connector_healthy,
            self.reconciliation_complete,
            self.risk_limits_active,
            self.mappings_valid,
        )
        return all(readiness_gates) and (
            self.environment == "DEMO" or self.live_execution_enabled
        )

    @property
    def runtime_interlock(self) -> Literal["BLOCKED", "ELIGIBLE"]:
        """Expose exposure readiness without changing custodian selections."""
        return "ELIGIBLE" if self.can_enable and self.bot_state == "RUNNING" else "BLOCKED"

    def enable(self) -> None:
        if not self.can_enable:
            raise AccountError("NOT_READY", "account readiness gates are not healthy")
        self.lifecycle_status = "ENABLED"
        self.version += 1

    def set_execution_mode(self, mode: Literal["MANUAL", "SEMI_AUTO", "FULL_AUTO"], *, now: datetime | None = None) -> None:
        """Change automation only for this account and fence old Signals."""
        if mode == self.execution_mode:
            return
        self.execution_mode = mode
        self.execution_mode_revision += 1
        self.mode_changed_at = now or _utcnow()
        self.version += 1

    def automation_eligible(
        self,
        *,
        signal_created_at: datetime,
        signal_revision: int,
        eligible: bool,
        approved: bool,
        now: datetime | None = None,
    ) -> bool:
        """Return whether this account may schedule one Signal automatically."""
        current = now or _utcnow()
        if not eligible or signal_revision < 1 or signal_created_at > current:
            return False
        # A mode change is an explicit fence: it can never wake an older Signal.
        if signal_created_at < self.mode_changed_at:
            return False
        if self.execution_mode == "FULL_AUTO":
            return True
        return self.execution_mode == "SEMI_AUTO" and approved


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
    def __init__(self, database_url: str = "") -> None:
        self.accounts: dict[str, BrokerAccount] = {}
        self.bindings: dict[str, ConnectorBinding] = {}
        self.database_url = database_url
        self._lock = RLock()
        if database_url:
            self._load()
            self._invalidate_runtime_connector_state()

    def _load(self) -> None:
        with connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT id, provider, broker_server, external_account_id, display_name,
                              environment, lifecycle_status, bot_state, execution_mode,
                              live_execution_enabled, execution_epoch, connector_generation,
                              lease_owner, lease_expires_at, connector_status,
                              reconciliation_status, reconciliation_watermark,
                              reconciliation_observed_at, version, execution_mode_revision, mode_changed_at
                         FROM broker_accounts"""
                )
                for row in cursor.fetchall():
                    account = BrokerAccount(
                        id=str(row[0]), provider=row[1], broker_server=row[2],
                        external_account_id=row[3], display_name=row[4], environment=row[5],
                        lifecycle_status=row[6], bot_state=row[7], execution_mode=row[8],
                        live_execution_enabled=row[9], execution_epoch=row[10],
                        connector_generation=row[11], lease_owner=row[12], lease_expires_at=row[13],
                        connector_bound=row[14] != "UNAVAILABLE",
                        connector_healthy=row[14] == "HEALTHY",
                        reconciliation_complete=row[15] == "COMPLETE", reconciliation_watermark=row[16],
                        reconciliation_observed_at=row[17], version=row[18],
                        execution_mode_revision=row[19], mode_changed_at=row[20],
                    )
                    self.accounts[account.id] = account
                cursor.execute(
                    """SELECT broker_account_id, provider, broker_server, external_account_id,
                              key_id, secret_salt, secret_hash, revoked_at
                         FROM connector_bindings"""
                )
                for row in cursor.fetchall():
                    self.bindings[str(row[0])] = ConnectorBinding(
                        account_id=str(row[0]), provider=row[1], broker_server=row[2],
                        external_account_id=row[3], key_id=row[4], salt_hex=row[5],
                        secret_hash=row[6], revoked=row[7] is not None,
                    )

    def _invalidate_runtime_connector_state(self) -> None:
        """Require a fresh connector lease and reconciliation after a restart."""
        for account in self.accounts.values():
            account.connector_healthy = False
            account.lease_owner = None
            account.lease_expires_at = None
            account.reconciliation_complete = False
            self._persist_account(account)

    def _persist_account(self, account: BrokerAccount) -> None:
        if not self.database_url:
            return
        connector_status = "HEALTHY" if account.connector_healthy else (
            "BOUND" if account.connector_bound else "UNAVAILABLE"
        )
        reconciliation_status = "COMPLETE" if account.reconciliation_complete else "INCOMPLETE"
        with connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE broker_accounts
                          SET lifecycle_status = %s, bot_state = %s, execution_mode = %s,
                              live_execution_enabled = %s, execution_epoch = %s,
                              connector_generation = %s, lease_owner = %s,
                              lease_expires_at = %s, connector_status = %s,
                              reconciliation_status = %s, reconciliation_watermark = %s,
                              reconciliation_observed_at = %s, version = %s,
                              execution_mode_revision = %s, mode_changed_at = %s,
                              updated_at = now()
                        WHERE id = %s""",
                    (account.lifecycle_status, account.bot_state, account.execution_mode,
                     account.live_execution_enabled, account.execution_epoch,
                     account.connector_generation, account.lease_owner,
                     account.lease_expires_at, connector_status, reconciliation_status,
                     account.reconciliation_watermark, account.reconciliation_observed_at,
                     account.version,
                     account.execution_mode_revision, account.mode_changed_at,
                     account.id),
                )

    def register(
        self,
        *,
        provider: str,
        broker_server: str,
        external_account_id: str,
        display_name: str,
        environment: Literal["DEMO", "LIVE"],
    ) -> BrokerAccount:
        identity = (provider, broker_server, external_account_id)
        if any(account.identity == identity for account in self.accounts.values()):
            raise AccountError("DUPLICATE_IDENTITY", "BrokerAccount identity already exists")
        account = BrokerAccount(
            provider=provider,
            broker_server=broker_server,
            external_account_id=external_account_id,
            display_name=display_name,
            environment=environment,
        )
        if self.database_url:
            try:
                with connect(self.database_url) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """INSERT INTO broker_accounts
                               (id, provider, broker_server, external_account_id, display_name, environment)
                               VALUES (%s, %s, %s, %s, %s, %s)""",
                            (account.id, account.provider, account.broker_server,
                             account.external_account_id, account.display_name, account.environment),
                        )
            except UniqueViolation as error:
                raise AccountError("DUPLICATE_IDENTITY", "BrokerAccount identity already exists") from error
        self.accounts[account.id] = account
        return account

    def bind_connector(self, account_id: str, secret: str) -> str:
        account = self.accounts[account_id]
        salt_hex, digest = hash_connector_secret(secret)
        key_id = secrets.token_urlsafe(12)
        self.bindings[account_id] = ConnectorBinding(
            account.id, *account.identity, key_id, salt_hex, digest
        )
        account.connector_bound = True
        if self.database_url:
            binding = self.bindings[account_id]
            with connect(self.database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO connector_bindings
                           (id, broker_account_id, provider, broker_server, external_account_id,
                            key_id, secret_salt, secret_hash)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (broker_account_id) DO UPDATE SET
                             provider = EXCLUDED.provider, broker_server = EXCLUDED.broker_server,
                             external_account_id = EXCLUDED.external_account_id, key_id = EXCLUDED.key_id,
                             secret_salt = EXCLUDED.secret_salt, secret_hash = EXCLUDED.secret_hash,
                             revoked_at = NULL""",
                        (str(uuid4()), binding.account_id, binding.provider, binding.broker_server,
                         binding.external_account_id, binding.key_id, binding.salt_hex,
                         binding.secret_hash),
                    )
            self._persist_account(account)
        return key_id

    def create_bound_account(
        self,
        *,
        provider: str,
        broker_server: str,
        external_account_id: str,
        display_name: str,
        environment: Literal["DEMO", "LIVE"],
        secret: str,
    ) -> tuple[BrokerAccount, str]:
        """Create an immutable account and its only binding in one transaction."""
        identity = (provider, broker_server, external_account_id)
        with self._lock:
            if any(account.identity == identity for account in self.accounts.values()):
                raise AccountError("DUPLICATE_IDENTITY", "BrokerAccount identity already exists")
            salt_hex, digest = hash_connector_secret(secret)
            account = BrokerAccount(
                provider=provider,
                broker_server=broker_server,
                external_account_id=external_account_id,
                display_name=display_name,
                environment=environment,
            )
            key_id = secrets.token_urlsafe(12)
            binding = ConnectorBinding(
                account.id, *account.identity, key_id, salt_hex, digest
            )
            if self.database_url:
                try:
                    with connect(self.database_url) as connection:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                """INSERT INTO broker_accounts
                                   (id, provider, broker_server, external_account_id, display_name, environment)
                                   VALUES (%s, %s, %s, %s, %s, %s)""",
                                (account.id, provider, broker_server, external_account_id,
                                 display_name, environment),
                            )
                            cursor.execute(
                                """INSERT INTO connector_bindings
                                   (id, broker_account_id, provider, broker_server, external_account_id,
                                    key_id, secret_salt, secret_hash)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                                (str(uuid4()), account.id, provider, broker_server,
                                 external_account_id, key_id, salt_hex, digest),
                            )
                except UniqueViolation as error:
                    raise AccountError("DUPLICATE_IDENTITY", "BrokerAccount identity already exists") from error
            account.connector_bound = True
            self.accounts[account.id] = account
            self.bindings[account.id] = binding
            return account, key_id

    def set_live_execution(self, account_id: str, enabled: bool) -> BrokerAccount:
        account = self.accounts.get(account_id)
        if account is None:
            raise AccountError("WRONG_ACCOUNT", "BrokerAccount not found")
        if account.live_execution_enabled == enabled:
            return account
        account.live_execution_enabled = enabled
        account.version += 1
        self._persist_account(account)
        return account

    def persist_account(self, account: BrokerAccount) -> None:
        """Persist an already validated account projection through the registry seam."""
        if account.id not in self.accounts or self.accounts[account.id] is not account:
            raise AccountError("WRONG_ACCOUNT", "BrokerAccount is not owned by this registry")
        self._persist_account(account)

    def authenticate(self, account_id: str, key_id: str, secret: str, generation: int) -> BrokerAccount:
        account = self.accounts.get(account_id)
        binding = self.bindings.get(account_id)
        credentials_valid = (
            account is not None
            and binding is not None
            and not binding.revoked
            and hmac.compare_digest(binding.key_id, key_id)
            and verify_connector_secret(secret, binding.salt_hex, binding.secret_hash)
        )
        if not credentials_valid:
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
        account.connector_healthy = True
        self._persist_account(account)
        return account

    def mark_connector_unhealthy(self, account_id: str) -> BrokerAccount:
        account = self.accounts.get(account_id)
        if account is None:
            raise AccountError("WRONG_ACCOUNT", "BrokerAccount not found")
        account.connector_healthy = False
        self._persist_account(account)
        return account

    def mark_reconciled(
        self, account_id: str, watermark: str | None = None,
    ) -> BrokerAccount:
        account = self.accounts.get(account_id)
        if account is None:
            raise AccountError("WRONG_ACCOUNT", "BrokerAccount not found")
        account.reconciliation_complete = True
        if watermark:
            account.reconciliation_watermark = watermark
            if isinstance(watermark, str):
                try:
                    account.reconciliation_observed_at = datetime.fromisoformat(
                        watermark.replace("Z", "+00:00")
                    )
                except ValueError:
                    account.reconciliation_observed_at = None
            else:
                account.reconciliation_observed_at = watermark
        self._persist_account(account)
        return account

    def set_execution_mode(
        self,
        account_id: str,
        mode: Literal["MANUAL", "SEMI_AUTO", "FULL_AUTO"],
        *,
        now: datetime | None = None,
    ) -> BrokerAccount:
        account = self.accounts.get(account_id)
        if account is None:
            raise AccountError("WRONG_ACCOUNT", "BrokerAccount not found")
        account.set_execution_mode(mode, now=now)
        self._persist_account(account)
        return account

    def read_only_snapshot(self, account_id: str) -> dict[str, Any]:
        account = self.accounts[account_id]
        return {
            "account_id": account.id,
            "environment": account.environment,
            "identity": {
                "provider": account.provider,
                "broker_server": account.broker_server,
                "external_account_id": account.external_account_id,
            },
            "lifecycle_status": account.lifecycle_status,
            "bot_state": account.bot_state,
            "execution_mode": account.execution_mode,
            "execution_mode_revision": account.execution_mode_revision,
            "mode_changed_at": account.mode_changed_at.isoformat(),
            "live_execution_enabled": account.live_execution_enabled,
            "runtime_interlock": account.runtime_interlock,
            "connector": {
                "bound": account.connector_bound,
                "healthy": account.connector_healthy,
                "generation": account.connector_generation,
            },
            "reconciliation": {"complete": account.reconciliation_complete},
            "readiness": {"can_enable": account.can_enable},
            "execution_locked": True,
        }


def assert_account_scope(account_id: str, referenced_account_id: str) -> None:
    if account_id != referenced_account_id:
        raise AccountError("ACCOUNT_CONTEXT_MISMATCH", "referenced record belongs to another BrokerAccount")
