"""The account-local DEMO lifecycle transaction.

The lifecycle interface is deliberately small. It owns the transition,
readiness decision, idempotency record, audit evidence, execution epoch, and
entry fence as one operation. HTTP and WebSocket handlers only translate
requests and responses at this seam.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID, uuid4

from psycopg import connect
from psycopg.types.json import Jsonb


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _parse_time(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


@dataclass(frozen=True)
class ReadinessContext:
    binding_identity: tuple[str, str, str] | None
    connector_healthy: bool
    lease_current: bool
    reconciliation_complete: bool
    no_unknown: bool
    runtime_interlock: str
    runtime_reason_codes: tuple[str, ...]
    recovery_ready: bool
    risk_limits_version: int | None = None
    pair_mappings: dict[str, str] = field(default_factory=dict)
    facts_complete: bool = True
    binding_matches: bool = True
    binding_revoked: bool = False
    generation_current: bool = True
    broker_facts_fresh: bool = True

    @property
    def allowed(self) -> bool:
        return bool(
            self.binding_identity
            and self.binding_matches
            and not self.binding_revoked
            and self.connector_healthy
            and self.lease_current
            and self.generation_current
            and self.reconciliation_complete
            and self.broker_facts_fresh
            and self.no_unknown
            and self.runtime_interlock == "ELIGIBLE"
            and not self.runtime_reason_codes
            and self.recovery_ready
            and self.facts_complete
            and self.risk_limits_version is not None
            and bool(self.pair_mappings)
        )

    @property
    def reason_codes(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.binding_identity:
            reasons.append("CONNECTOR_BINDING_MISSING")
        elif not self.binding_matches:
            reasons.append("CONNECTOR_BINDING_MISMATCH")
        if self.binding_revoked:
            reasons.append("CONNECTOR_BINDING_REVOKED")
        if not self.connector_healthy:
            reasons.append("CONNECTOR_UNHEALTHY")
        if not self.lease_current:
            reasons.append("CONNECTOR_LEASE_STALE")
        if not self.generation_current:
            reasons.append("CONNECTOR_GENERATION_STALE")
        if not self.reconciliation_complete:
            reasons.append("BROKER_FACTS_STALE")
        if not self.broker_facts_fresh:
            reasons.append("BROKER_FACTS_STALE")
        if not self.no_unknown:
            reasons.append("UNKNOWN_COMMANDS_PRESENT")
        if self.runtime_interlock != "ELIGIBLE":
            reasons.append("RUNTIME_INTERLOCK_BLOCKED")
        reasons.extend(self.runtime_reason_codes)
        if not self.recovery_ready:
            reasons.append("RECOVERY_INCOMPLETE")
        if not self.facts_complete:
            reasons.append("READINESS_FACTS_INCOMPLETE")
        elif self.risk_limits_version is None:
            reasons.append("RISK_LIMITS_MISSING")
        if self.facts_complete and not self.pair_mappings:
            reasons.append("PAIR_MAPPING_MISSING")
        return tuple(dict.fromkeys(reasons))


@dataclass(frozen=True)
class ReadinessFacts:
    risk_limits_version: int | None = None
    pair_mappings: dict[str, str] = field(default_factory=dict)


@dataclass
class LifecycleResult:
    command_id: str
    audit_id: str
    status: str
    action: str
    lifecycle_status: str
    bot_state: str
    account_version: int
    execution_epoch: int
    readiness: ReadinessContext
    replayed: bool = False
    actor: str = ""
    reason: str = ""
    request_hash: str = ""
    prior_state: dict[str, Any] = field(default_factory=dict)
    new_state: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    completed_at: str = ""
    interlock_state: str = ""
    rejection_code: str | None = None


class LifecycleCoordinator:
    """Deep account lifecycle module with JSON and PostgreSQL adapters."""

    ACTIONS = frozenset({"enable", "start", "stop", "disable"})

    def __init__(
        self,
        state_path: str | os.PathLike[str] | None = None,
        database_url: str = "",
        *,
        account_registry: Any | None = None,
        execution: Any | None = None,
    ) -> None:
        self.state_path = Path(state_path) if state_path else None
        self.database_url = database_url
        self.account_registry = account_registry
        self.execution = execution
        self._commands: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self._audits: dict[str, list[dict[str, Any]]] = {}
        self._facts: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._global_lock = threading.RLock()
        self._load()
        if self.database_url:
            self._db_load_facts()

    def _lock_for(self, account_id: str) -> threading.RLock:
        with self._global_lock:
            return self._locks.setdefault(account_id, threading.RLock())

    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        for item in raw.get("commands", []):
            self._commands[self._command_key(item)] = item
        self._audits = {str(key): list(value) for key, value in raw.get("audits", {}).items()}
        self._facts = dict(raw.get("readiness", {}))

    @staticmethod
    def _command_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(item["account_id"]), str(item.get("actor", "")),
            str(item.get("operation", "lifecycle")), str(item["idempotency_key"]),
        )

    def _save(self) -> None:
        if not self.state_path:
            return
        payload = {
            "commands": list(self._commands.values()),
            "audits": self._audits,
            "readiness": self._facts,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.", dir=self.state_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _db_load_facts(self) -> None:
        with connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT broker_account_id, risk_limits_version, pair_mappings "
                    "FROM lifecycle_readiness"
                )
                for account_id, risk_version, mappings in cursor.fetchall():
                    self._facts[str(account_id)] = {
                        "risk_limits_version": risk_version,
                        "pair_mappings": dict(mappings or {}),
                    }

    def record_risk_limits(self, account_id: str, *, version: int) -> None:
        with self._lock_for(account_id):
            self._facts.setdefault(account_id, {})["risk_limits_version"] = version
            if self.database_url:
                with connect(self.database_url) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """INSERT INTO lifecycle_readiness
                               (broker_account_id, risk_limits_version)
                               VALUES (%s, %s)
                               ON CONFLICT (broker_account_id) DO UPDATE
                               SET risk_limits_version = EXCLUDED.risk_limits_version,
                                   updated_at = now()""",
                            (account_id, version),
                        )
            self._save()

    def record_pair_mapping(
        self, account_id: str, pair: str, broker_symbol: str, *, valid: bool,
    ) -> None:
        """Store readiness by canonical Pair code, never by broker symbol."""
        with self._lock_for(account_id):
            facts = self._facts.setdefault(account_id, {})
            mappings = facts.setdefault("pair_mappings", {})
            if valid:
                mappings[pair] = broker_symbol
            else:
                mappings.pop(pair, None)
            if self.database_url:
                with connect(self.database_url) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """INSERT INTO lifecycle_readiness
                               (broker_account_id, pair_mappings)
                               VALUES (%s, %s)
                               ON CONFLICT (broker_account_id) DO UPDATE
                               SET pair_mappings = EXCLUDED.pair_mappings,
                                   updated_at = now()""",
                            (account_id, Jsonb(mappings)),
                        )
            self._save()

    def readiness_facts(self, account_id: str) -> ReadinessFacts:
        facts = self._facts.get(account_id, {})
        return ReadinessFacts(
            facts.get("risk_limits_version"), dict(facts.get("pair_mappings", {}))
        )

    def _account_state(self, account: Any) -> dict[str, Any]:
        interlock = (
            self.execution.runtime_interlock(account.id)
            if self.execution is not None
            else None
        )
        return {
            "lifecycle_status": account.lifecycle_status,
            "bot_state": account.bot_state,
            "execution_mode": account.execution_mode,
            "account_version": account.version,
            "execution_epoch": account.execution_epoch,
            "runtime_interlock": (
                interlock.status if interlock is not None
                else getattr(account, "runtime_interlock", "BLOCKED")
            ),
            "interlock_reason_codes": (
                list(interlock.reasons) if interlock is not None else []
            ),
            "interlock_evidence": (
                dict(interlock.evidence) if interlock is not None else {}
            ),
        }

    @staticmethod
    def _readiness_payload(readiness: ReadinessContext) -> dict[str, Any]:
        return {
            "allowed": readiness.allowed,
            "binding_identity": list(readiness.binding_identity) if readiness.binding_identity else None,
            "binding_matches": readiness.binding_matches,
            "binding_revoked": readiness.binding_revoked,
            "connector_healthy": readiness.connector_healthy,
            "lease_current": readiness.lease_current,
            "generation_current": readiness.generation_current,
            "reconciliation_complete": readiness.reconciliation_complete,
            "broker_facts_fresh": readiness.broker_facts_fresh,
            "no_unknown": readiness.no_unknown,
            "runtime_interlock": readiness.runtime_interlock,
            "runtime_reason_codes": list(readiness.runtime_reason_codes),
            "recovery_ready": readiness.recovery_ready,
            "risk_limits_version": readiness.risk_limits_version,
            "pair_mappings": dict(readiness.pair_mappings),
            "reason_codes": list(readiness.reason_codes),
            "facts_complete": readiness.facts_complete,
        }

    def readiness_context(
        self, account: Any, binding: Any, execution: Any, *,
        for_lifecycle: bool = False,
    ) -> ReadinessContext:
        facts = self.readiness_facts(account.id)
        now = _now()
        lease_expires = _parse_time(getattr(account, "lease_expires_at", None))
        lease_current = bool(
            getattr(account, "lease_owner", None)
            and lease_expires is not None
            and lease_expires > now
        )
        account_identity = getattr(account, "identity", None)
        binding_identity = (
            (binding.provider, binding.broker_server, binding.external_account_id)
            if binding else None
        )
        binding_matches = bool(
            binding and not getattr(binding, "revoked", False)
            and binding_identity == account_identity
            and binding.account_id == account.id
        )
        observed = _parse_time(
            getattr(account, "reconciliation_observed_at", None)
            or getattr(account, "reconciliation_watermark", None)
        )
        broker_facts_fresh = bool(
            observed is not None and (now - observed).total_seconds() <= 300
        )
        decision = execution.runtime_interlock(account.id)
        runtime_reasons = tuple(decision.reasons)
        if for_lifecycle:
            runtime_reasons = tuple(
                reason for reason in runtime_reasons if reason != "LIFECYCLE_STOPPED"
            )
        runtime_status = decision.status
        if for_lifecycle and runtime_status == "BLOCKED" and not runtime_reasons:
            runtime_status = "ELIGIBLE"
        no_unknown = not any(
            record.account_id == account.id and record.state == "UNKNOWN"
            for record in execution.dispatch_records.values()
        ) and not any(
            item.get("status") in {"PENDING", "ESCALATED"}
            for item in execution.recovery_records(account.id)
        )
        return ReadinessContext(
            binding_identity=binding_identity,
            binding_matches=binding_matches,
            binding_revoked=bool(binding and getattr(binding, "revoked", False)),
            connector_healthy=bool(getattr(account, "connector_healthy", False)),
            lease_current=lease_current,
            generation_current=getattr(account, "connector_generation", 0) >= 0,
            reconciliation_complete=bool(getattr(account, "reconciliation_complete", False)),
            broker_facts_fresh=broker_facts_fresh,
            no_unknown=no_unknown,
            runtime_interlock=runtime_status,
            runtime_reason_codes=runtime_reasons,
            recovery_ready=not execution.account(account.id).recovery_required,
            risk_limits_version=facts.risk_limits_version,
            pair_mappings=facts.pair_mappings,
            facts_complete=True,
        )

    @staticmethod
    def _transition_readiness(
        readiness: ReadinessContext, action: str,
    ) -> ReadinessContext:
        """Ignore only the lifecycle's own stop fence when reopening it."""
        if action not in {"enable", "start"}:
            return readiness
        reasons = tuple(
            reason for reason in readiness.runtime_reason_codes
            if reason != "LIFECYCLE_STOPPED"
        )
        status = readiness.runtime_interlock
        if status == "BLOCKED" and not reasons:
            status = "ELIGIBLE"
        return replace(
            readiness, runtime_interlock=status, runtime_reason_codes=reasons,
        )

    @contextmanager
    def _execution_account_lock(self, account_id: str) -> Iterator[None]:
        if self.execution is None or not hasattr(self.execution, "account_lock"):
            yield
            return
        with self.execution.account_lock(account_id):
            yield

    def restore_account(self, account: Any) -> Any:
        items = [item for item in self._commands.values() if item["account_id"] == account.id]
        if not items:
            return account
        latest = max(items, key=lambda item: item.get("completed_at", item.get("created_at", "")))
        state = latest.get("new_state", latest.get("result_snapshot", {}))
        account.lifecycle_status = state.get("lifecycle_status", account.lifecycle_status)
        account.bot_state = state.get("bot_state", account.bot_state)
        account.version = state.get("account_version", account.version)
        account.execution_epoch = state.get("execution_epoch", account.execution_epoch)
        return account

    def _result_from_item(
        self, item: dict[str, Any], account: Any, *, replayed: bool,
    ) -> LifecycleResult:
        state = item.get("new_state", item.get("result_snapshot", {}))
        raw = item.get("readiness_payload", {})
        readiness = ReadinessContext(
            binding_identity=tuple(raw["binding_identity"]) if raw.get("binding_identity") else None,
            binding_matches=bool(raw.get("binding_matches", True)),
            binding_revoked=bool(raw.get("binding_revoked", False)),
            connector_healthy=bool(raw.get("connector_healthy", False)),
            lease_current=bool(raw.get("lease_current", False)),
            generation_current=bool(raw.get("generation_current", True)),
            reconciliation_complete=bool(raw.get("reconciliation_complete", False)),
            broker_facts_fresh=bool(raw.get("broker_facts_fresh", True)),
            no_unknown=bool(raw.get("no_unknown", False)),
            runtime_interlock=raw.get("runtime_interlock", "BLOCKED"),
            runtime_reason_codes=tuple(raw.get("runtime_reason_codes", ())),
            recovery_ready=bool(raw.get("recovery_ready", False)),
            risk_limits_version=raw.get("risk_limits_version"),
            pair_mappings=dict(raw.get("pair_mappings", {})),
        )
        return LifecycleResult(
            command_id=str(item["command_id"]), audit_id=str(item["audit_id"]),
            status=str(item["status"]), action=str(item["action"]),
            lifecycle_status=state.get("lifecycle_status", account.lifecycle_status),
            bot_state=state.get("bot_state", account.bot_state),
            account_version=int(state.get("account_version", account.version)),
            execution_epoch=int(state.get("execution_epoch", account.execution_epoch)),
            readiness=readiness, replayed=replayed,
            actor=str(item.get("actor", "")), reason=str(item.get("reason", "")),
            request_hash=str(item.get("request_hash", "")),
            prior_state=dict(item.get("prior_state", {})), new_state=dict(state),
            created_at=str(item.get("created_at", "")),
            completed_at=str(item.get("completed_at", "")),
            interlock_state=str(state.get("runtime_interlock", "BLOCKED")),
            rejection_code=item.get("rejection_code"),
        )

    def _hash_request(
        self, action: str, idempotency_key: str, expected_version: int,
        reason: str, actor: str, operation: str,
    ) -> str:
        payload = {
            "action": action, "idempotency_key": idempotency_key,
            "expected_version": expected_version, "reason": reason,
            "actor": actor, "operation": operation,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _validate_transition(self, account: Any, action: str) -> str | None:
        if action not in self.ACTIONS:
            return "UNKNOWN_LIFECYCLE_ACTION"
        if account.lifecycle_status == "ARCHIVED":
            return "ARCHIVED_ACCOUNT"
        if account.environment != "DEMO":
            return "DEMO_ONLY"
        if account.execution_mode != "MANUAL":
            return "MANUAL_MODE_REQUIRED"
        if action == "enable" and (account.lifecycle_status != "DISABLED" or account.bot_state != "STOPPED"):
            return "INVALID_ENABLE_TRANSITION"
        if action == "start" and (account.lifecycle_status != "ENABLED" or account.bot_state != "STOPPED"):
            return "INVALID_START_TRANSITION"
        if action == "stop" and account.bot_state == "STOPPED":
            return "ALREADY_STOPPED"
        if action == "disable" and account.bot_state != "STOPPED":
            return "DISABLE_REQUIRES_STOPPED"
        return None

    def command(
        self, account: Any, action: str, *, idempotency_key: str,
        expected_version: int, reason: str, actor: str,
        readiness: ReadinessContext | None = None, fence: Any | None = None,
        principal: str | None = None, operation: str = "lifecycle",
    ) -> LifecycleResult:
        actor = principal or actor
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("REASON_REQUIRED")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("AUTHENTICATION_REQUIRED")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("IDEMPOTENCY_KEY_REQUIRED")
        with self._execution_account_lock(account.id):
            if self.database_url:
                return self._command_db(
                    account, action, idempotency_key=idempotency_key,
                    expected_version=expected_version, reason=reason, actor=actor,
                    readiness=readiness, operation=operation,
                )
            return self._command_local(
                account, action, idempotency_key=idempotency_key,
                expected_version=expected_version, reason=reason, actor=actor,
                readiness=readiness, fence=fence, operation=operation,
            )

    def _command_local(
        self, account: Any, action: str, *, idempotency_key: str,
        expected_version: int, reason: str, actor: str,
        readiness: ReadinessContext | None, fence: Any | None, operation: str,
    ) -> LifecycleResult:
        with self._lock_for(account.id):
            request_hash = self._hash_request(
                action, idempotency_key, expected_version, reason, actor, operation
            )
            key = (account.id, actor, operation, idempotency_key)
            previous = self._commands.get(key)
            if previous is not None:
                if previous.get("request_hash") != request_hash:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return self._result_from_item(previous, account, replayed=True)
            readiness = readiness or ReadinessContext(
                None, False, False, False, False, "BLOCKED", (), False,
            )
            readiness = self._transition_readiness(readiness, action)
            prior_state = self._account_state(account)
            error = self._validate_transition(account, action)
            if error is None and expected_version != account.version:
                error = "STALE_VERSION"
            if error is None and action in {"enable", "start"} and not readiness.allowed:
                error = "READINESS:" + ",".join(readiness.reason_codes)
            if error is not None:
                self._record_local_rejection(
                    account, action, idempotency_key, request_hash, expected_version,
                    reason, actor, operation, readiness, error, prior_state,
                )
                raise ValueError(error)
            account_before = dict(account.__dict__)
            execution_before = self.execution._snapshot() if self.execution is not None else None
            audits_before = copy.deepcopy(self._audits.get(account.id, []))
            try:
                if action == "enable":
                    account.connector_bound = readiness.binding_identity is not None
                    account.connector_healthy = readiness.connector_healthy
                    account.reconciliation_complete = readiness.reconciliation_complete
                    account.risk_limits_active = readiness.risk_limits_version is not None
                    account.mappings_valid = bool(readiness.pair_mappings)
                    account.enable()
                elif action == "start":
                    account.bot_state = "RUNNING"
                    account.version += 1
                    if fence:
                        fence(True)
                elif action == "stop":
                    account.bot_state = "STOPPED"
                    account.execution_epoch += 1
                    account.version += 1
                    if fence:
                        fence(False)
                elif action == "disable":
                    account.lifecycle_status = "DISABLED"
                    account.bot_state = "STOPPED"
                    account.execution_epoch += 1
                    account.version += 1
                    if fence:
                        fence(False)
                if action in {"start", "stop", "disable"} and self.execution is not None:
                    self.execution.apply_lifecycle_commit(
                        account.id, account.execution_epoch,
                        allowed=action == "start", persist=False,
                    )
                if action == "enable" and self.execution is not None:
                    # The account remains stopped after enable, but the
                    # previous lifecycle fence is no longer a health reason.
                    self.execution.apply_lifecycle_commit(
                        account.id, account.execution_epoch, allowed=False,
                        persist=False,
                    )
                    execution_state = self.execution.account(account.id)
                    execution_state.interlock_reasons = tuple(
                        reason for reason in execution_state.interlock_reasons
                        if reason != "LIFECYCLE_STOPPED"
                    )
                    if not execution_state.interlock_reasons:
                        execution_state.runtime_interlock = "ELIGIBLE"
                new_state = self._account_state(account)
                created = _iso(_now()) or ""
                command_id, audit_id = str(uuid4()), str(uuid4())
                item = {
                    "account_id": account.id, "actor": actor, "operation": operation,
                    "idempotency_key": idempotency_key, "request_hash": request_hash,
                    "command_id": command_id, "audit_id": audit_id, "action": action,
                    "status": "ACCEPTED", "expected_version": expected_version,
                    "observed_version": account.version, "execution_epoch": account.execution_epoch,
                    "reason": reason, "prior_state": prior_state, "new_state": new_state,
                    "readiness_payload": self._readiness_payload(readiness),
                    "created_at": created, "completed_at": _iso(_now()) or "",
                }
                self._commands[key] = item
                self._audits.setdefault(account.id, []).append({
                    "id": audit_id, "event_type": f"lifecycle.{action}",
                    "status": "ACCEPTED", "actor": actor, "reason": reason,
                    "idempotency_key": idempotency_key, "request_hash": request_hash,
                    "prior_state": prior_state, "new_state": new_state,
                    "readiness": self._readiness_payload(readiness),
                    "created_at": item["created_at"], "completed_at": item["completed_at"],
                })
                self._save()
            except BaseException:
                account.__dict__.clear()
                account.__dict__.update(account_before)
                self._commands.pop(key, None)
                if audits_before:
                    self._audits[account.id] = audits_before
                else:
                    self._audits.pop(account.id, None)
                if self.execution is not None and execution_before is not None:
                    self.execution._restore(execution_before)
                raise
            self._apply_committed_projection(account, item)
            return self._result_from_item(item, account, replayed=False)

    def _record_local_rejection(
        self, account: Any, action: str, idempotency_key: str, request_hash: str,
        expected_version: int, reason: str, actor: str, operation: str,
        readiness: ReadinessContext, error: str, prior_state: dict[str, Any],
    ) -> None:
        command_id, audit_id = str(uuid4()), str(uuid4())
        item = {
            "account_id": account.id, "actor": actor, "operation": operation,
            "idempotency_key": idempotency_key, "request_hash": request_hash,
            "command_id": command_id, "audit_id": audit_id, "action": action,
            "status": "REJECTED", "expected_version": expected_version,
            "observed_version": account.version, "execution_epoch": account.execution_epoch,
            "reason": reason, "rejection_code": error,
            "prior_state": prior_state, "new_state": prior_state,
            "readiness_payload": self._readiness_payload(readiness),
            "created_at": _iso(_now()) or "", "completed_at": _iso(_now()) or "",
        }
        self._commands[self._command_key(item)] = item
        self._audits.setdefault(account.id, []).append({
            "id": audit_id, "event_type": f"lifecycle.{action}", "status": "REJECTED",
            "actor": actor, "reason": reason, "code": error,
            "idempotency_key": idempotency_key, "request_hash": request_hash,
            "prior_state": prior_state, "new_state": prior_state,
            "readiness": self._readiness_payload(readiness),
            "created_at": item["created_at"], "completed_at": item["completed_at"],
        })
        self._save()

    def _db_readiness(self, cursor: Any, account: Any, readiness: ReadinessContext) -> ReadinessContext:
        """Read every lifecycle gate from rows locked in the same transaction."""
        cursor.execute(
            """SELECT provider, broker_server, external_account_id,
                      connector_status, reconciliation_status, reconciliation_watermark,
                      reconciliation_observed_at, connector_generation, pending_generation,
                      lease_owner, lease_expires_at
               FROM broker_accounts WHERE id = %s""", (account.id,)
        )
        account_row = cursor.fetchone()
        if account_row is None:
            return readiness
        identity = tuple(str(value) for value in account_row[:3])
        connector_status = account_row[3]
        reconciliation_status = account_row[4]
        observed = _parse_time(account_row[6] or account_row[5])
        broker_facts_fresh = bool(
            observed is not None and (_now() - observed).total_seconds() <= 300
        )
        lease_expires = _parse_time(account_row[10])
        lease_current = bool(
            account_row[9] and lease_expires is not None and lease_expires > _now()
        )
        generation_current = (
            account_row[8] is None and int(account_row[7]) >= 0
        )

        cursor.execute(
            """SELECT risk_limits_version, pair_mappings
               FROM lifecycle_readiness WHERE broker_account_id = %s FOR UPDATE""",
            (account.id,),
        )
        fact_row = cursor.fetchone()
        risk_version = fact_row[0] if fact_row else None
        mappings = dict(fact_row[1] or {}) if fact_row else {}
        cursor.execute(
            "SELECT max(version) FROM risk_limits WHERE broker_account_id = %s",
            (account.id,),
        )
        risk_row = cursor.fetchone()
        if risk_row is None or risk_row[0] is None or risk_version != int(risk_row[0]):
            risk_version = None
        cursor.execute(
            """SELECT canonical_code, broker_symbol FROM pairs
               WHERE broker_account_id = %s FOR SHARE""",
            (account.id,),
        )
        persisted_pairs = {
            str(pair): str(symbol)
            for pair, symbol in cursor.fetchall()
            if pair and symbol
        }
        mappings = {
            pair: symbol for pair, symbol in mappings.items()
            if pair in persisted_pairs and persisted_pairs[pair] == symbol
        }

        cursor.execute(
            """SELECT EXISTS (
                     SELECT 1 FROM connector_dispatch_outbox
                     WHERE broker_account_id = %s AND state = 'UNKNOWN'
                   ) OR EXISTS (
                     SELECT 1
                     FROM execution_state_snapshots snapshot,
                          jsonb_each(snapshot.state -> 'reconciliation_work') work
                     WHERE work.value ->> 'account_id' = %s
                       AND work.value ->> 'status' IN ('PENDING', 'ESCALATED')
                   )""",
            (account.id, account.id),
        )
        unresolved = bool(cursor.fetchone()[0])

        cursor.execute(
            """SELECT provider, broker_server, external_account_id, revoked_at
               FROM connector_bindings WHERE broker_account_id = %s FOR UPDATE""", (account.id,)
        )
        binding_row = cursor.fetchone()
        binding_identity = tuple(binding_row[:3]) if binding_row else None
        binding_revoked = bool(binding_row and binding_row[3] is not None)

        runtime_status = readiness.runtime_interlock
        runtime_reasons = tuple(readiness.runtime_reason_codes)
        recovery_ready = readiness.recovery_ready and not unresolved
        cursor.execute(
            """SELECT status, reason_codes, recovery_evidence
               FROM runtime_interlocks WHERE broker_account_id = %s FOR UPDATE""",
            (account.id,),
        )
        interlock_row = cursor.fetchone()
        if interlock_row is not None:
            runtime_status = str(interlock_row[0])
            runtime_reasons = tuple(str(item) for item in (interlock_row[1] or []))
        cursor.execute(
            """SELECT state -> 'accounts' -> %s
               FROM execution_state_snapshots WHERE snapshot_id = 1 FOR SHARE""",
            (account.id,),
        )
        snapshot_row = cursor.fetchone()
        if snapshot_row and isinstance(snapshot_row[0], dict) and interlock_row is None:
            execution_state = snapshot_row[0]
            runtime_status = str(execution_state.get("runtime_interlock", runtime_status))
            runtime_reasons = tuple(
                str(item) for item in execution_state.get("interlock_reasons", runtime_reasons)
            )
            recovery_ready = not bool(
                execution_state.get("recovery_required", not recovery_ready)
            ) and not unresolved

        return replace(
            readiness,
            binding_identity=binding_identity,
            binding_matches=bool(binding_identity == identity and not binding_revoked),
            binding_revoked=binding_revoked,
            connector_healthy=connector_status == "HEALTHY",
            lease_current=lease_current,
            generation_current=generation_current,
            reconciliation_complete=reconciliation_status == "COMPLETE",
            broker_facts_fresh=broker_facts_fresh,
            risk_limits_version=risk_version,
            pair_mappings=mappings,
            no_unknown=not unresolved,
            recovery_ready=recovery_ready,
            runtime_interlock=runtime_status,
            runtime_reason_codes=runtime_reasons,
        )

    def _command_db(
        self, account: Any, action: str, *, idempotency_key: str,
        expected_version: int, reason: str, actor: str,
        readiness: ReadinessContext | None, operation: str,
    ) -> LifecycleResult:
        rejected_error: str | None = None
        response_item: dict[str, Any] | None = None
        with self._lock_for(account.id):
            request_hash = self._hash_request(
                action, idempotency_key, expected_version, reason, actor, operation
            )
            with connect(self.database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT response_json, request_hash
                           FROM lifecycle_commands
                           WHERE broker_account_id = %s AND actor = %s
                             AND operation = %s AND idempotency_key = %s
                           FOR UPDATE""",
                        (account.id, actor, operation, idempotency_key),
                    )
                    previous = cursor.fetchone()
                    if previous:
                        if previous[1] != request_hash:
                            raise ValueError("IDEMPOTENCY_CONFLICT")
                        return self._result_from_item(dict(previous[0]), account, replayed=True)
                    cursor.execute(
                        """SELECT lifecycle_status, bot_state, execution_mode,
                                  environment, execution_epoch, version,
                                  connector_status, reconciliation_status,
                                  reconciliation_watermark
                           FROM broker_accounts WHERE id = %s FOR UPDATE""",
                        (account.id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise ValueError("WRONG_ACCOUNT")
                    # The account row is the serialization point. Recheck the
                    # idempotency row after acquiring it so two processes that
                    # use the same key cannot race into the unique index.
                    cursor.execute(
                        """SELECT response_json, request_hash
                           FROM lifecycle_commands
                           WHERE broker_account_id = %s AND actor = %s
                             AND operation = %s AND idempotency_key = %s
                           FOR UPDATE""",
                        (account.id, actor, operation, idempotency_key),
                    )
                    previous = cursor.fetchone()
                    if previous:
                        if previous[1] != request_hash:
                            raise ValueError("IDEMPOTENCY_CONFLICT")
                        return self._result_from_item(dict(previous[0]), account, replayed=True)
                    interlock = (
                        self.execution.runtime_interlock(account.id)
                        if self.execution is not None else None
                    )
                    prior_state = {
                        "lifecycle_status": row[0], "bot_state": row[1],
                        "execution_mode": row[2], "environment": row[3],
                        "account_version": int(row[5]), "execution_epoch": int(row[4]),
                        "runtime_interlock": interlock.status if interlock else "UNKNOWN",
                        "interlock_reason_codes": list(interlock.reasons) if interlock else [],
                        "interlock_evidence": dict(interlock.evidence) if interlock else {},
                    }
                    baseline = readiness or ReadinessContext(
                        None, False, False, False, False, "BLOCKED", (), False,
                    )
                    authoritative = self._db_readiness(cursor, account, baseline)
                    prior_state["runtime_interlock"] = authoritative.runtime_interlock
                    prior_state["interlock_reason_codes"] = list(
                        authoritative.runtime_reason_codes
                    )
                    effective = self._transition_readiness(authoritative, action)
                    validation_account = copy.copy(account)
                    validation_account.lifecycle_status = row[0]
                    validation_account.bot_state = row[1]
                    validation_account.execution_mode = row[2]
                    validation_account.environment = row[3]
                    validation_account.version = int(row[5])
                    validation_account.execution_epoch = int(row[4])
                    error = self._validate_transition(validation_account, action)
                    if expected_version != int(row[5]):
                        error = error or "STALE_VERSION"
                    if row[3] != "DEMO":
                        error = error or "DEMO_ONLY"
                    if row[2] != "MANUAL":
                        error = error or "MANUAL_MODE_REQUIRED"
                    if action in {"enable", "start"} and not effective.allowed:
                        error = error or ("READINESS:" + ",".join(effective.reason_codes))
                    command_id, audit_id = str(uuid4()), str(uuid4())
                    created = _now()
                    status = "REJECTED" if error else "ACCEPTED"
                    new_epoch = int(row[4]) + (1 if not error and action in {"stop", "disable"} else 0)
                    new_state = dict(prior_state)
                    if not error:
                        if action in {"enable", "start"}:
                            new_interlock = effective.runtime_interlock
                            new_reasons = list(effective.runtime_reason_codes)
                        elif action in {"stop", "disable"}:
                            new_interlock = (
                                "QUARANTINED"
                                if authoritative.runtime_interlock == "QUARANTINED"
                                else "BLOCKED"
                            )
                            new_reasons = list(authoritative.runtime_reason_codes)
                            if "LIFECYCLE_STOPPED" not in new_reasons:
                                new_reasons.append("LIFECYCLE_STOPPED")
                        else:
                            new_interlock = authoritative.runtime_interlock
                            new_reasons = list(authoritative.runtime_reason_codes)
                        new_state.update({
                            "lifecycle_status": "ENABLED" if action != "disable" else "DISABLED",
                            "bot_state": "RUNNING" if action == "start" else "STOPPED",
                            "account_version": int(row[5]) + 1,
                            "execution_epoch": new_epoch,
                            "runtime_interlock": new_interlock,
                            "interlock_reason_codes": new_reasons,
                            "interlock_evidence": dict(
                                interlock.evidence if interlock else {}
                            ),
                        })
                    response_item = {
                        "account_id": account.id, "actor": actor, "operation": operation,
                        "idempotency_key": idempotency_key, "request_hash": request_hash,
                        "command_id": command_id, "audit_id": audit_id, "action": action,
                        "status": status, "expected_version": expected_version,
                        "observed_version": new_state["account_version"],
                        "execution_epoch": new_epoch, "reason": reason,
                        "rejection_code": error, "prior_state": prior_state,
                        "new_state": new_state,
                        "readiness_payload": self._readiness_payload(effective),
                        "created_at": _iso(created) or "", "completed_at": _iso(_now()) or "",
                    }
                    if not error:
                        cursor.execute(
                            """UPDATE broker_accounts SET lifecycle_status = %s,
                               bot_state = %s, execution_epoch = %s, version = %s,
                               updated_at = now() WHERE id = %s""",
                            (new_state["lifecycle_status"], new_state["bot_state"],
                             new_epoch, new_state["account_version"], account.id),
                        )
                        cursor.execute(
                            """INSERT INTO execution_accounts
                               (broker_account_id, execution_epoch, exposure_gate)
                               VALUES (%s, %s, %s)
                               ON CONFLICT (broker_account_id) DO UPDATE
                               SET execution_epoch = EXCLUDED.execution_epoch,
                                   exposure_gate = EXCLUDED.exposure_gate, updated_at = now()""",
                            (account.id, new_epoch, "OPEN" if action == "start" else "STOPPED"),
                        )
                        if action in {"stop", "disable"}:
                            self._fence_postgres_entries(cursor, account.id, new_epoch)
                        self._fence_snapshot(
                            cursor, account.id, new_epoch,
                            exposure_gate="OPEN" if action == "start" else "STOPPED",
                        )
                    cursor.execute(
                        """INSERT INTO lifecycle_commands
                           (id, broker_account_id, action, idempotency_key, request_hash,
                            expected_version, observed_version, execution_epoch, status,
                            audit_id, actor, operation, reason, readiness_reason_codes,
                            prior_state, new_state, response_json, created_at, completed_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                   %s, %s, %s, %s, %s, %s, %s)""",
                        (command_id, account.id, action, idempotency_key, request_hash,
                         expected_version, new_state["account_version"], new_epoch, status,
                         audit_id, actor, operation, reason, Jsonb(list(effective.reason_codes)),
                         Jsonb(prior_state), Jsonb(new_state), Jsonb(response_item),
                         created, _parse_time(response_item["completed_at"])),
                    )
                    cursor.execute(
                        """INSERT INTO lifecycle_audit
                           (id, broker_account_id, event_type, reason, actor, status, payload)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (audit_id, account.id, f"lifecycle.{action}", reason, actor,
                         status, Jsonb(response_item)),
                    )
                    rejected_error = error
            if rejected_error:
                raise _LifecycleRejected(rejected_error)
            assert response_item is not None
            self._apply_committed_projection(account, response_item, persist_execution=False)
            return self._result_from_item(response_item, account, replayed=False)

    @staticmethod
    def _fence_postgres_entries(cursor: Any, account_id: str, epoch: int) -> tuple[str, ...]:
        cursor.execute(
            """UPDATE connector_dispatch_outbox
               SET state = 'REJECTED',
                   result_payload = '{"state":"REJECTED","code":"LIFECYCLE_FENCE"}'::jsonb,
                   updated_at = now()
               WHERE broker_account_id = %s AND state = 'QUEUED'
                 AND execution_epoch < %s AND command_type = 'order.submit_market'
               RETURNING command_id""",
            (account_id, epoch),
        )
        fenced_ids = tuple(str(row[0]) for row in cursor.fetchall())
        if not fenced_ids:
            return ()
        fenced_uuid = [UUID(value) for value in fenced_ids]
        cursor.execute(
            """UPDATE order_intents oi SET status = 'REJECTED'
               FROM connector_dispatch_outbox d
               WHERE d.broker_account_id = oi.broker_account_id
                 AND d.command_id = oi.command_id AND d.broker_account_id = %s
                 AND d.command_id = ANY(%s)
                 AND d.state = 'REJECTED' AND oi.status IN ('INTENT','CHECKED','DISPATCHING')""",
            (account_id, fenced_uuid),
        )
        cursor.execute(
            """UPDATE outbox_events e SET status = 'ABORTED'
               FROM connector_dispatch_outbox d JOIN order_intents oi ON oi.command_id = d.command_id
               WHERE e.order_intent_id = oi.id AND d.broker_account_id = %s
                 AND d.command_id = ANY(%s) AND d.state = 'REJECTED'""",
            (account_id, fenced_uuid),
        )
        cursor.execute(
            """UPDATE connector_journal j SET state = 'ABORTED_NOT_INVOKED'
               FROM connector_dispatch_outbox d JOIN order_intents oi ON oi.command_id = d.command_id
               WHERE j.order_intent_id = oi.id AND d.broker_account_id = %s
                 AND d.command_id = ANY(%s) AND d.state = 'REJECTED'""",
            (account_id, fenced_uuid),
        )
        cursor.execute(
            """UPDATE risk_reservations r SET status = 'RELEASED'
               FROM connector_dispatch_outbox d JOIN order_intents oi ON oi.command_id = d.command_id
               WHERE r.id = oi.risk_reservation_id AND d.broker_account_id = %s
                 AND d.command_id = ANY(%s) AND d.state = 'REJECTED'""",
            (account_id, fenced_uuid),
        )
        return fenced_ids

    @staticmethod
    def _fence_snapshot(
        cursor: Any, account_id: str, epoch: int, *, exposure_gate: str = "STOPPED",
    ) -> None:
        cursor.execute("SELECT state FROM execution_state_snapshots WHERE snapshot_id = 1 FOR UPDATE")
        row = cursor.fetchone()
        if not row or not isinstance(row[0], dict):
            return
        state = copy.deepcopy(row[0])
        fenced: set[str] = set()
        for command_id, record in state.get("dispatch_records", {}).items():
            if (record.get("account_id") == account_id and record.get("state") == "QUEUED"
                    and int(record.get("execution_epoch", 0)) < epoch
                    and record.get("command_type") == "order.submit_market"):
                record["state"] = "FENCED"
                record["result_payload"] = {"state": "REJECTED", "code": "LIFECYCLE_FENCE"}
                fenced.add(command_id)
        for order in state.get("orders", {}).values():
            if order.get("command_id") in fenced:
                order["status"] = "REJECTED"
        for event in state.get("events", {}).values():
            order = state.get("orders", {}).get(event.get("order_id"), {})
            if order.get("command_id") in fenced:
                event["status"] = "ABORTED"
        for journal in state.get("journal", {}).values():
            order = state.get("orders", {}).get(journal.get("order_id"), {})
            if order.get("command_id") in fenced:
                journal["state"] = "ABORTED_NOT_INVOKED"
        fenced_order_ids = {
            order_id for order_id, order in state.get("orders", {}).items()
            if order.get("command_id") in fenced
        }
        fenced_reservation_ids = {
            reservation_id for order_id, reservation_id
            in state.get("order_reservations", {}).items()
            if order_id in fenced_order_ids
        }
        for reservation_id in fenced_reservation_ids:
            reservation = state.get("reservations", {}).get(reservation_id)
            if reservation is not None and reservation.get("status") == "ACTIVE":
                reservation["status"] = "RELEASED"
        state.setdefault("accounts", {}).setdefault(
            account_id, {"account_id": account_id}
        )["execution_epoch"] = epoch
        state["accounts"][account_id]["exposure_gate"] = exposure_gate
        cursor.execute(
            "UPDATE execution_state_snapshots SET state = %s, updated_at = now() WHERE snapshot_id = 1",
            (Jsonb(state),),
        )

    def _apply_committed_projection(
        self, account: Any, item: dict[str, Any], *, persist_execution: bool = True,
    ) -> None:
        state = item["new_state"]
        account.lifecycle_status = state["lifecycle_status"]
        account.bot_state = state["bot_state"]
        account.version = int(state["account_version"])
        account.execution_epoch = int(state["execution_epoch"])
        if self.execution is not None and hasattr(self.execution, "apply_lifecycle_commit"):
            self.execution.apply_lifecycle_commit(
                account.id, account.execution_epoch,
                allowed=item.get("action") == "start",
                persist=persist_execution,
            )
            if item.get("action") == "enable":
                execution_state = self.execution.account(account.id)
                execution_state.exposure_gate = "STOPPED"
                execution_state.interlock_reasons = tuple(
                    reason for reason in execution_state.interlock_reasons
                    if reason != "LIFECYCLE_STOPPED"
                )
                if not execution_state.interlock_reasons:
                    execution_state.runtime_interlock = "ELIGIBLE"
                if persist_execution:
                    self.execution._save()

    def audits(self, account_id: str) -> list[dict[str, Any]]:
        if self.database_url:
            with connect(self.database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT payload FROM lifecycle_audit WHERE broker_account_id = %s ORDER BY created_at",
                        (account_id,),
                    )
                    return [dict(row[0]) for row in cursor.fetchall()]
        return list(self._audits.get(account_id, []))


class _LifecycleRejected(ValueError):
    """Raised after a rejected command has been committed as durable evidence."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
