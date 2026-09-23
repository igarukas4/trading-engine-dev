"""Account lifecycle commands and durable readiness facts.

The coordinator is deliberately broker-free.  It persists the command/audit
projection locally for the single-process reference deployment and keeps every
fact keyed by BrokerAccount id.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from psycopg import connect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def _now() -> datetime:
    return datetime.now(timezone.utc)


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

    @property
    def allowed(self) -> bool:
        return bool(
            self.binding_identity
            and self.binding_matches
            and self.connector_healthy
            and self.lease_current
            and self.reconciliation_complete
            and self.no_unknown
            and self.runtime_interlock == "ELIGIBLE"
            and not self.runtime_reason_codes
            and self.recovery_ready
            and self.risk_limits_version is not None
            and self.pair_mappings
        )

    @property
    def reason_codes(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.binding_identity: reasons.append("CONNECTOR_BINDING_MISSING")
        elif not self.binding_matches: reasons.append("CONNECTOR_BINDING_MISMATCH")
        if not self.connector_healthy: reasons.append("CONNECTOR_UNHEALTHY")
        if not self.lease_current: reasons.append("CONNECTOR_LEASE_STALE")
        if not self.reconciliation_complete: reasons.append("BROKER_FACTS_STALE")
        if not self.no_unknown: reasons.append("UNKNOWN_COMMANDS_PRESENT")
        if self.runtime_interlock != "ELIGIBLE": reasons.append("RUNTIME_INTERLOCK_BLOCKED")
        reasons.extend(self.runtime_reason_codes)
        if not self.recovery_ready: reasons.append("RECOVERY_INCOMPLETE")
        if self.facts_complete and self.risk_limits_version is None: reasons.append("RISK_LIMITS_MISSING")
        if self.facts_complete and not self.pair_mappings: reasons.append("PAIR_MAPPING_MISSING")
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


class LifecycleCoordinator:
    def __init__(self, state_path: str | os.PathLike[str] | None = None, database_url: str = "") -> None:
        self.state_path = Path(state_path) if state_path else None
        self.database_url = database_url
        self._commands: dict[tuple[str, str], dict[str, Any]] = {}
        self._audits: dict[str, list[dict[str, Any]]] = {}
        self._facts: dict[str, dict[str, Any]] = {}
        self._load()
        if self.database_url:
            self._ensure_schema()
            self._db_load()

    def _ensure_schema(self) -> None:
        with connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM lifecycle_readiness LIMIT 1")

    def _db_load(self) -> None:
        if not self.database_url:
            return
        with connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT broker_account_id, risk_limits_version, pair_mappings FROM lifecycle_readiness")
                for account_id, risk_version, mappings in cursor.fetchall():
                    self._facts[str(account_id)] = {"risk_limits_version": risk_version, "pair_mappings": mappings or {}}
                cursor.execute("SELECT broker_account_id, idempotency_key, id, audit_id, action, status, request_hash, observed_version, execution_epoch, actor, reason, created_at FROM lifecycle_commands")
                for row in cursor.fetchall():
                    item = {"account_id": str(row[0]), "idempotency_key": row[1], "command_id": str(row[2]), "audit_id": str(row[3]), "action": row[4], "status": row[5], "request_hash": row[6], "observed_version": row[7], "execution_epoch": row[8], "actor": row[9], "reason": row[10], "created_at": row[11].isoformat()}
                    self._commands[(item["account_id"], item["idempotency_key"])] = item

    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        for item in raw.get("commands", []):
            self._commands[(item["account_id"], item["idempotency_key"])] = item
        self._audits = {key: list(value) for key, value in raw.get("audits", {}).items()}
        self._facts = dict(raw.get("readiness", {}))

    def _save(self) -> None:
        if not self.state_path:
            return
        payload = {
            "commands": list(self._commands.values()),
            "audits": self._audits,
            "readiness": self._facts,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.state_path.name}.", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def record_risk_limits(self, account_id: str, *, version: int) -> None:
        self._facts.setdefault(account_id, {})["risk_limits_version"] = version
        self._save()
        if self.database_url:
            with connect(self.database_url) as connection, connection.cursor() as cursor:
                cursor.execute("INSERT INTO lifecycle_readiness (broker_account_id, risk_limits_version) VALUES (%s,%s) ON CONFLICT (broker_account_id) DO UPDATE SET risk_limits_version=EXCLUDED.risk_limits_version, updated_at=now()", (account_id, version))

    def record_pair_mapping(self, account_id: str, pair: str, broker_symbol: str, *, valid: bool) -> None:
        facts = self._facts.setdefault(account_id, {})
        mappings = facts.setdefault("pair_mappings", {})
        if valid:
            mappings[pair] = broker_symbol
        else:
            mappings.pop(pair, None)
        self._save()
        if self.database_url:
            with connect(self.database_url) as connection, connection.cursor() as cursor:
                cursor.execute("INSERT INTO lifecycle_readiness (broker_account_id, pair_mappings) VALUES (%s,%s) ON CONFLICT (broker_account_id) DO UPDATE SET pair_mappings=EXCLUDED.pair_mappings, updated_at=now()", (account_id, json.dumps(mappings)))

    def readiness_facts(self, account_id: str) -> ReadinessFacts:
        facts = self._facts.get(account_id, {})
        return ReadinessFacts(facts.get("risk_limits_version"), dict(facts.get("pair_mappings", {})))

    def restore_account(self, account: Any) -> Any:
        """Restore the last accepted lifecycle projection after a restart."""
        items = [item for item in self._commands.values() if item["account_id"] == account.id]
        if not items:
            return account
        latest = max(items, key=lambda item: item["created_at"])
        action = latest["action"]
        if action == "enable":
            account.lifecycle_status, account.bot_state, account.version = "ENABLED", "STOPPED", latest.get("observed_version", 2)
        elif action == "start":
            account.lifecycle_status, account.bot_state = "ENABLED", "RUNNING"
        elif action == "stop":
            account.bot_state = "STOPPED"
        elif action == "disable":
            account.lifecycle_status, account.bot_state = "DISABLED", "STOPPED"
        return account

    def readiness_context(self, account: Any, binding: Any, execution: Any) -> ReadinessContext:
        facts = self.readiness_facts(account.id)
        now = _now()
        lease_current = bool(account.lease_owner and account.lease_expires_at and account.lease_expires_at > now)
        decision = execution.runtime_interlock(account.id)
        no_unknown = not any(
            record.account_id == account.id and record.state == "UNKNOWN"
            for record in execution.dispatch_records.values()
        ) and not any(
            item.get("status") in {"PENDING", "ESCALATED"}
            for item in execution.recovery_records(account.id)
        )
        return ReadinessContext(
            binding_identity=(binding.provider, binding.broker_server, binding.external_account_id) if binding else None,
            binding_matches=bool(binding and (binding.provider, binding.broker_server, binding.external_account_id) == account.identity and binding.account_id == account.id),
            connector_healthy=account.connector_healthy,
            lease_current=lease_current,
            reconciliation_complete=account.reconciliation_complete,
            no_unknown=no_unknown,
            runtime_interlock=decision.status,
            runtime_reason_codes=decision.reasons,
            recovery_ready=not execution.account(account.id).recovery_required,
            risk_limits_version=facts.risk_limits_version,
            pair_mappings=facts.pair_mappings,
            facts_complete=True,
        )

    def _result(self, item: dict[str, Any], account: Any, readiness: ReadinessContext, *, replayed: bool) -> LifecycleResult:
        snapshot = item.get("result_snapshot", {})
        return LifecycleResult(
            command_id=item["command_id"], audit_id=item["audit_id"], status=item["status"], action=item["action"],
            lifecycle_status=snapshot.get("lifecycle_status", account.lifecycle_status),
            bot_state=snapshot.get("bot_state", account.bot_state),
            account_version=snapshot.get("account_version", account.version),
            execution_epoch=snapshot.get("execution_epoch", account.execution_epoch),
            readiness=readiness, replayed=replayed,
        )

    def command(self, account: Any, action: str, *, idempotency_key: str, expected_version: int,
                reason: str, actor: str, readiness: ReadinessContext, fence: Any) -> LifecycleResult:
        if action not in {"enable", "start", "stop", "disable"}:
            raise ValueError("UNKNOWN_LIFECYCLE_ACTION")
        payload = json.dumps({"action": action, "expected_version": expected_version, "reason": reason}, sort_keys=True)
        request_hash = hashlib.sha256(payload.encode()).hexdigest()
        key = (account.id, idempotency_key)
        previous = self._commands.get(key)
        if previous:
            if previous["request_hash"] != request_hash:
                conflict_id = str(uuid4())
                self._audits.setdefault(account.id, []).append({"id": conflict_id, "event_type": "lifecycle.idempotency_conflict", "reason": reason, "actor": actor, "status": "REJECTED", "idempotency_key": idempotency_key})
                self._save()
                raise ValueError("IDEMPOTENCY_CONFLICT")
            return self._result(previous, account, readiness, replayed=True)
        if expected_version != account.version:
            raise ValueError("STALE_VERSION")
        if account.environment != "DEMO":
            raise ValueError("DEMO_ONLY")
        if account.execution_mode != "MANUAL":
            raise ValueError("MANUAL_MODE_REQUIRED")
        if account.lifecycle_status == "ARCHIVED":
            raise ValueError("ARCHIVED_ACCOUNT")
        if action == "enable" and (account.lifecycle_status != "DISABLED" or account.bot_state != "STOPPED"):
            raise ValueError("INVALID_ENABLE_TRANSITION")
        if action == "start" and (account.lifecycle_status != "ENABLED" or account.bot_state != "STOPPED"):
            raise ValueError("INVALID_START_TRANSITION")
        if action == "stop" and account.bot_state == "STOPPED":
            raise ValueError("ALREADY_STOPPED")
        if action == "disable" and account.bot_state != "STOPPED":
            raise ValueError("DISABLE_REQUIRES_STOPPED")
        if action in {"enable", "start"} and not readiness.allowed:
            raise ValueError("READINESS:" + ",".join(readiness.reason_codes))
        if action == "enable":
            # Project durable readiness facts onto the account's existing
            # coarse enable gate before invoking its invariant.
            account.connector_bound = readiness.binding_identity is not None
            account.connector_healthy = readiness.connector_healthy
            account.reconciliation_complete = readiness.reconciliation_complete
            account.risk_limits_active = (
                readiness.risk_limits_version is not None
            )
            account.mappings_valid = bool(readiness.pair_mappings)
            account.enable()
        elif action == "start":
            if account.lifecycle_status != "ENABLED":
                raise ValueError("LIFECYCLE_DISABLED")
            account.bot_state = "RUNNING"
            account.version += 1
            fence(True)
        elif action == "stop":
            account.bot_state = "STOPPED"
            account.execution_epoch += 1
            account.version += 1
            fence(False)
        else:
            account.lifecycle_status = "DISABLED"
            account.bot_state = "STOPPED"
            account.execution_epoch += 1
            account.version += 1
            fence(False)
        command_id, audit_id = str(uuid4()), str(uuid4())
        item = {"account_id": account.id, "idempotency_key": idempotency_key, "request_hash": request_hash,
                "command_id": command_id, "audit_id": audit_id, "action": action, "status": "ACCEPTED",
                "actor": actor, "reason": reason, "observed_version": account.version,
                "execution_epoch": account.execution_epoch, "created_at": _now().isoformat()}
        item["result_snapshot"] = {"lifecycle_status": account.lifecycle_status, "bot_state": account.bot_state,
                                   "account_version": account.version, "execution_epoch": account.execution_epoch}
        self._commands[key] = item
        self._audits.setdefault(account.id, []).append({"id": audit_id, "event_type": f"lifecycle.{action}", "reason": reason, "actor": actor})
        self._save()
        if self.database_url:
            with connect(self.database_url) as connection, connection.cursor() as cursor:
                cursor.execute("UPDATE broker_accounts SET lifecycle_status=%s, bot_state=%s, execution_epoch=%s, version=%s, updated_at=now() WHERE id=%s", (account.lifecycle_status, account.bot_state, account.execution_epoch, account.version, account.id))
                cursor.execute("INSERT INTO lifecycle_commands (id, broker_account_id, action, idempotency_key, request_hash, expected_version, observed_version, execution_epoch, status, audit_id, actor, reason, readiness_reason_codes, prior_state, new_state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (command_id, account.id, action, idempotency_key, request_hash, expected_version, account.version - 1, account.execution_epoch, "ACCEPTED", audit_id, actor, reason, json.dumps(readiness.reason_codes), json.dumps({}), json.dumps({"lifecycle_status": account.lifecycle_status, "bot_state": account.bot_state})))
                cursor.execute("INSERT INTO lifecycle_audit (id, broker_account_id, event_type, reason, actor, status, payload) VALUES (%s,%s,%s,%s,%s,%s,%s)", (audit_id, account.id, f"lifecycle.{action}", reason, actor, "ACCEPTED", json.dumps(item)))
        return self._result(item, account, readiness, replayed=False)

    def audits(self, account_id: str) -> list[dict[str, Any]]:
        return list(self._audits.get(account_id, []))
