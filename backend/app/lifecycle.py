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
    facts_complete: bool = False

    @property
    def allowed(self) -> bool:
        return bool(
            self.binding_identity
            and self.connector_healthy
            and self.lease_current
            and self.reconciliation_complete
            and self.no_unknown
            and self.runtime_interlock == "ELIGIBLE"
            and not self.runtime_reason_codes
            and self.recovery_ready
            and (not self.facts_complete or (self.risk_limits_version is not None and self.pair_mappings))
        )


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
    def __init__(self, state_path: str | os.PathLike[str] | None = None) -> None:
        self.state_path = Path(state_path) if state_path else None
        self._commands: dict[tuple[str, str], dict[str, Any]] = {}
        self._audits: dict[str, list[dict[str, Any]]] = {}
        self._facts: dict[str, dict[str, Any]] = {}
        self._load()

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

    def record_pair_mapping(self, account_id: str, pair: str, broker_symbol: str, *, valid: bool) -> None:
        facts = self._facts.setdefault(account_id, {})
        mappings = facts.setdefault("pair_mappings", {})
        if valid:
            mappings[pair] = broker_symbol
        else:
            mappings.pop(pair, None)
        self._save()

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
        return LifecycleResult(
            command_id=item["command_id"], audit_id=item["audit_id"], status=item["status"], action=item["action"],
            lifecycle_status=account.lifecycle_status, bot_state=account.bot_state,
            account_version=account.version, execution_epoch=account.execution_epoch,
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
                raise ValueError("IDEMPOTENCY_CONFLICT")
            return self._result(previous, account, readiness, replayed=True)
        if expected_version != account.version:
            raise ValueError("STALE_VERSION")
        if action in {"enable", "start"} and not readiness.allowed:
            raise ValueError("NOT_READY")
        if action == "enable":
            # Project durable readiness facts onto the account's existing
            # coarse enable gate before invoking its invariant.
            account.connector_bound = readiness.binding_identity is not None
            account.connector_healthy = readiness.connector_healthy
            account.reconciliation_complete = readiness.reconciliation_complete
            account.risk_limits_active = (
                readiness.risk_limits_version is not None or not readiness.facts_complete
            )
            account.mappings_valid = bool(readiness.pair_mappings) or not readiness.facts_complete
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
        self._commands[key] = item
        self._audits.setdefault(account.id, []).append({"id": audit_id, "event_type": f"lifecycle.{action}", "reason": reason, "actor": actor})
        self._save()
        return self._result(item, account, readiness, replayed=False)

    def audits(self, account_id: str) -> list[dict[str, Any]]:
        return list(self._audits.get(account_id, []))
