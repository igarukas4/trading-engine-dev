"""Account-local execution safety substrate.

The broker connector is deliberately an injected dependency.  This module owns
the durable-domain decisions around it: reservations and intents are created
before dispatch, broker ambiguity is journaled as UNKNOWN, and recovery asks
the journal/broker for truth before any further side effect.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal, Protocol
from uuid import uuid4

from .risk_calendar import RiskAssessment


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Treat naive timestamps as UTC and normalize aware timestamps."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


RUNTIME_HEALTH_REASONS = frozenset(
    {
        "CONNECTOR_UNAVAILABLE",
        "CONNECTOR_GAP",
        "BROKER_FACTS_STALE",
        "RECONCILIATION_FAILED",
        "RISK_STATE_UNCERTAIN",
        "RESERVATION_INCONSISTENT",
        "ORDERING_OVERLOAD",
        "BACKUP_RECOVERY_POINT_STALE",
        "CRITICAL_ALERT_DELIVERY_STALE",
    }
)
CONNECTOR_HEALTH_REASONS = frozenset(
    {"CONNECTOR_UNAVAILABLE", "CONNECTOR_GAP"}
)
PROTECTION_INTERLOCK_REASONS = frozenset(
    {"NATIVE_PROTECTION_UNCONFIRMED", "NATIVE_PROTECTION_CHANGED"}
)
RESTART_RECOVERY_REASON = "RESTART_RECONCILIATION_REQUIRED"
UNRESOLVED_RECONCILIATION_STATUSES = frozenset({"PENDING", "ESCALATED"})
COMPLETE_RECONCILIATION_SECTIONS = frozenset({"orders", "fills", "positions"})


def canonical_order_hash(
    order_payload: dict[str, Any], signal_revision: int, risk_amount: str | Decimal
) -> str:
    """Hash every caller-controlled input that can change an order decision."""
    return json.dumps(
        {
            "order_payload": order_payload,
            "risk_amount": str(risk_amount),
            "signal_revision": signal_revision,
        },
        sort_keys=True,
    )


class ExecutionError(ValueError):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


@dataclass
class AccountExecutionState:
    account_id: str
    next_dispatch_sequence: int = 1
    execution_epoch: int = 1
    exposure_gate: Literal[
        "OPEN", "FENCE_PENDING", "QUARANTINED", "STOPPED"
    ] = "OPEN"
    fence_sequence: int = 0
    runtime_interlock: Literal["ELIGIBLE", "BLOCKED", "QUARANTINED"] = "ELIGIBLE"
    interlock_reasons: tuple[str, ...] = ()
    interlock_evidence: dict[str, Any] = field(default_factory=dict)
    quarantine_requires_command: bool = False
    interlock_updated_at: datetime | None = None
    recovery_status: Literal["READY", "RECOVERING"] = "READY"
    recovery_required: bool = False
    recovery_gate_before: Literal[
        "OPEN", "FENCE_PENDING", "QUARANTINED", "STOPPED"
    ] = "OPEN"
    recovery_started_at: datetime | None = None
    recovery_completed_at: datetime | None = None


@dataclass
class RiskReservation:
    id: str
    account_id: str
    signal_id: str
    amount: str = "0"
    status: Literal["ACTIVE", "CONSUMED", "RELEASED"] = "ACTIVE"


@dataclass
class OrderIntent:
    id: str
    account_id: str
    signal_id: str
    idempotency_key: str
    canonical_hash: str
    execution_epoch: int
    dispatch_sequence: int
    payload: dict[str, Any]
    status: Literal[
        "INTENT",
        "CHECKED",
        "DISPATCHING",
        "SUBMITTED",
        "PARTIALLY_FILLED",
        "REJECTED",
        "UNKNOWN",
        "FILLED",
        "CANCELLED",
    ] = "INTENT"
    external_id: str | None = None
    requested_volume: str | None = None
    cumulative_filled_volume: str = "0"
    remaining_volume: str | None = None
    risk_assessment_id: str | None = None
    command_id: str | None = None


@dataclass
class OutboxEvent:
    id: str
    account_id: str
    order_id: str
    dispatch_sequence: int
    status: Literal["PENDING", "DISPATCHING", "PUBLISHED", "ABORTED"] = "PENDING"


@dataclass
class ConnectorDispatchRecord:
    """Durable backend-to-connector delivery attempt."""

    command_id: str
    account_id: str
    identity: dict[str, str]
    command_type: str
    dispatch_sequence: int
    generation: int
    execution_epoch: int
    idempotency_key: str
    request_hash: str
    payload: dict[str, Any]
    result_payload: dict[str, Any] | None = None
    state: Literal["QUEUED", "SENT", "ACCEPTED", "REJECTED", "UNKNOWN"] = "QUEUED"


@dataclass
class ConnectorJournalEntry:
    id: str
    account_id: str
    order_id: str
    dispatch_sequence: int
    state: Literal[
        "PREPARED",
        "DISPATCHING",
        "ACCEPTED",
        "REJECTED",
        "ABORTED_NOT_INVOKED",
    ] = "PREPARED"
    external_id: str | None = None
    observed_at: datetime = field(default_factory=_now)


@dataclass
class Fill:
    id: str
    account_id: str
    order_id: str
    external_deal_id: str
    volume: str
    native_protection_confirmed: bool


@dataclass
class Position:
    account_id: str
    order_id: str
    volume: str
    protection_status: Literal["CONFIRMED", "UNCONFIRMED", "QUARANTINED"]
    remaining_volume: str | None = None
    stage: Literal["ENTRY", "TP1_CONFIRMED", "TP2_CONFIRMED", "CLOSING", "CLOSED"] = "ENTRY"
    runner_volume: str | None = None
    native_stop_loss: str | None = None
    native_take_profit: str | None = None
    last_confirmed_stop: str | None = None
    accounting_mode: Literal["NETTING", "HEDGING"] = "NETTING"
    external_position_id: str | None = None
    pair: str | None = None
    direction: Literal["LONG", "SHORT"] | None = None
    entry_price: str | None = None
    current_pnl: str | None = None
    data_status: Literal["CONFIRMED", "UNKNOWN", "STALE"] = "UNKNOWN"
    version: int = 0

    def __post_init__(self) -> None:
        if self.remaining_volume is None:
            self.remaining_volume = self.volume


@dataclass
class PositionCommand:
    id: str
    account_id: str
    order_id: str
    command_type: Literal["TP1", "TP2", "TRAIL", "CLOSE", "PROTECTION"]
    requested_volume: str | None
    reduce_only: bool = True
    status: Literal["RECEIVED", "DISPATCHING", "CONFIRMED", "UNKNOWN", "REJECTED"] = "RECEIVED"
    requested_stop: str | None = None
    requested_take_profit: str | None = None
    confirmed_stop: str | None = None
    reason: str | None = None
    idempotency_key: str | None = None


@dataclass
class DispatchResult:
    order_id: str
    status: str
    reason: str | None = None


@dataclass
class SafetyFence:
    account_id: str
    sequence: int
    kind: str
    status: Literal["FENCE_PENDING", "ACKNOWLEDGED"] = "FENCE_PENDING"


@dataclass
class PreOrderResult:
    reservation: RiskReservation
    order: OrderIntent
    outbox_event: OutboxEvent


class BrokerAdapter(Protocol):
    """Capability-based broker boundary used by Execution Coordination."""

    def order_check(self, order: OrderIntent) -> bool: ...

    def order_send(self, order: OrderIntent) -> Any: ...


Broker = BrokerAdapter


@dataclass(frozen=True)
class AuditEvent:
    id: str
    account_id: str
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime


@dataclass(frozen=True)
class ReconciliationResult:
    account_id: str
    status: str
    applied_fill_ids: tuple[str, ...] = ()
    duplicate_fill_ids: tuple[str, ...] = ()
    position_ids: tuple[str, ...] = ()


@dataclass
class ReconciliationWork:
    """Durable, account-local follow-up for an ambiguous broker effect."""

    id: str
    account_id: str
    subject_id: str
    subject_kind: Literal["ORDER", "POSITION_COMMAND", "COMMAND"]
    first_seen_at: datetime
    deadline_at: datetime
    status: Literal["PENDING", "RECOVERED", "ESCALATED"] = "PENDING"
    attempts: int = 0
    last_attempt_at: datetime | None = None
    reason: str = "CONNECTOR_RESULT_AMBIGUOUS"
    entry_gate_before: Literal["OPEN", "FENCE_PENDING", "QUARANTINED", "STOPPED"] = "OPEN"


@dataclass(frozen=True)
class RuntimeInterlockDecision:
    """Account-local decision about whether new exposure may be accepted."""

    account_id: str
    status: Literal["ELIGIBLE", "BLOCKED", "QUARANTINED"]
    reasons: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
    requires_custodian_command: bool = False

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return self.reasons

    @property
    def recovery_evidence(self) -> dict[str, Any]:
        return self.evidence


@dataclass(frozen=True)
class ProtectionRepairResult:
    account_id: str
    position_id: str
    status: Literal["RECOVERED", "QUARANTINED"]
    attempts: int
    reason_code: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class CriticalAlert:
    id: str
    account_id: str
    reason_code: str
    detail: str
    evidence: dict[str, Any]
    status: Literal["OPEN", "RESOLVED"] = "OPEN"
    created_at: datetime = field(default_factory=_now)
    resolved_at: datetime | None = None


class ExecutionStateStore(Protocol):
    def load(self) -> dict[str, Any] | None: ...

    def save(self, state: dict[str, Any]) -> None: ...


class JsonExecutionStore:
    """Small atomic store for local restart tests and single-process dev runs."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


DurableExecutionStore = JsonExecutionStore


class PostgresExecutionStore:
    """PostgreSQL-backed checkpoint store used by the API process."""

    def __init__(self, database_url: str):
        self.database_url = database_url

    def load(self) -> dict[str, Any] | None:
        from psycopg import connect

        with connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT state FROM execution_state_snapshots WHERE snapshot_id = 1"
                )
                row = cursor.fetchone()
        return row[0] if row else None

    def save(self, state: dict[str, Any]) -> None:
        from psycopg import connect
        from psycopg.types.json import Jsonb

        with connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO execution_state_snapshots (snapshot_id, state)
                       VALUES (1, %s)
                       ON CONFLICT (snapshot_id) DO UPDATE
                       SET state = EXCLUDED.state, updated_at = now()""",
                    (Jsonb(state),),
                )


OperatorCommandKind = Literal[
    "APPROVE_SIGNAL", "EXECUTE_SIGNAL", "CLOSE_ALL", "CANCEL_ORDER"
]


@dataclass
class GlobalEmergencyTarget:
    operation_id: str
    account_id: str
    requested_kind: Literal["STOP_ONLY", "CLOSE_ALL"]
    status: Literal["PENDING", "CONVERGED", "UNRESOLVED"] = "PENDING"
    detail: str | None = None


@dataclass
class GlobalEmergencyOperation:
    id: str
    requested_kind: Literal["STOP_ONLY", "CLOSE_ALL"]
    target_account_ids: tuple[str, ...]
    status: Literal["INCOMPLETE", "COMPLETE"] = "INCOMPLETE"
    version: int = 1
    targets: dict[str, GlobalEmergencyTarget] = field(default_factory=dict)


@dataclass
class OperatorCommand:
    """An auditable, idempotent operator action tied to one account."""

    id: str
    account_id: str
    signal_id: str | None
    kind: OperatorCommandKind
    idempotency_key: str
    reason: str
    confirmed: bool
    status: Literal["ACCEPTED", "DISPATCHING", "REJECTED", "EXECUTED", "UNKNOWN"]
    rejection_code: str | None = None
    order_id: str | None = None


class ExecutionSubstrate:
    """In-memory reference implementation of the account-local transaction.

    PostgreSQL migration 006 persists these same records.  Keeping this model
    deterministic makes connector failure fixtures runnable without a broker.
    """

    def __init__(self) -> None:
        self._accounts: dict[str, AccountExecutionState] = {}
        self._locks: dict[str, threading.RLock] = {}
        self.reservations: dict[str, RiskReservation] = {}
        self.orders: dict[str, OrderIntent] = {}
        self.events: dict[str, OutboxEvent] = {}
        self.dispatch_records: dict[str, ConnectorDispatchRecord] = {}
        self.journal: dict[str, ConnectorJournalEntry] = {}
        self.fills: dict[str, Fill] = {}
        self.positions: dict[tuple[str, str], Position] = {}
        self.position_commands: list[PositionCommand] = []
        self._idempotency: dict[tuple[str, str], tuple[str, str]] = {}
        self._order_reservations: dict[str, str] = {}
        self.commands: dict[str, OperatorCommand] = {}
        self._command_keys: dict[tuple[str, str], str] = {}
        self._position_command_keys: dict[tuple[str, str], str] = {}
        self._approved_signals: dict[tuple[str, str], str] = {}
        self.global_emergencies: dict[str, GlobalEmergencyOperation] = {}
        self._critical_alerts: dict[str, CriticalAlert] = {}
        self.protection_repairs: dict[tuple[str, str], dict[str, Any]] = {}
        self.calendar_blocks: dict[str, list[dict[str, Any]]] = {}
        self.calendar_overrides: dict[str, dict[str, Any]] = {}

    def _lock_for(self, account_id: str) -> threading.RLock:
        return self._locks.setdefault(account_id, threading.RLock())

    @contextmanager
    def account_lock(self, account_id: str) -> Iterator[None]:
        """Serialize a handler's fresh risk snapshot with order acceptance."""
        with self._lock_for(account_id):
            yield

    def account(self, account_id: str) -> AccountExecutionState:
        return self._accounts.setdefault(account_id, AccountExecutionState(account_id))

    def runtime_interlock(self, account_id: str) -> RuntimeInterlockDecision:
        """Return the account-local exposure decision and its evidence."""
        account = self.account(account_id)
        return RuntimeInterlockDecision(
            account_id=account_id,
            status=account.runtime_interlock,
            reasons=account.interlock_reasons,
            evidence=dict(account.interlock_evidence),
            requires_custodian_command=account.quarantine_requires_command,
        )

    def _set_runtime_interlock(
        self,
        account_id: str,
        *,
        reasons: Iterable[str],
        evidence: dict[str, Any] | None = None,
        persistent: bool = False,
        now: datetime | None = None,
    ) -> RuntimeInterlockDecision:
        account = self.account(account_id)
        normalized = tuple(dict.fromkeys(reason for reason in reasons if reason))
        if not normalized:
            raise ValueError("runtime interlock requires a reason")
        if account.quarantine_requires_command and not persistent:
            return self.runtime_interlock(account_id)
        account.runtime_interlock = "QUARANTINED" if persistent else "BLOCKED"
        account.interlock_reasons = tuple(dict.fromkeys((*account.interlock_reasons, *normalized)))
        account.interlock_evidence = {
            **account.interlock_evidence,
            **dict(evidence or {}),
        }
        account.quarantine_requires_command = (
            account.quarantine_requires_command or persistent
        )
        account.interlock_updated_at = _as_utc(now or _now())
        account.exposure_gate = "QUARANTINED"
        return self.runtime_interlock(account_id)

    def _remove_runtime_interlock_reasons(
        self, account_id: str, reason_codes: Iterable[str]
    ) -> None:
        account = self.account(account_id)
        removed = set(reason_codes)
        account.interlock_reasons = tuple(
            reason for reason in account.interlock_reasons if reason not in removed
        )
        if not account.interlock_reasons:
            self._restore_runtime_interlock_if_safe(account_id)

    def block_new_exposure(
        self,
        account_id: str,
        reason_code: str,
        *,
        evidence: dict[str, Any] | None = None,
        persistent: bool = False,
        now: datetime | None = None,
    ) -> RuntimeInterlockDecision:
        """Close only one account's entry gate for a runtime safety reason."""
        return self._set_runtime_interlock(
            account_id,
            reasons=(reason_code,),
            evidence=evidence,
            persistent=persistent,
            now=now,
        )

    set_runtime_interlock = block_new_exposure

    def critical_alerts(self, account_id: str) -> list[dict[str, Any]]:
        """Return open account-local critical alerts for dashboard projection."""
        return [
            {
                **alert.__dict__,
                "created_at": alert.created_at.isoformat(),
                "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
            }
            for alert in self._critical_alerts.values()
            if alert.account_id == account_id and alert.status == "OPEN"
        ]

    def _raise_critical_alert(
        self,
        account_id: str,
        reason_code: str,
        *,
        detail: str,
        evidence: dict[str, Any] | None = None,
    ) -> CriticalAlert:
        existing = next(
            (
                alert for alert in self._critical_alerts.values()
                if alert.account_id == account_id
                and alert.reason_code == reason_code
                and alert.status == "OPEN"
            ),
            None,
        )
        if existing is not None:
            return existing
        alert = CriticalAlert(
            id=str(uuid4()),
            account_id=account_id,
            reason_code=reason_code,
            detail=detail,
            evidence=dict(evidence or {}),
        )
        self._critical_alerts[alert.id] = alert
        audit = getattr(self, "_audit", None)
        if callable(audit):
            audit(
                account_id,
                "critical.runtime_interlock",
                reason_code=reason_code,
                alert_id=alert.id,
                evidence=alert.evidence,
            )
        return alert

    def observe_runtime_health(
        self,
        account_id: str,
        *,
        connector_healthy: bool | None = None,
        connector_gap: bool = False,
        broker_facts_fresh: bool = True,
        reconciliation_healthy: bool = True,
        risk_state_known: bool = True,
        reservation_consistent: bool = True,
        ordering_safe: bool = True,
        backup_observed_at: datetime | None = None,
        alert_delivery_observed_at: datetime | None = None,
        now: datetime | None = None,
        freshness_window: timedelta = timedelta(minutes=5),
        escalation_deadline: timedelta = timedelta(minutes=15),
    ) -> RuntimeInterlockDecision:
        """Project account-local runtime facts into the exposure interlock."""
        at = _as_utc(now or _now())
        reasons: list[str] = []
        evidence: dict[str, Any] = {"observed_at": at.isoformat()}
        if connector_healthy is False:
            reasons.append("CONNECTOR_UNAVAILABLE")
        if connector_gap:
            reasons.append("CONNECTOR_GAP")
        if not broker_facts_fresh:
            reasons.append("BROKER_FACTS_STALE")
        if not reconciliation_healthy:
            reasons.append("RECONCILIATION_FAILED")
        if not risk_state_known:
            reasons.append("RISK_STATE_UNCERTAIN")
        if not reservation_consistent:
            reasons.append("RESERVATION_INCONSISTENT")
        if not ordering_safe:
            reasons.append("ORDERING_OVERLOAD")

        persistent = False
        for name, observed_at, reason in (
            ("backup", backup_observed_at, "BACKUP_RECOVERY_POINT_STALE"),
            ("critical_alert_delivery", alert_delivery_observed_at, "CRITICAL_ALERT_DELIVERY_STALE"),
        ):
            if observed_at is None:
                continue
            observed_at = _as_utc(observed_at)
            age = at - observed_at
            evidence[f"{name}_observed_at"] = observed_at.isoformat()
            evidence[f"{name}_age_seconds"] = age.total_seconds()
            if age > freshness_window:
                reasons.append(reason)
            if age > escalation_deadline:
                persistent = True
                self._raise_critical_alert(
                    account_id,
                    reason,
                    detail=f"{name} freshness deadline exceeded",
                    evidence=evidence,
                )

        if reasons:
            return self._set_runtime_interlock(
                account_id,
                reasons=reasons,
                evidence=evidence,
                persistent=persistent,
                now=at,
            )
        account = self.account(account_id)
        if account.runtime_interlock == "QUARANTINED":
            return self.runtime_interlock(account_id)
        self._remove_runtime_interlock_reasons(account_id, RUNTIME_HEALTH_REASONS)
        decision = self.runtime_interlock(account_id)
        if decision.status == "ELIGIBLE":
            account.interlock_evidence = dict(evidence)
        return decision

    def observe_connector_health(
        self,
        account_id: str,
        *,
        healthy: bool,
        gap: bool = False,
        now: datetime | None = None,
    ) -> RuntimeInterlockDecision:
        """Update connector facts without clearing unrelated account interlocks."""
        if not healthy or gap:
            reasons = []
            if not healthy:
                reasons.append("CONNECTOR_UNAVAILABLE")
            if gap:
                reasons.append("CONNECTOR_GAP")
            return self._set_runtime_interlock(
                account_id,
                reasons=reasons,
                evidence={"healthy": healthy, "gap": gap},
                now=now,
            )
        self._remove_runtime_interlock_reasons(account_id, CONNECTOR_HEALTH_REASONS)
        return self.runtime_interlock(account_id)

    update_runtime_health = observe_runtime_health

    def recover_runtime_interlock(
        self,
        account_id: str,
        *,
        evidence: dict[str, Any],
        custodian_command: bool = False,
        now: datetime | None = None,
    ) -> RuntimeInterlockDecision:
        """Clear a persistent quarantine only after evidence and command."""
        account = self.account(account_id)
        if account.runtime_interlock != "QUARANTINED":
            self._restore_runtime_interlock_if_safe(account_id, evidence)
            return self.runtime_interlock(account_id)
        if not custodian_command:
            raise ExecutionError("QUARANTINE_RECOVERY_COMMAND_REQUIRED")
        if not evidence.get("broker_reconciled") and not evidence.get("verified"):
            raise ExecutionError("QUARANTINE_RECOVERY_EVIDENCE_REQUIRED")
        if self._has_unconfirmed_open_protection(account_id):
            raise ExecutionError("NATIVE_PROTECTION_UNVERIFIED")
        account.quarantine_requires_command = False
        account.runtime_interlock = "ELIGIBLE"
        account.interlock_reasons = ()
        account.interlock_evidence = dict(evidence)
        observed_at = _as_utc(now or _now())
        account.interlock_updated_at = observed_at
        if account.exposure_gate == "QUARANTINED":
            account.exposure_gate = "OPEN"
        for alert in self._critical_alerts.values():
            if alert.account_id == account_id and alert.status == "OPEN":
                alert.status = "RESOLVED"
                alert.resolved_at = observed_at
        audit = getattr(self, "_audit", None)
        if callable(audit):
            audit(account_id, "execution.interlock.recovered", evidence=evidence)
        return self.runtime_interlock(account_id)

    clear_runtime_interlock = recover_runtime_interlock

    @staticmethod
    def _payload_currencies(order_payload: dict[str, Any]) -> set[str]:
        supplied = order_payload.get("currencies") or order_payload.get("currency")
        if isinstance(supplied, str):
            currencies = {supplied.upper()}
        else:
            currencies = {str(item).upper() for item in (supplied or ())}
        pair = str(order_payload.get("symbol", order_payload.get("pair", ""))).upper()
        if len(pair) == 6 and pair.isalpha():
            currencies.update((pair[:3], pair[3:]))
        return currencies

    def _calendar_blocks_entry(
        self,
        account_id: str,
        order_payload: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> bool:
        pair = str(order_payload.get("symbol", order_payload.get("pair", ""))).upper()
        currencies = self._payload_currencies(order_payload)
        at = _as_utc(now or _now())
        for block in self._active_calendar_blocks(account_id, at):
            if not block.get("scope_known", False):
                return True
            if block.get("pair") and str(block["pair"]).upper() == pair:
                return True
            if currencies.intersection({str(item).upper() for item in block.get("currencies", ())}):
                return True
        return False

    @staticmethod
    def _calendar_block_is_active(block: dict[str, Any], at: datetime) -> bool:
        if not block.get("active", True):
            return False
        expires_at = block.get("expires_at")
        if expires_at is not None and at >= _state_datetime(expires_at):
            return False
        blackout_start = block.get("blackout_start")
        if blackout_start is not None and at < _state_datetime(blackout_start):
            return False
        blackout_end = block.get("blackout_end")
        if blackout_end is not None and at >= _state_datetime(blackout_end):
            return False
        return True

    def _active_calendar_blocks(
        self, account_id: str, at: datetime
    ) -> list[dict[str, Any]]:
        return [
            block
            for block in self.calendar_blocks.get(account_id, [])
            if self._calendar_block_is_active(block, at)
        ]

    def set_calendar_interlock(
        self,
        account_id: str,
        *,
        blackout: bool = True,
        pair: str | None = None,
        currencies: Iterable[str] = (),
        scope_known: bool = True,
        reason_code: str = "CALENDAR_BLACKOUT_ACTIVE",
        expires_at: datetime | None = None,
        blackout_start: datetime | None = None,
        blackout_end: datetime | None = None,
        now: datetime | None = None,
    ) -> RuntimeInterlockDecision:
        """Apply calendar impact to one account, preserving known scope."""
        normalized_pair = pair.upper() if pair else None
        normalized_currencies = tuple(sorted({str(item).upper() for item in currencies}))
        normalized_expires_at = _as_utc(expires_at) if expires_at else None
        normalized_blackout_start = _as_utc(blackout_start) if blackout_start else None
        normalized_blackout_end = _as_utc(blackout_end) if blackout_end else None
        blocks = self.calendar_blocks.setdefault(account_id, [])
        matching = [
            block for block in blocks
            if block.get("pair") == normalized_pair
            and tuple(block.get("currencies", ())) == normalized_currencies
            and block.get("scope_known", False) == scope_known
        ]
        if blackout:
            block = matching[0] if matching else {
                "pair": normalized_pair,
                "currencies": normalized_currencies,
                "scope_known": scope_known,
                "reason_code": reason_code,
                "active": True,
            }
            block.update({
                "active": True,
                "reason_code": reason_code,
                "expires_at": normalized_expires_at,
                "blackout_start": normalized_blackout_start,
                "blackout_end": normalized_blackout_end,
                "updated_at": _as_utc(now or _now()).isoformat(),
            })
            if not matching:
                blocks.append(block)
            if not scope_known or (normalized_pair is None and not normalized_currencies):
                return self._set_runtime_interlock(
                    account_id,
                    reasons=(reason_code,),
                    evidence={
                        "scope_known": scope_known,
                        "pair": normalized_pair,
                        "currencies": normalized_currencies,
                    },
                    now=now,
                )
            return self.runtime_interlock(account_id)

        for block in matching:
            block["active"] = False
        if self.account(account_id).runtime_interlock != "QUARANTINED":
            active_broad_blocks = [
                block
                for block in self._active_calendar_blocks(
                    account_id, _as_utc(now or _now())
                )
                if not block.get("scope_known", False)
            ]
            if not active_broad_blocks:
                self._remove_runtime_interlock_reasons(
                    account_id, {reason_code, "CALENDAR_BLACKOUT_ACTIVE"}
                )
            self._restore_runtime_interlock_if_safe(account_id)
        return self.runtime_interlock(account_id)

    def add_manual_economic_event_override(
        self,
        account_id: str,
        *,
        blackout_start: datetime,
        blackout_end: datetime,
        reason: str,
        pair: str | None = None,
        currencies: Iterable[str] = (),
        expires_at: datetime | None = None,
        override_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or extend a blackout; this method never shortens one."""
        blackout_start = _as_utc(blackout_start)
        blackout_end = _as_utc(blackout_end)
        expires_at = _as_utc(expires_at) if expires_at else None
        if blackout_end <= blackout_start:
            raise ValueError("blackout_end must be after blackout_start")
        key = override_id or str(uuid4())
        prior = self.calendar_overrides.get(key)
        if prior is not None:
            if prior.get("account_id") != account_id:
                raise ExecutionError("WRONG_ACCOUNT")
            blackout_start = min(blackout_start, _state_datetime(prior["blackout_start"]))
            blackout_end = max(blackout_end, _state_datetime(prior["blackout_end"]))
        record = {
            "id": key,
            "account_id": account_id,
            "pair": pair.upper() if pair else None,
            "currencies": tuple(sorted({str(item).upper() for item in currencies})),
            "blackout_start": blackout_start.isoformat(),
            "blackout_end": blackout_end.isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "reason": reason,
        }
        self.calendar_overrides[key] = record
        self.set_calendar_interlock(
            account_id,
            pair=record["pair"],
            currencies=record["currencies"],
            scope_known=bool(record["pair"] or record["currencies"]),
            expires_at=expires_at,
            blackout_start=blackout_start,
            blackout_end=blackout_end,
        )
        audit = getattr(self, "_audit", None)
        if callable(audit):
            audit(
                account_id,
                "calendar.manual_override.created",
                override_id=record["id"],
                pair=record["pair"],
                currencies=record["currencies"],
                blackout_start=record["blackout_start"],
                blackout_end=record["blackout_end"],
            )
        return record

    add_calendar_override = add_manual_economic_event_override

    def _assessment_is_fresh(
        self,
        account_id: str,
        signal_id: str,
        assessment: RiskAssessment,
        now: datetime,
        signal_revision: int | None = None,
    ) -> None:
        if assessment.broker_account_id != account_id:
            raise ExecutionError("RISK_ASSESSMENT_ACCOUNT_MISMATCH")
        if assessment.purpose != "PRE_ORDER":
            raise ExecutionError("PRE_ORDER_ASSESSMENT_REQUIRED")
        if not assessment.approved:
            raise ExecutionError("PRE_ORDER_RISK_REJECTED")
        if signal_revision is not None and assessment.signal_revision != signal_revision:
            raise ExecutionError("SIGNAL_REVISION_CHANGED")
        if assessment.signal_id != signal_id:
            raise ExecutionError("RISK_ASSESSMENT_SIGNAL_MISMATCH")
        if assessment.assessed_at is None or assessment.valid_until is None:
            raise ExecutionError("RISK_ASSESSMENT_NOT_FRESH")
        if now < assessment.assessed_at or now >= assessment.valid_until:
            raise ExecutionError("RISK_ASSESSMENT_EXPIRED")

    def _reservation_state_is_consistent(self, account_id: str) -> bool:
        for reservation in self.reservations.values():
            if reservation.account_id != account_id:
                continue
            if reservation.status not in {"ACTIVE", "CONSUMED", "RELEASED"}:
                return False
            if self._decimal(reservation.amount) < 0:
                return False
        for order_id, reservation_id in self._order_reservations.items():
            order = self.orders.get(order_id)
            if order is None or order.account_id != account_id:
                continue
            reservation = self.reservations.get(reservation_id)
            if reservation is None or reservation.account_id != account_id:
                return False
        return True

    def _entry_interlock_reasons(
        self,
        account_id: str,
        order_payload: dict[str, Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        account = self.account(account_id)
        at = _as_utc(now or _now())
        calendar_blocks = self.calendar_blocks.get(account_id, [])
        broad_calendar_reasons = {
            str(block.get("reason_code", "CALENDAR_BLACKOUT_ACTIVE"))
            for block in calendar_blocks
            if not block.get("scope_known", False)
        }
        active_calendar_blocks = self._active_calendar_blocks(account_id, at)
        active_broad_calendar_reasons = {
            str(block.get("reason_code", "CALENDAR_BLACKOUT_ACTIVE"))
            for block in active_calendar_blocks
            if not block.get("scope_known", False)
        }
        expired_calendar_reasons = broad_calendar_reasons - active_broad_calendar_reasons
        if expired_calendar_reasons:
            self._remove_runtime_interlock_reasons(
                account_id, expired_calendar_reasons
            )
            account = self.account(account_id)
        reasons = list(account.interlock_reasons)
        if account.runtime_interlock != "ELIGIBLE" and not reasons:
            reasons.append("RUNTIME_INTERLOCK_ACTIVE")
        if not self._reservation_state_is_consistent(account_id):
            self._set_runtime_interlock(
                account_id,
                reasons=("RESERVATION_INCONSISTENT",),
                evidence={"account_id": account_id},
            )
            reasons.append("RESERVATION_INCONSISTENT")
        if order_payload is not None and self._calendar_blocks_entry(account_id, order_payload, now=at):
            reasons.append("CALENDAR_BLACKOUT_ACTIVE")
        return tuple(dict.fromkeys(reasons))

    def _idempotent_result(
        self, account_id: str, idempotency_key: str, canonical_hash: str
    ) -> PreOrderResult | None:
        prior = self._idempotency.get((account_id, idempotency_key))
        if prior is None:
            return None
        prior_order_id, prior_hash = prior
        if prior_hash != canonical_hash:
            raise ExecutionError("IDEMPOTENCY_KEY_REUSED")
        order = self.orders[prior_order_id]
        return PreOrderResult(
            self._reservation_for(order.id), order, self._event_for(order.id)
        )

    def existing_pre_order(
        self, *, account_id: str, idempotency_key: str, canonical_hash: str
    ) -> PreOrderResult | None:
        """Return a prior accepted order before retry-time gates can reject it."""
        with self._lock_for(account_id):
            return self._idempotent_result(
                account_id, idempotency_key, canonical_hash
            )

    def _accept_execution(
        self,
        *,
        account_id: str,
        signal_id: str,
        idempotency_key: str,
        canonical_hash: str,
        execution_epoch: int,
        risk_assessment: RiskAssessment | None,
        signal_revision: int,
        order_payload: dict[str, Any],
        risk_amount: str,
    ) -> PreOrderResult:
        """Acceptance seam; the durable coordinator persists the assessment here."""
        return self.pre_order(
            account_id=account_id,
            signal_id=signal_id,
            idempotency_key=idempotency_key,
            canonical_hash=canonical_hash,
            execution_epoch=execution_epoch,
            risk_assessment=risk_assessment,
            signal_revision=signal_revision,
            order_payload=order_payload,
            risk_amount=risk_amount,
        )

    def pre_order(
        self, *, account_id: str, signal_id: str, idempotency_key: str,
        canonical_hash: str, execution_epoch: int,
        risk_assessment: RiskAssessment | None, signal_revision: int,
        order_payload: dict[str, Any], risk_amount: str = "0",
        now: datetime | None = None,
    ) -> PreOrderResult:
        with self._lock_for(account_id):
            account = self.account(account_id)
            prior = self._idempotent_result(
                account_id, idempotency_key, canonical_hash
            )
            if prior is not None:
                return prior
            if risk_assessment is None:
                raise ExecutionError("PRE_ORDER_ASSESSMENT_REQUIRED")
            interlock_reasons = self._entry_interlock_reasons(
                account_id, order_payload, now=now
            )
            if interlock_reasons:
                if "CALENDAR_BLACKOUT_ACTIVE" in interlock_reasons:
                    raise ExecutionError("CALENDAR_BLACKOUT_ACTIVE")
                raise ExecutionError("EXPOSURE_GATE_CLOSED")
            self._assessment_is_fresh(
                account_id, signal_id, risk_assessment, now or _now(), signal_revision
            )
            if account.exposure_gate != "OPEN":
                raise ExecutionError("EXPOSURE_GATE_CLOSED")
            if execution_epoch != account.execution_epoch:
                raise ExecutionError("STALE_EXECUTION_EPOCH")
            reservation = RiskReservation(str(uuid4()), account_id, signal_id, risk_amount)
            order = OrderIntent(
                id=str(uuid4()),
                account_id=account_id,
                signal_id=signal_id,
                idempotency_key=idempotency_key,
                canonical_hash=canonical_hash,
                execution_epoch=execution_epoch,
                dispatch_sequence=account.next_dispatch_sequence,
                payload=json.loads(json.dumps(order_payload)),
            )
            account.next_dispatch_sequence += 1
            event = OutboxEvent(str(uuid4()), account_id, order.id, order.dispatch_sequence)
            self.reservations[reservation.id] = reservation
            self.orders[order.id] = order
            self._order_reservations[order.id] = reservation.id
            self.events[event.id] = event
            self._idempotency[(account_id, idempotency_key)] = (order.id, canonical_hash)
            return PreOrderResult(reservation, order, event)

    def _command(
        self,
        *,
        account_id: str,
        signal_id: str | None,
        kind: OperatorCommandKind,
        idempotency_key: str,
        reason: str,
        confirmed: bool,
    ) -> OperatorCommand:
        if not reason.strip():
            raise ExecutionError("OPERATOR_REASON_REQUIRED")
        if not confirmed:
            raise ExecutionError("OPERATOR_CONFIRMATION_REQUIRED")
        prior_id = self._command_keys.get((account_id, idempotency_key))
        if prior_id:
            return self.commands[prior_id]
        command = OperatorCommand(
            str(uuid4()),
            account_id,
            signal_id,
            kind,
            idempotency_key,
            reason,
            confirmed,
            "ACCEPTED",
        )
        self.commands[command.id] = command
        self._command_keys[(account_id, idempotency_key)] = command.id
        return command

    def approve_signal(
        self, *, account_id: str, signal_id: str, idempotency_key: str,
        reason: str, confirmed: bool, signal_revision: int,
        signal_eligible: bool = True,
    ) -> OperatorCommand:
        """Approve only; approval never dispatches a broker side effect."""
        with self._lock_for(account_id):
            command = self._command(
                account_id=account_id,
                signal_id=signal_id,
                kind="APPROVE_SIGNAL",
                idempotency_key=idempotency_key,
                reason=reason,
                confirmed=confirmed,
            )
            if command.status != "ACCEPTED":
                return command
            if not signal_eligible:
                command.status = "REJECTED"
                command.rejection_code = "SIGNAL_NOT_ELIGIBLE"
                return command
            self._approved_signals[(account_id, signal_id)] = command.id
            command.reason = f"{reason} [revision:{signal_revision}]"
            return command

    def execute_signal(
        self, *, account_id: str, signal_id: str, idempotency_key: str,
        reason: str, confirmed: bool, signal_revision: int,
        signal_fresh: bool, fence_safe: bool,
        account_state: str, live_lock: bool, execution_epoch: int,
        order_payload: dict[str, Any], risk_amount: str = "0",
        risk_assessment: RiskAssessment | None = None,
    ) -> PreOrderResult:
        """Execute an already approved Signal after every last-mile gate."""
        with self._lock_for(account_id):
            canonical_hash = canonical_order_hash(
                order_payload, signal_revision, risk_amount
            )
            prior = self._idempotent_result(
                account_id, idempotency_key, canonical_hash
            )
            if prior is not None:
                return prior
            approval_id = self._approved_signals.get((account_id, signal_id))
            if approval_id is None:
                raise ExecutionError("SIGNAL_APPROVAL_REQUIRED")
            if not all((signal_fresh, fence_safe, live_lock)):
                raise ExecutionError("EXECUTION_GATE_UNSAFE")
            if account_state != "RUNNING":
                raise ExecutionError("ACCOUNT_STATE_UNSAFE")
            if order_payload.get("signal_revision") not in (None, signal_revision):
                raise ExecutionError("SIGNAL_REVISION_CHANGED")
            if not order_payload.get("stop_loss") or not order_payload.get("take_profit"):
                raise ExecutionError("NATIVE_PROTECTION_REQUIRED")
            command = self._command(
                account_id=account_id,
                signal_id=signal_id,
                kind="EXECUTE_SIGNAL",
                idempotency_key=idempotency_key,
                reason=reason,
                confirmed=confirmed,
            )
            result = self._accept_execution(
                account_id=account_id,
                signal_id=signal_id,
                idempotency_key=idempotency_key,
                canonical_hash=canonical_hash,
                execution_epoch=execution_epoch,
                risk_assessment=risk_assessment,
                signal_revision=signal_revision,
                order_payload=order_payload,
                risk_amount=risk_amount,
            )
            command.status, command.order_id = "EXECUTED", result.order.id
            return result

    def schedule_automated_signal(
        self, *, account_id: str, signal_id: str, idempotency_key: str,
        mode: Literal["MANUAL", "SEMI_AUTO", "FULL_AUTO"],
        signal_created_at: datetime, signal_revision: int, signal_eligible: bool,
        signal_approved: bool, mode_changed_at: datetime,
        signal_fresh: bool, fence_safe: bool,
        account_state: str, live_lock: bool, execution_epoch: int,
        order_payload: dict[str, Any], risk_amount: str = "0",
        risk_assessment: RiskAssessment | None = None,
        account_ready: bool = True,
    ) -> PreOrderResult:
        """Schedule exactly one account-local order for an eligible Signal."""
        with self._lock_for(account_id):
            canonical_hash = canonical_order_hash(
                order_payload, signal_revision, risk_amount
            )
            prior = self._idempotent_result(
                account_id, idempotency_key, canonical_hash
            )
            if prior is not None:
                return prior
            if mode == "MANUAL":
                raise ExecutionError("AUTOMATION_DISABLED")
            if signal_created_at < mode_changed_at:
                raise ExecutionError("SIGNAL_PRECEDES_MODE_CHANGE")
            if not signal_eligible or signal_revision < 1:
                raise ExecutionError("SIGNAL_NOT_ELIGIBLE")
            if mode == "SEMI_AUTO" and not signal_approved:
                raise ExecutionError("SIGNAL_APPROVAL_REQUIRED")
            if not all((signal_fresh, fence_safe, live_lock, account_ready)):
                raise ExecutionError("EXECUTION_GATE_UNSAFE")
            if account_state != "RUNNING":
                raise ExecutionError("ACCOUNT_STATE_UNSAFE")
            if order_payload.get("signal_revision") not in (None, signal_revision):
                raise ExecutionError("SIGNAL_REVISION_CHANGED")
            if not order_payload.get("stop_loss") or not order_payload.get("take_profit"):
                raise ExecutionError("NATIVE_PROTECTION_REQUIRED")
            return self._accept_execution(
                account_id=account_id, signal_id=signal_id,
                idempotency_key=idempotency_key,
                canonical_hash=canonical_hash,
                execution_epoch=execution_epoch, risk_assessment=risk_assessment,
                signal_revision=signal_revision,
                order_payload=order_payload, risk_amount=risk_amount,
            )

    def begin_global_emergency(
        self, account_ids: list[str] | tuple[str, ...], *,
        kind: Literal["STOP_ONLY", "CLOSE_ALL"] = "STOP_ONLY",
    ) -> GlobalEmergencyOperation:
        """Freeze target membership at acceptance; convergence is explicit per account."""
        targets = tuple(dict.fromkeys(account_ids))
        if not targets:
            raise ExecutionError("GLOBAL_TARGETS_REQUIRED")
        operation_id = str(uuid4())
        target_records = {
            account_id: GlobalEmergencyTarget(operation_id, account_id, kind)
            for account_id in targets
        }
        operation = GlobalEmergencyOperation(
            id=operation_id,
            requested_kind=kind,
            target_account_ids=targets,
            targets=target_records,
        )
        self.global_emergencies[operation.id] = operation
        for account_id in targets:
            self.emergency_stop(account_id)
        return operation

    def converge_global_target(
        self, operation_id: str, account_id: str, *,
        resolved: bool, detail: str | None = None,
    ) -> GlobalEmergencyOperation:
        operation = self.global_emergencies.get(operation_id)
        if operation is None or account_id not in operation.targets:
            raise ExecutionError("GLOBAL_TARGET_NOT_FOUND")
        target = operation.targets[account_id]
        target.status = "CONVERGED" if resolved else "UNRESOLVED"
        target.detail = detail
        operation.status = "COMPLETE" if all(
            item.status == "CONVERGED" for item in operation.targets.values()
        ) else "INCOMPLETE"
        operation.version += 1
        return operation

    def close_all(
        self, *, account_id: str, idempotency_key: str, reason: str,
        confirmed: bool, connector: Any | None = None,
    ) -> OperatorCommand:
        """Explicitly close positions; emergency stop itself never closes them."""
        with self._lock_for(account_id):
            command = self._command(
                account_id=account_id,
                signal_id=None,
                kind="CLOSE_ALL",
                idempotency_key=idempotency_key,
                reason=reason,
                confirmed=confirmed,
            )
            if command.status == "ACCEPTED" and connector is not None:
                command.status = "DISPATCHING"
                self._before_connector_call(command)
                close_all = getattr(connector, "close_all", None)
                if not callable(close_all):
                    command.status = "REJECTED"
                    command.rejection_code = "CONNECTOR_CAPABILITY_UNAVAILABLE"
                    return command
                try:
                    response = close_all(account_id)
                except Exception:
                    response = None
                if self._response_is_ambiguous(response):
                    command.status = "UNKNOWN"
                    command.rejection_code = "RECONCILIATION_PENDING"
                elif self._response_status(response) in {
                    "ACCEPTED", "CONFIRMED", "EXECUTED", "FILLED", "CLOSED"
                }:
                    command.status = "EXECUTED"
                else:
                    command.status = "REJECTED"
                    command.rejection_code = "CONNECTOR_REJECTED"
            return command

    def _apply_cancel_order(self, order_id: str) -> None:
        order = self.orders.get(order_id)
        if order is None:
            return
        order.status = "CANCELLED"
        self._reservation_for(order_id).status = "RELEASED"
        event = self._event_for(order_id)
        if event.status in {"PENDING", "DISPATCHING"}:
            event.status = "ABORTED"

    def cancel_order(
        self, *, account_id: str, order_id: str, idempotency_key: str,
        reason: str, confirmed: bool, connector: Any | None = None,
    ) -> OperatorCommand:
        """Cancel one pending entry with durable ambiguity handling."""
        with self._lock_for(account_id):
            order = self.orders.get(order_id)
            if order is None or order.account_id != account_id:
                raise ExecutionError("WRONG_ACCOUNT")
            if order.status == "UNKNOWN":
                raise ExecutionError("RECONCILIATION_PENDING")
            if order.status not in {"INTENT", "SUBMITTED", "PARTIALLY_FILLED"}:
                raise ExecutionError("ORDER_NOT_CANCELLABLE")
            command = self._command(
                account_id=account_id,
                signal_id=order.signal_id,
                kind="CANCEL_ORDER",
                idempotency_key=idempotency_key,
                reason=reason,
                confirmed=confirmed,
            )
            command.order_id = order_id
            if command.status != "ACCEPTED" or connector is None:
                return command
            command.status = "DISPATCHING"
            self._before_connector_call(command)
            cancel_order = getattr(connector, "cancel_order", None)
            if not callable(cancel_order):
                command.status = "REJECTED"
                command.rejection_code = "CONNECTOR_CAPABILITY_UNAVAILABLE"
                return command
            try:
                response = cancel_order(order)
            except Exception:
                response = None
            if self._response_is_ambiguous(response):
                command.status = "UNKNOWN"
                command.rejection_code = "RECONCILIATION_PENDING"
            elif self._response_status(response) in {
                "ACCEPTED", "CONFIRMED", "EXECUTED", "CANCELLED", "CLOSED"
            }:
                command.status = "EXECUTED"
                self._apply_cancel_order(order_id)
            else:
                command.status = "REJECTED"
                command.rejection_code = "CONNECTOR_REJECTED"
            return command

    def outbox(self, account_id: str) -> list[OutboxEvent]:
        pending = (
            event
            for event in self.events.values()
            if event.account_id == account_id and event.status == "PENDING"
        )
        return sorted(pending, key=lambda event: event.dispatch_sequence)

    def _reservation_for(self, order_id: str) -> RiskReservation:
        return self.reservations[self._order_reservations[order_id]]

    def _event_for(self, order_id: str) -> OutboxEvent:
        return next(event for event in self.events.values() if event.order_id == order_id)

    def _fill_for_deal(
        self, account_id: str, external_deal_id: str
    ) -> Fill | None:
        return next(
            (
                fill
                for fill in self.fills.values()
                if fill.account_id == account_id
                and fill.external_deal_id == external_deal_id
            ),
            None,
        )

    def _reject_dispatch(
        self,
        event: OutboxEvent,
        order: OrderIntent,
        reservation: RiskReservation,
        journal: ConnectorJournalEntry,
        reason: str,
    ) -> DispatchResult:
        journal.state = "REJECTED"
        event.status = "ABORTED"
        order.status = "REJECTED"
        reservation.status = "RELEASED"
        return DispatchResult(order.id, "REJECTED", reason)

    def _before_connector_call(self, subject: Any) -> None:
        """Hook for durable coordinators to checkpoint before broker I/O."""

    @staticmethod
    def _connector_method(
        connector: Any, names: tuple[str, ...], subject: Any,
    ) -> Any:
        for name in names:
            method = getattr(connector, name, None)
            if callable(method):
                return method(subject)
        raise AttributeError("connector capability is unavailable")

    @staticmethod
    def _connector_query(
        connector: Any, names: tuple[str, ...], subject: Any, account_id: str,
    ) -> Any:
        for name in names:
            method = getattr(connector, name, None)
            if not callable(method):
                continue
            try:
                response = method(subject)
            except TypeError:
                try:
                    response = method(account_id)
                except Exception:
                    continue
            except Exception:
                continue
            if response is not None:
                return response
        return None

    @staticmethod
    def _response_status(response: Any) -> str | None:
        if response is None:
            return None
        if isinstance(response, dict):
            value = response.get("status")
            return str(value).upper() if value is not None else None
        return str(response).upper()

    @staticmethod
    def _response_is_ambiguous(response: Any) -> bool:
        return ExecutionSubstrate._response_status(response) in {
            None, "UNKNOWN", "TIMEOUT", "AMBIGUOUS", "UNAVAILABLE"
        }

    @staticmethod
    def _response_value(response: Any, *names: str) -> Any:
        if not isinstance(response, dict):
            return None
        for name in names:
            if response.get(name) is not None:
                return response[name]
        return None

    def _apply_confirmed_trailing_stop(
        self,
        account_id: str,
        command: PositionCommand,
        response: Any,
        *response_fields: str,
    ) -> None:
        if command.command_type != "TRAIL":
            return
        confirmed_stop = self._response_value(response, *response_fields)
        if confirmed_stop is None:
            return
        confirmed_stop = str(confirmed_stop)
        command.confirmed_stop = confirmed_stop
        position = self.position(account_id, command.order_id)
        position.last_confirmed_stop = confirmed_stop

    def dispatch_next(self, account_id: str, connector: Any) -> DispatchResult:
        with self._lock_for(account_id):
            if any(
                order.account_id == account_id and order.status == "UNKNOWN"
                for order in self.orders.values()
            ):
                raise ExecutionError("RECONCILIATION_PENDING")
            pending = self.outbox(account_id)
            if not pending:
                raise ExecutionError("DISPATCH_EMPTY")
            event = pending[0]
            order = self.orders[event.order_id]
            reservation = self._reservation_for(order.id)
            event.status = "DISPATCHING"
            journal = ConnectorJournalEntry(
                str(uuid4()),
                account_id,
                order.id,
                order.dispatch_sequence,
                "DISPATCHING",
            )
            self.journal[order.id] = journal
            order.status = "DISPATCHING"
            self._before_connector_call(order)
            try:
                checked = connector.order_check(order)
            except Exception:
                checked = False
            if not checked:
                return self._reject_dispatch(
                    event, order, reservation, journal, "ORDER_CHECK_FAILED"
                )
            try:
                response = connector.order_send(order)
            except Exception:
                response = "TIMEOUT"
            if self._response_is_ambiguous(response):
                order.status = "UNKNOWN"
                return DispatchResult(order.id, "UNKNOWN", "CONNECTOR_RESULT_AMBIGUOUS")
            response_status = self._response_status(response)
            accepted = response if isinstance(response, dict) else {"status": response_status}
            if response_status not in {"ACCEPTED", "SUBMITTED", "FILLED"}:
                return self._reject_dispatch(
                    event, order, reservation, journal, "CONNECTOR_REJECTED"
                )
            if response_status == "FILLED":
                order.status = "FILLED"
            else:
                order.status = "SUBMITTED"
            order.external_id = accepted.get("external_id")
            journal.state = "ACCEPTED"
            journal.external_id = order.external_id
            event.status = "PUBLISHED"
            return DispatchResult(order.id, order.status)

    def dispatch_position_command(
        self, account_id: str, command_id: str, connector: Any,
    ) -> DispatchResult:
        """Send one reduce-only position command after durable preparation."""
        with self._lock_for(account_id):
            command = next(
                (item for item in self.position_commands if item.id == command_id),
                None,
            )
            if command is None or command.account_id != account_id:
                raise ExecutionError("POSITION_COMMAND_NOT_FOUND")
            if command.status == "UNKNOWN":
                raise ExecutionError("RECONCILIATION_PENDING")
            if command.status != "RECEIVED":
                return DispatchResult(command.id, command.status)
            capability_names = (
                ("modify_position", "modify")
                if command.command_type == "TRAIL"
                else ("close_position", "close")
            )
            if not any(callable(getattr(connector, name, None)) for name in capability_names):
                command.status = "REJECTED"
                command.reason = "CONNECTOR_CAPABILITY_UNAVAILABLE"
                return DispatchResult(command.id, "REJECTED", "CONNECTOR_CAPABILITY_UNAVAILABLE")
            command.status = "DISPATCHING"
            self._before_connector_call(command)
            try:
                response = self._connector_method(connector, capability_names, command)
            except Exception:
                response = None
            status = self._response_status(response)
            if status is None or status == "UNKNOWN" or status == "TIMEOUT":
                command.status = "UNKNOWN"
                return DispatchResult(command.id, "UNKNOWN", "CONNECTOR_RESULT_AMBIGUOUS")
            if status in {"ACCEPTED", "CONFIRMED", "EXECUTED", "FILLED", "CLOSED"}:
                command.status = "CONFIRMED"
                self._apply_confirmed_trailing_stop(
                    account_id, command, response,
                    "confirmed_stop", "stop", "requested_stop",
                )
                return DispatchResult(command.id, "CONFIRMED")
            command.status = "REJECTED"
            command.reason = f"CONNECTOR_{status}"
            return DispatchResult(command.id, "REJECTED", "CONNECTOR_REJECTED")

    def recover_position_command(
        self, account_id: str, command_id: str, connector: Any,
    ) -> DispatchResult:
        """Read broker truth for an UNKNOWN position command without resending."""
        with self._lock_for(account_id):
            command = next(
                (item for item in self.position_commands if item.id == command_id),
                None,
            )
            if command is None or command.account_id != account_id:
                raise ExecutionError("POSITION_COMMAND_NOT_FOUND")
            if command.status != "UNKNOWN":
                return DispatchResult(command.id, command.status)
            response = self._connector_query(
                connector,
                ("position_command_state", "command_state", "broker_command_state"),
                command,
                account_id,
            )
            status = self._response_status(response)
            if status in {"CONFIRMED", "EXECUTED", "FILLED", "CLOSED", "ACCEPTED"}:
                command.status = "CONFIRMED"
                self._apply_confirmed_trailing_stop(
                    account_id, command, response,
                    "confirmed_stop", "stop", "requested_stop",
                )
                return DispatchResult(command.id, "CONFIRMED")
            if status in {"REJECTED", "NOT_FOUND", "CANCELLED"}:
                command.status = "REJECTED"
                return DispatchResult(command.id, "REJECTED", "CONNECTOR_REJECTED")
            return DispatchResult(command.id, "UNKNOWN", "RECONCILIATION_PENDING")

    def recover_operator_command(
        self, account_id: str, command_id: str, connector: Any,
    ) -> DispatchResult:
        """Read broker truth for an UNKNOWN operator command without resending."""
        with self._lock_for(account_id):
            command = self.commands.get(command_id)
            if command is None or command.account_id != account_id:
                raise ExecutionError("WRONG_ACCOUNT")
            if command.status != "UNKNOWN":
                return DispatchResult(command.id, command.status)
            response = self._connector_query(
                connector,
                ("command_state", "close_all_state", "broker_command_state"),
                command,
                account_id,
            )
            status = self._response_status(response)
            if status in {"CONFIRMED", "EXECUTED", "FILLED", "CLOSED", "ACCEPTED"}:
                command.status = "EXECUTED"
                command.rejection_code = None
                self._apply_operator_command(command)
                return DispatchResult(command.id, "EXECUTED")
            if status in {"REJECTED", "NOT_FOUND", "CANCELLED"}:
                command.status = "REJECTED"
                return DispatchResult(command.id, "REJECTED", "CONNECTOR_REJECTED")
            return DispatchResult(command.id, "UNKNOWN", "RECONCILIATION_PENDING")

    def _apply_operator_command(self, command: OperatorCommand) -> None:
        if command.kind == "CANCEL_ORDER" and command.order_id is not None:
            self._apply_cancel_order(command.order_id)

    def recover(self, account_id: str, order_id: str, connector: Any) -> DispatchResult:
        with self._lock_for(account_id):
            order = self.orders.get(order_id)
            if order is None or order.account_id != account_id:
                raise ExecutionError("WRONG_ACCOUNT")
            if order.status != "UNKNOWN":
                return DispatchResult(order.id, order.status)
            journal = self.journal.get(order.id)
            if journal is None:
                raise ExecutionError("JOURNAL_MISSING")
            observed = self._connector_query(
                connector, ("journal",), order, account_id
            )
            if not observed:
                observed = self._connector_query(
                    connector, ("broker_state",), order, account_id
                )
            if not observed:
                return DispatchResult(order.id, "UNKNOWN", "RECONCILIATION_PENDING")
            status = self._response_status(observed)
            external_id = self._response_value(observed, "external_id")
            if status == "FILLED":
                order.status = "FILLED"
                order.external_id = external_id
                journal.state = "ACCEPTED"
                self._event_for(order.id).status = "PUBLISHED"
                self._reservation_for(order.id).status = "CONSUMED"
            elif status in {"ACCEPTED", "SUBMITTED", "PARTIALLY_FILLED"}:
                order.status = "PARTIALLY_FILLED" if status == "PARTIALLY_FILLED" else "SUBMITTED"
                order.external_id = external_id
                journal.state = "ACCEPTED"
                self._event_for(order.id).status = "PUBLISHED"
            elif status in {"REJECTED", "NOT_FOUND"}:
                order.status = "REJECTED"
                journal.state = "REJECTED"
                self._event_for(order.id).status = "ABORTED"
                self._reservation_for(order.id).status = "RELEASED"
            return DispatchResult(order.id, order.status)

    def record_fill(
        self,
        account_id: str,
        order_id: str,
        external_deal_id: str,
        volume: str,
        *,
        native_protection_confirmed: bool,
        external_position_id: str | None = None,
        accounting_mode: Literal["NETTING", "HEDGING"] = "NETTING",
    ) -> Fill:
        with self._lock_for(account_id):
            order = self.orders.get(order_id)
            if order is None or order.account_id != account_id:
                raise ExecutionError("WRONG_ACCOUNT")
            existing_fill = self._fill_for_deal(account_id, external_deal_id)
            if existing_fill is not None:
                return existing_fill
            fill = Fill(
                str(uuid4()),
                account_id,
                order_id,
                external_deal_id,
                str(volume),
                native_protection_confirmed,
            )
            self.fills[fill.id] = fill
            order.status = "FILLED"
            self._reservation_for(order_id).status = "CONSUMED"
            payload = order.payload
            position = self.positions.get((account_id, order_id))
            has_native_stop = payload.get("stop_loss") is not None
            if position is None:
                protection = (
                    "CONFIRMED"
                    if native_protection_confirmed and has_native_stop
                    else "UNCONFIRMED"
                )
                side = str(payload.get("side", "BUY")).upper()
                position = Position(
                    account_id=account_id,
                    order_id=order_id,
                    volume=str(volume),
                    protection_status=protection,
                    native_stop_loss=str(payload.get("stop_loss")) if has_native_stop else None,
                    native_take_profit=str(payload.get("take_profit")) if payload.get("take_profit") is not None else None,
                    last_confirmed_stop=str(payload.get("stop_loss")) if payload.get("stop_loss") is not None else None,
                    accounting_mode=accounting_mode,
                    external_position_id=external_position_id,
                    pair=str(payload.get("symbol", payload.get("pair", "UNKNOWN"))),
                    direction="LONG" if side in {"BUY", "LONG"} else "SHORT",
                    entry_price=(str(payload["entry_price"]) if payload.get("entry_price") is not None else "UNKNOWN"),
                )
                self.positions[(account_id, order_id)] = position
            else:
                total = self._decimal(position.volume) + self._decimal(volume)
                position.volume = self._decimal_string(total)
                position.remaining_volume = self._decimal_string(
                    self._decimal(position.remaining_volume or "0")
                    + self._decimal(volume)
                )
                if not native_protection_confirmed or not has_native_stop:
                    position.protection_status = "UNCONFIRMED"
            if position.protection_status != "CONFIRMED":
                self.account(account_id).exposure_gate = "QUARANTINED"
                self.install_fence(account_id, "SAFETY_FENCE")
                position.protection_status = "UNCONFIRMED"
                self._set_runtime_interlock(
                    account_id,
                    reasons=("NATIVE_PROTECTION_UNCONFIRMED",),
                    evidence={"position_id": order_id, "order_id": order_id},
                )
            return fill

    @staticmethod
    def _protection_stop(response: Any) -> str | None:
        value = ExecutionSubstrate._response_value(
            response,
            "stop_loss", "sl", "native_stop_loss", "confirmed_stop",
        )
        return str(value) if value is not None else None

    def _protection_response_verified(self, response: Any, expected_stop: str) -> bool:
        status = self._response_status(response)
        if status not in {"CONFIRMED", "EXECUTED", "FILLED", "ACCEPTED"}:
            return False
        confirmed = self._protection_stop(response)
        if confirmed is not None:
            return confirmed == expected_stop
        return bool(self._response_value(response, "protection_confirmed", "native_protection_confirmed"))

    def repair_native_protection(
        self,
        account_id: str,
        position_id: str,
        connector: Any,
        *,
        max_attempts: int | None = None,
        now: datetime | None = None,
    ) -> ProtectionRepairResult:
        """Reapply and verify a Position's native StopLoss within a fixed bound."""
        with self._lock_for(account_id):
            position = self.position(account_id, position_id)
            expected_stop = position.native_stop_loss or position.last_confirmed_stop
            if position.protection_status == "CONFIRMED" and expected_stop is not None:
                return ProtectionRepairResult(
                    account_id, position_id, "RECOVERED", 0,
                    evidence={"confirmed_stop_loss": expected_stop},
                )
            if expected_stop is None:
                position.protection_status = "QUARANTINED"
                self.install_fence(account_id, "SAFETY_FENCE")
                self._set_runtime_interlock(
                    account_id,
                    reasons=("NATIVE_PROTECTION_UNVERIFIED",),
                    evidence={"position_id": position_id, "reason": "STOP_LOSS_MISSING"},
                    persistent=True,
                    now=now,
                )
                self._raise_critical_alert(
                    account_id,
                    "NATIVE_PROTECTION_UNVERIFIED",
                    detail="native StopLoss cannot be reconstructed",
                    evidence={"position_id": position_id},
                )
                return ProtectionRepairResult(
                    account_id, position_id, "QUARANTINED", 0,
                    "NATIVE_PROTECTION_UNVERIFIED",
                )

            attempts_limit = max_attempts if max_attempts is not None else 3
            if attempts_limit < 1:
                raise ValueError("max_attempts must be positive")
            attempts = 0
            evidence: dict[str, Any] = {"requested_stop_loss": expected_stop}
            while attempts < attempts_limit:
                attempts += 1
                response: Any = None
                for name in (
                    "modify_position_protection",
                    "modify_protection",
                    "set_stop_loss",
                ):
                    method = getattr(connector, name, None)
                    if not callable(method):
                        continue
                    try:
                        response = method(position, expected_stop)
                    except TypeError:
                        response = method(account_id, position_id, expected_stop)
                    except Exception:
                        response = None
                    break
                if not self._protection_response_verified(response, expected_stop):
                    continue
                verified: Any = response
                for name in (
                    "position_protection",
                    "position_state",
                    "broker_position",
                ):
                    method = getattr(connector, name, None)
                    if not callable(method):
                        continue
                    try:
                        verified = method(position)
                    except TypeError:
                        verified = method(account_id, position_id)
                    except Exception:
                        verified = None
                    break
                if not self._protection_response_verified(verified, expected_stop):
                    continue
                confirmed_stop = self._protection_stop(verified) or expected_stop
                position.native_stop_loss = confirmed_stop
                position.last_confirmed_stop = confirmed_stop
                position.protection_status = "CONFIRMED"
                evidence.update({
                    "confirmed_stop_loss": confirmed_stop,
                    "attempts": attempts,
                    "verified": True,
                })
                self.protection_repairs[(account_id, position_id)] = {
                    "attempts": attempts,
                    "status": "RECOVERED",
                    "evidence": evidence,
                }
                self._remove_runtime_interlock_reasons(
                    account_id, PROTECTION_INTERLOCK_REASONS
                )
                if self.runtime_interlock(account_id).status == "ELIGIBLE":
                    self.account(account_id).interlock_evidence = dict(evidence)
                return ProtectionRepairResult(
                    account_id, position_id, "RECOVERED", attempts, evidence=evidence,
                )

            position.protection_status = "QUARANTINED"
            self.install_fence(account_id, "SAFETY_FENCE")
            evidence["attempts"] = attempts
            evidence["verified"] = False
            self.protection_repairs[(account_id, position_id)] = {
                "attempts": attempts,
                "status": "QUARANTINED",
                "evidence": evidence,
            }
            self._set_runtime_interlock(
                account_id,
                reasons=("NATIVE_PROTECTION_UNVERIFIED",),
                evidence={"position_id": position_id, **evidence},
                persistent=True,
                now=now,
            )
            self._raise_critical_alert(
                account_id,
                "NATIVE_PROTECTION_UNVERIFIED",
                detail="bounded native StopLoss repair could not be verified",
                evidence={"position_id": position_id, **evidence},
            )
            return ProtectionRepairResult(
                account_id, position_id, "QUARANTINED", attempts,
                "NATIVE_PROTECTION_UNVERIFIED", evidence,
            )

    repair_protection = repair_native_protection

    def _has_unconfirmed_open_protection(self, account_id: str) -> bool:
        return any(
            position.account_id == account_id
            and position.stage != "CLOSED"
            and position.protection_status != "CONFIRMED"
            for position in self.positions.values()
        )

    def _restore_runtime_interlock_if_safe(
        self, account_id: str, evidence: dict[str, Any] | None = None
    ) -> None:
        account = self.account(account_id)
        if account.quarantine_requires_command or account.interlock_reasons:
            return
        if self._has_unconfirmed_open_protection(account_id):
            return
        prior_gates = {
            work.entry_gate_before
            for work in getattr(self, "reconciliation_work", {}).values()
            if work.account_id == account_id
        }
        if prior_gates.intersection({"FENCE_PENDING", "STOPPED"}):
            return
        account.runtime_interlock = "ELIGIBLE"
        account.interlock_reasons = ()
        account.interlock_evidence = dict(evidence or {})
        account.interlock_updated_at = _now()
        if account.exposure_gate == "QUARANTINED":
            account.exposure_gate = "OPEN"

    def stage_exit(
        self,
        account_id: str,
        order_id: str,
        stage: Literal["TP1", "TP2", "RUNNER", "CLOSE"],
    ) -> None:
        """Reject price-crossing or unconfirmed exit stages; fills own progression."""
        with self._lock_for(account_id):
            position = self.position(account_id, order_id)
            if stage in {"TP1", "TP2"}:
                raise ExecutionError("EXIT_FILL_NOT_CONFIRMED")
            if stage not in {"CLOSE", "RUNNER"} or position.stage != "TP2_CONFIRMED":
                raise ExecutionError("EXIT_STAGE_NOT_READY")

    @staticmethod
    def _decimal(value: str | Decimal) -> Decimal:
        return Decimal(str(value))

    @staticmethod
    def _decimal_string(value: Decimal) -> str:
        return format(value.normalize(), "f")

    def record_exit_fill(
        self, account_id: str, order_id: str, external_deal_id: str,
        stage: Literal["TP1", "TP2", "RUNNER", "CLOSE"], volume: str,
    ) -> PositionCommand:
        """Project a confirmed reduce-only broker fill onto the Position."""
        with self._lock_for(account_id):
            position = self.position(account_id, order_id)
            existing_fill = self._fill_for_deal(account_id, external_deal_id)
            if existing_fill is not None:
                return next(
                    command
                    for command in self.position_commands
                    if command.account_id == account_id
                    and command.order_id == order_id
                    and command.reason == external_deal_id
                )
            requested = self._decimal(volume)
            remaining = self._decimal(position.remaining_volume or position.volume)
            if requested <= 0 or requested > remaining:
                raise ExecutionError("REDUCTION_EXCEEDS_EXPOSURE")
            if stage == "TP1" and position.stage != "ENTRY":
                raise ExecutionError("EXIT_STAGE_ALREADY_CONFIRMED")
            if stage == "TP2" and position.stage != "TP1_CONFIRMED":
                raise ExecutionError("EXIT_STAGE_NOT_READY")
            if stage == "RUNNER" and position.stage != "TP2_CONFIRMED":
                raise ExecutionError("EXIT_STAGE_NOT_READY")
            fill = Fill(str(uuid4()), account_id, order_id, external_deal_id, str(volume), True)
            self.fills[fill.id] = fill
            command = PositionCommand(str(uuid4()), account_id, order_id, stage, str(volume), reason=external_deal_id)
            command.status = "CONFIRMED"
            self.position_commands.append(command)
            position.remaining_volume = self._decimal_string(remaining - requested)
            if stage == "TP1":
                position.stage = "TP1_CONFIRMED"
            elif stage == "TP2":
                position.stage = "TP2_CONFIRMED"
                initial = self._decimal(position.volume)
                tp1_volume = sum(
                    (
                        self._decimal(command.requested_volume or "0")
                        for command in self.position_commands
                        if command.account_id == account_id
                        and command.order_id == order_id
                        and command.command_type == "TP1"
                    ),
                    Decimal("0"),
                )
                position.runner_volume = self._decimal_string(
                    initial - self._decimal(volume) - tp1_volume
                )
            elif position.remaining_volume == "0":
                position.stage = "CLOSED"
            return command

    def request_trailing(
        self, account_id: str, order_id: str, stop: str, *, direction: Literal["LONG", "SHORT"],
        closed_candle: bool, atomic_capability: bool,
    ) -> PositionCommand | None:
        """Create only capability-safe monotonic trailing commands."""
        with self._lock_for(account_id):
            position = self.position(account_id, order_id)
            if position.stage != "TP2_CONFIRMED":
                raise ExecutionError("TRAILING_NOT_READY")
            if not closed_candle:
                raise ExecutionError("TRAIL_WAITING_FOR_CLOSED_CANDLE")
            if not atomic_capability:
                position.last_confirmed_stop = position.last_confirmed_stop or position.native_stop_loss
                return None
            candidate = self._decimal(stop)
            prior = self._decimal(
                position.last_confirmed_stop or position.native_stop_loss or stop
            )
            if (direction == "LONG" and candidate <= prior) or (direction == "SHORT" and candidate >= prior):
                raise ExecutionError("TRAIL_NOT_TIGHTER")
            command = PositionCommand(str(uuid4()), account_id, order_id, "TRAIL", None, requested_stop=stop)
            self.position_commands.append(command)
            return command

    def confirm_trailing(self, account_id: str, order_id: str, command_id: str, stop: str) -> PositionCommand:
        with self._lock_for(account_id):
            command = next((item for item in self.position_commands if item.id == command_id), None)
            if command is None or command.account_id != account_id or command.order_id != order_id:
                raise ExecutionError("POSITION_COMMAND_NOT_FOUND")
            command.status = "CONFIRMED"
            command.confirmed_stop = stop
            self.position(account_id, order_id).last_confirmed_stop = stop
            return command

    def mark_protection_unknown(self, account_id: str, order_id: str) -> Position:
        with self._lock_for(account_id):
            position = self.position(account_id, order_id)
            position.protection_status = "QUARANTINED"
            self.account(account_id).exposure_gate = "QUARANTINED"
            self.install_fence(account_id, "PROTECTION_RECONCILIATION")
            self._set_runtime_interlock(
                account_id,
                reasons=("NATIVE_PROTECTION_UNCONFIRMED",),
                evidence={"position_id": order_id},
            )
            return position

    def mark_position_command_unknown(self, account_id: str, command_id: str) -> PositionCommand:
        with self._lock_for(account_id):
            command = next((item for item in self.position_commands if item.id == command_id), None)
            if command is None or command.account_id != account_id:
                raise ExecutionError("POSITION_COMMAND_NOT_FOUND")
            command.status = "UNKNOWN"
            self.mark_protection_unknown(account_id, command.order_id)
            return command

    def connector_disconnected(self, account_id: str, order_id: str) -> Position:
        """Fence new exposure on loss of connector while retaining broker-native safety."""
        with self._lock_for(account_id):
            position = self.position(account_id, order_id)
            if position.protection_status != "CONFIRMED":
                position.protection_status = "QUARANTINED"
                self.account(account_id).exposure_gate = "QUARANTINED"
                self._set_runtime_interlock(
                    account_id,
                    reasons=("NATIVE_PROTECTION_UNCONFIRMED",),
                    evidence={"position_id": order_id},
                )
            else:
                self.install_fence(account_id, "CONNECTOR_DISCONNECTED")
                self._set_runtime_interlock(
                    account_id,
                    reasons=("CONNECTOR_UNAVAILABLE",),
                    evidence={"position_id": order_id},
                )
            return position

    def confirm_protection(self, account_id: str, order_id: str) -> Position:
        """Confirm broker-native SL/TP before releasing a protection quarantine."""
        with self._lock_for(account_id):
            position = self.position(account_id, order_id)
            position.protection_status = "CONFIRMED"
            self._remove_runtime_interlock_reasons(
                account_id, PROTECTION_INTERLOCK_REASONS
            )
            return position

    def emergency_stop(self, account_id: str) -> SafetyFence:
        """Fence new exposure while leaving monitoring and exits available."""
        return self.install_fence(account_id, "EMERGENCY_STOP")

    def reconcile(self, account_id: str, order_id: str, connector: Any) -> DispatchResult:
        """Journal-first name used by the recovery command path."""
        return self.recover(account_id, order_id, connector)

    def position(self, account_id: str, order_id: str) -> Position:
        position = self.positions.get((account_id, order_id))
        if position is None:
            raise ExecutionError("POSITION_NOT_FOUND")
        return position

    def position_view(self, account_id: str, order_id: str) -> dict[str, Any]:
        """Return a fail-closed operational projection for one account."""
        with self._lock_for(account_id):
            position = self.position(account_id, order_id)
            order = self.orders.get(order_id)
            if order is None or order.account_id != account_id:
                raise ExecutionError("WRONG_ACCOUNT")
            view = {
                **position.__dict__,
                "position_id": order_id,
                "order_status": order.status,
                "protection_confirmed": position.protection_status == "CONFIRMED",
            }
            view["current_pnl"] = position.current_pnl or "UNKNOWN"
            return view

    def request_position_close(
        self, account_id: str, order_id: str, volume: str | None,
        confirmed_pair: str, reason: str, *, confirmed: bool = False,
        idempotency_key: str | None = None,
    ) -> PositionCommand:
        """Accept an explicitly confirmed reduce-only close/reduction intent."""
        with self._lock_for(account_id):
            order = self.orders.get(order_id)
            if order is None or order.account_id != account_id:
                raise ExecutionError("WRONG_ACCOUNT")
            position = self.position(account_id, order_id)
            expected_pair = position.pair or "UNKNOWN"
            if confirmed_pair != expected_pair:
                raise ExecutionError("POSITION_PAIR_MISMATCH")
            if not confirmed:
                raise ExecutionError("OPERATOR_CONFIRMATION_REQUIRED")
            if idempotency_key:
                prior_id = self._position_command_keys.get((account_id, idempotency_key))
                if prior_id:
                    return next(command for command in self.position_commands if command.id == prior_id)

            requested = None if volume in (None, "", "ALL") else str(volume)
            if requested is not None:
                requested_volume = self._decimal(requested)
                remaining_volume = self._decimal(position.remaining_volume or "0")
                if requested_volume <= 0:
                    raise ExecutionError("POSITION_VOLUME_INVALID")
                if requested_volume > remaining_volume:
                    raise ExecutionError("REDUCTION_EXCEEDS_EXPOSURE")
            if not reason.strip():
                raise ExecutionError("OPERATOR_REASON_REQUIRED")
            command = PositionCommand(
                id=str(uuid4()),
                account_id=account_id,
                order_id=order_id,
                command_type="CLOSE",
                requested_volume=requested,
                reduce_only=True,
                reason=reason,
                idempotency_key=idempotency_key,
            )
            self.position_commands.append(command)
            if idempotency_key:
                self._position_command_keys[(account_id, idempotency_key)] = command.id
            position.stage = "CLOSING"
            return command

    def request_position_modify_protection(
        self, account_id: str, order_id: str, stop_loss: str | None,
        take_profit: str | None, confirmed_pair: str, reason: str,
        *, confirmed: bool = False, idempotency_key: str | None = None,
    ) -> PositionCommand:
        """Create an explicitly approved native protection modification."""
        with self._lock_for(account_id):
            order = self.orders.get(order_id)
            if order is None or order.account_id != account_id:
                raise ExecutionError("WRONG_ACCOUNT")
            position = self.position(account_id, order_id)
            if confirmed_pair != (position.pair or "UNKNOWN"):
                raise ExecutionError("POSITION_PAIR_MISMATCH")
            if not confirmed:
                raise ExecutionError("OPERATOR_CONFIRMATION_REQUIRED")
            if stop_loss is None and take_profit is None:
                raise ExecutionError("PROTECTION_REQUIRED")
            if not reason.strip():
                raise ExecutionError("OPERATOR_REASON_REQUIRED")
            if idempotency_key:
                prior_id = self._position_command_keys.get((account_id, idempotency_key))
                if prior_id:
                    return next(command for command in self.position_commands if command.id == prior_id)
            command = PositionCommand(
                id=str(uuid4()), account_id=account_id, order_id=order_id,
                command_type="PROTECTION", requested_volume=None,
                requested_stop=str(stop_loss) if stop_loss is not None else None,
                requested_take_profit=str(take_profit) if take_profit is not None else None,
                reason=reason, idempotency_key=idempotency_key,
            )
            self.position_commands.append(command)
            if idempotency_key:
                self._position_command_keys[(account_id, idempotency_key)] = command.id
            return command

    request_position_protection = request_position_modify_protection

    def install_fence(self, account_id: str, kind: str = "SAFETY_FENCE") -> SafetyFence:
        account = self.account(account_id)
        account.fence_sequence += 1
        if account.exposure_gate != "QUARANTINED":
            account.exposure_gate = "FENCE_PENDING"
        account.execution_epoch += 1
        return SafetyFence(account_id, account.fence_sequence, kind)


def _serialize_state_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, dict):
        return {
            str(key): _serialize_state_value(item) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_serialize_state_value(item) for item in value]
    return value


def _deserialize_state_value(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__datetime__"}:
            return datetime.fromisoformat(value["__datetime__"])
        return {
            key: _deserialize_state_value(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_deserialize_state_value(item) for item in value]
    return value


def _state_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, dict) and "__datetime__" in value:
        return _as_utc(datetime.fromisoformat(value["__datetime__"]))
    return _as_utc(datetime.fromisoformat(value))


class ExecutionCoordinator(ExecutionSubstrate):
    """Durable account-scoped execution coordination.

    The coordinator keeps the existing in-memory model as its working set and
    writes a complete account-local journal state after each public mutation.
    The broker adapter is only called after the intent and reservation are
    stored. A second coordinator can load the same store after a restart.
    """

    def __init__(
        self,
        state_store: ExecutionStateStore | None = None,
        *,
        state_path: str | os.PathLike[str] | None = None,
        database_url: str | None = None,
        account_identity_provider: Any | None = None,
        reconciliation_deadline: timedelta = timedelta(minutes=5),
        max_protection_repair_attempts: int = 3,
    ) -> None:
        super().__init__()
        if reconciliation_deadline <= timedelta(0):
            raise ValueError("reconciliation_deadline must be positive")
        if max_protection_repair_attempts < 1:
            raise ValueError("max_protection_repair_attempts must be positive")
        configured_stores = sum(
            value is not None for value in (state_store, state_path, database_url)
        )
        if configured_stores > 1:
            raise ValueError("execution state store options are mutually exclusive")
        if state_store is not None:
            self._state_store = state_store
        elif state_path is not None:
            self._state_store = JsonExecutionStore(state_path)
        elif database_url:
            self._state_store = PostgresExecutionStore(database_url)
        else:
            self._state_store = None
        self._mutation_depth = 0
        self._account_identity_provider = account_identity_provider
        self._account_identities: dict[str, dict[str, str]] = {}
        self.reconciliation_deadline = reconciliation_deadline
        self.max_protection_repair_attempts = max_protection_repair_attempts
        self.reconciliation_work: dict[str, ReconciliationWork] = {}
        self.audit_events: list[AuditEvent] = []
        self.risk_assessments: dict[str, dict[str, Any]] = {}
        if self._state_store is not None:
            self._restore(self._state_store.load())
            with self._mutation():
                self._begin_restart_recovery()
                self._recover_inflight_dispatches()

    def bind_account_identity(self, account_id: str, identity: dict[str, str]) -> None:
        """Bind the immutable BrokerAccount identity used by connector dispatch."""
        if not isinstance(account_id, str) or not account_id:
            raise ExecutionError("WRONG_ACCOUNT")
        if set(identity) != {"provider", "broker_server", "external_account_id"}:
            raise ExecutionError("INVALID_ACCOUNT_IDENTITY")
        if any(not isinstance(value, str) or not value for value in identity.values()):
            raise ExecutionError("INVALID_ACCOUNT_IDENTITY")
        prior = self._account_identities.get(account_id)
        if prior is not None and prior != identity:
            raise ExecutionError("ACCOUNT_IDENTITY_MISMATCH")
        self._account_identities[account_id] = dict(identity)

    def _expected_account_identity(self, account_id: str) -> dict[str, str] | None:
        if self._account_identity_provider is not None:
            try:
                value = self._account_identity_provider(account_id)
            except (KeyError, AttributeError, TypeError):
                value = None
            if hasattr(value, "identity"):
                value = value.identity
            if isinstance(value, tuple):
                value = dict(zip(("provider", "broker_server", "external_account_id"), value))
            if isinstance(value, dict) and set(value) == {
                "provider", "broker_server", "external_account_id"
            }:
                return {key: str(item) for key, item in value.items()}
        bound = self._account_identities.get(account_id)
        return dict(bound) if bound is not None else None

    def _validate_connector_identity(self, account_id: str, identity: dict[str, str]) -> None:
        if set(identity) != {"provider", "broker_server", "external_account_id"}:
            raise ExecutionError("INVALID_ACCOUNT_IDENTITY")
        if any(not isinstance(value, str) or not value for value in identity.values()):
            raise ExecutionError("INVALID_ACCOUNT_IDENTITY")
        expected = self._expected_account_identity(account_id)
        if self._account_identity_provider is not None and expected is None:
            raise ExecutionError("ACCOUNT_IDENTITY_UNAVAILABLE")
        if expected is not None and identity != expected:
            raise ExecutionError("ACCOUNT_IDENTITY_MISMATCH")

    def _snapshot(self) -> dict[str, Any]:
        return _serialize_state_value(
            {
                "accounts": {
                    key: value.__dict__ for key, value in self._accounts.items()
                },
                "account_identities": self._account_identities,
                "reservations": {
                    key: value.__dict__ for key, value in self.reservations.items()
                },
                "orders": {key: value.__dict__ for key, value in self.orders.items()},
                "events": {key: value.__dict__ for key, value in self.events.items()},
                "dispatch_records": {
                    key: value.__dict__ for key, value in self.dispatch_records.items()
                },
                "journal": {
                    key: value.__dict__ for key, value in self.journal.items()
                },
                "fills": {key: value.__dict__ for key, value in self.fills.items()},
                "positions": {
                    f"{account_id}|{position_id}": value.__dict__
                    for (account_id, position_id), value in self.positions.items()
                },
                "position_commands": [
                    value.__dict__ for value in self.position_commands
                ],
                "commands": {
                    key: value.__dict__ for key, value in self.commands.items()
                },
                "order_reservations": self._order_reservations,
                "risk_assessments": self.risk_assessments,
                "reconciliation_work": {
                    key: value.__dict__
                    for key, value in self.reconciliation_work.items()
                },
                "critical_alerts": {
                    key: value.__dict__ for key, value in self._critical_alerts.items()
                },
                "protection_repairs": {
                    f"{account_id}|{position_id}": value
                    for (account_id, position_id), value in self.protection_repairs.items()
                },
                "calendar_blocks": self.calendar_blocks,
                "calendar_overrides": self.calendar_overrides,
                "global_emergencies": {
                    key: {
                        **value.__dict__,
                        "targets": {
                            target_id: target.__dict__
                            for target_id, target in value.targets.items()
                        },
                    }
                    for key, value in self.global_emergencies.items()
                },
                "audit_events": [value.__dict__ for value in self.audit_events],
            }
        )

    def _save(self) -> None:
        if self._state_store is not None:
            self._state_store.save(self._snapshot())

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        """Checkpoint once after the outermost successful public mutation."""
        self._mutation_depth += 1
        try:
            yield
        except BaseException:
            self._mutation_depth -= 1
            raise
        else:
            self._mutation_depth -= 1
            if self._mutation_depth == 0:
                self._save()

    def _restore(self, state: dict[str, Any] | None) -> None:
        if not state:
            return
        state = _deserialize_state_value(state)
        self._account_identities = {
            str(account_id): dict(identity)
            for account_id, identity in state.get("account_identities", {}).items()
        }
        for key, value in state.get("accounts", {}).items():
            value["interlock_reasons"] = tuple(value.get("interlock_reasons", ()))
            for timestamp_key in (
                "interlock_updated_at",
                "recovery_started_at",
                "recovery_completed_at",
            ):
                if value.get(timestamp_key) is not None:
                    value[timestamp_key] = _state_datetime(value[timestamp_key])
            self._accounts[key] = AccountExecutionState(**value)
        for key, value in state.get("reservations", {}).items():
            self.reservations[key] = RiskReservation(**value)
        for key, value in state.get("orders", {}).items():
            self.orders[key] = OrderIntent(**value)
        for key, value in state.get("events", {}).items():
            self.events[key] = OutboxEvent(**value)
        for key, value in state.get("dispatch_records", {}).items():
            self.dispatch_records[key] = ConnectorDispatchRecord(**value)
        for key, value in state.get("journal", {}).items():
            value["observed_at"] = _state_datetime(value["observed_at"])
            self.journal[key] = ConnectorJournalEntry(**value)
        for key, value in state.get("fills", {}).items():
            self.fills[key] = Fill(**value)
        for key, value in state.get("positions", {}).items():
            account_id, position_id = key.split("|", 1)
            self.positions[(account_id, position_id)] = Position(**value)
        self.position_commands = [
            PositionCommand(**value) for value in state.get("position_commands", [])
        ]
        for command in self.position_commands:
            if command.idempotency_key:
                self._position_command_keys[
                    (command.account_id, command.idempotency_key)
                ] = command.id
        for key, value in state.get("commands", {}).items():
            self.commands[key] = OperatorCommand(**value)
        self.audit_events = [
            AuditEvent(
                id=value["id"], account_id=value["account_id"],
                event_type=value["event_type"], payload=value["payload"],
                occurred_at=_state_datetime(value["occurred_at"]),
            )
            for value in state.get("audit_events", [])
        ]
        self.risk_assessments = state.get("risk_assessments", {})
        for key, value in state.get("reconciliation_work", {}).items():
            value["first_seen_at"] = _state_datetime(value["first_seen_at"])
            value["deadline_at"] = _state_datetime(value["deadline_at"])
            if value.get("last_attempt_at") is not None:
                value["last_attempt_at"] = _state_datetime(value["last_attempt_at"])
            self.reconciliation_work[key] = ReconciliationWork(**value)
        for key, value in state.get("critical_alerts", {}).items():
            value["created_at"] = _state_datetime(value["created_at"])
            if value.get("resolved_at") is not None:
                value["resolved_at"] = _state_datetime(value["resolved_at"])
            self._critical_alerts[key] = CriticalAlert(**value)
        for key, value in state.get("protection_repairs", {}).items():
            account_id, position_id = key.split("|", 1)
            self.protection_repairs[(account_id, position_id)] = value
        self.calendar_blocks = state.get("calendar_blocks", {})
        self.calendar_overrides = state.get("calendar_overrides", {})
        for key, value in state.get("global_emergencies", {}).items():
            targets = {
                target_id: GlobalEmergencyTarget(**target)
                for target_id, target in value.get("targets", {}).items()
            }
            self.global_emergencies[key] = GlobalEmergencyOperation(
                id=value["id"], requested_kind=value["requested_kind"],
                target_account_ids=tuple(value["target_account_ids"]),
                status=value["status"], version=value["version"], targets=targets,
            )
        for order in self.orders.values():
            reservation_id = state.get("order_reservations", {}).get(order.id)
            if reservation_id is None:
                reservation_id = next(
                    reservation.id for reservation in self.reservations.values()
                    if reservation.account_id == order.account_id
                    and reservation.signal_id == order.signal_id
                )
            self._order_reservations[order.id] = reservation_id
            self._idempotency[(order.account_id, order.idempotency_key)] = (
                order.id, order.canonical_hash
            )
        for command in self.commands.values():
            self._command_keys[(command.account_id, command.idempotency_key)] = command.id
            if command.kind == "APPROVE_SIGNAL" and command.signal_id:
                self._approved_signals[(command.account_id, command.signal_id)] = command.id

    def _recover_inflight_dispatches(self) -> None:
        """Turn pre-crash side-effect checkpoints into journal-first recovery work."""
        for order in self.orders.values():
            self._recover_inflight_subject(
                order, "ORDER", journal=self.journal.get(order.id)
            )
        for command in self.position_commands:
            self._recover_inflight_subject(command, "POSITION_COMMAND")
        for command in self.commands.values():
            self._recover_inflight_subject(command, "COMMAND")

    def _begin_restart_recovery(self) -> None:
        """Fence every restored account until its complete broker snapshot arrives."""
        for account in self._accounts.values():
            if account.recovery_status == "RECOVERING":
                continue
            account.recovery_status = "RECOVERING"
            account.recovery_required = True
            account.recovery_gate_before = account.exposure_gate
            account.recovery_started_at = _now()
            account.recovery_completed_at = None
            account.interlock_evidence = {
                **account.interlock_evidence,
                "recovery_started_at": account.recovery_started_at.isoformat(),
            }
            if account.runtime_interlock != "QUARANTINED":
                account.runtime_interlock = "BLOCKED"
            if account.exposure_gate == "OPEN":
                account.exposure_gate = "FENCE_PENDING"
            self._audit(
                account.account_id,
                "execution.recovery.started",
                reason=RESTART_RECOVERY_REASON,
            )

    def _has_unresolved_reconciliation(self, account_id: str) -> bool:
        return any(
            work.account_id == account_id
            and work.status in UNRESOLVED_RECONCILIATION_STATUSES
            for work in self.reconciliation_work.values()
        )

    def _complete_restart_recovery(
        self, account_id: str, *, observed_at: datetime | None = None
    ) -> None:
        account = self.account(account_id)
        if not account.recovery_required:
            return
        if self._has_unresolved_reconciliation(account_id):
            return
        if account.quarantine_requires_command:
            return
        account.interlock_reasons = tuple(
            reason
            for reason in account.interlock_reasons
            if reason != RESTART_RECOVERY_REASON
        )
        account.recovery_status = "READY"
        account.recovery_required = False
        account.recovery_completed_at = _as_utc(observed_at or _now())
        if not account.interlock_reasons:
            account.runtime_interlock = "ELIGIBLE"
            if account.recovery_gate_before == "OPEN":
                account.exposure_gate = "OPEN"
            elif account.exposure_gate != "OPEN":
                account.exposure_gate = account.recovery_gate_before
        self._audit(
            account_id,
            "execution.recovery.completed",
            completed_at=account.recovery_completed_at.isoformat(),
            exposure_gate=account.exposure_gate,
        )

    def recovery_snapshot(self, account_id: str) -> dict[str, Any]:
        """Return restart recovery progress without exposing another account."""
        account = self.account(account_id)
        return {
            "account_id": account_id,
            "status": account.recovery_status,
            "required": account.recovery_required,
            "exposure_gate": account.exposure_gate,
            "runtime_interlock": account.runtime_interlock,
            "reasons": list(account.interlock_reasons),
            "recovery_reason": (
                RESTART_RECOVERY_REASON
                if account.recovery_required
                else None
            ),
            "started_at": (
                account.recovery_started_at.isoformat()
                if account.recovery_started_at
                else None
            ),
            "completed_at": (
                account.recovery_completed_at.isoformat()
                if account.recovery_completed_at
                else None
            ),
            "pending_work": self.recovery_records(account_id),
        }

    def _recover_inflight_subject(
        self,
        subject: OrderIntent | PositionCommand | OperatorCommand,
        subject_kind: Literal["ORDER", "POSITION_COMMAND", "COMMAND"],
        *,
        journal: ConnectorJournalEntry | None = None,
    ) -> None:
        journal_is_inflight = (
            subject_kind == "ORDER"
            and journal is not None
            and journal.state == "DISPATCHING"
        )
        if subject.status == "DISPATCHING" or journal_is_inflight:
            subject.status = "UNKNOWN"
            observed_at = journal.observed_at if journal is not None else None
            self._record_unknown(
                subject.account_id,
                subject.id,
                now=observed_at if subject_kind == "ORDER" else None,
                subject_kind=subject_kind,
            )
            return
        if subject.status == "UNKNOWN" and self._work_for(
            subject.account_id, subject.id, subject_kind
        ) is None:
            self._record_unknown(
                subject.account_id, subject.id, subject_kind=subject_kind
            )

    def _audit(self, account_id: str, event_type: str, **payload: Any) -> None:
        self.audit_events.append(
            AuditEvent(str(uuid4()), account_id, event_type, payload, _now())
        )

    def _before_connector_call(self, subject: Any) -> None:
        """Make the journal/checkpoint visible before invoking broker I/O."""
        self._save()

    def _work_for(
        self, account_id: str, subject_id: str,
        subject_kind: Literal["ORDER", "POSITION_COMMAND", "COMMAND"] | None = None,
    ) -> ReconciliationWork | None:
        return next(
            (
                item for item in self.reconciliation_work.values()
                if item.account_id == account_id and item.subject_id == subject_id
                and (subject_kind is None or item.subject_kind == subject_kind)
                and item.status in UNRESOLVED_RECONCILIATION_STATUSES
            ),
            None,
        )

    def _record_unknown(
        self, account_id: str, order_id: str, *, now: datetime | None = None,
        subject_kind: Literal["ORDER", "POSITION_COMMAND", "COMMAND"] = "ORDER",
    ) -> ReconciliationWork:
        """Persist recovery work before the account is fenced from new exposure."""
        existing = self._work_for(account_id, order_id, subject_kind)
        if existing is not None:
            return existing
        observed_at = now or _now()
        account = self.account(account_id)
        if subject_kind == "ORDER" and order_id not in self.journal:
            order = self.orders.get(order_id)
            if order is not None:
                self.journal[order_id] = ConnectorJournalEntry(
                    str(uuid4()), account_id, order_id, order.dispatch_sequence,
                    "DISPATCHING", observed_at=observed_at,
                )
        work = ReconciliationWork(
            id=str(uuid4()), account_id=account_id, subject_id=order_id,
            subject_kind=subject_kind, first_seen_at=observed_at,
            deadline_at=observed_at + self.reconciliation_deadline,
            entry_gate_before=account.exposure_gate,
        )
        self.reconciliation_work[work.id] = work
        account.exposure_gate = "QUARANTINED"
        self.install_fence(account_id, "UNKNOWN_RECONCILIATION")
        self._set_runtime_interlock(
            account_id,
            reasons=("RECONCILIATION_PENDING",),
            evidence={"subject_id": order_id, "subject_kind": subject_kind},
        )
        self._audit(
            account_id, "execution.reconciliation.pending", order_id=order_id,
            deadline_at=work.deadline_at.isoformat(), subject_kind=subject_kind,
        )
        return work

    def _restore_temporary_entry_eligibility(self, account_id: str) -> None:
        """Reopen only the temporary UNKNOWN fence after all local facts converge."""
        account_work = [
            item for item in self.reconciliation_work.values()
            if item.account_id == account_id
        ]
        unresolved = self._has_unresolved_reconciliation(account_id)
        protection_unconfirmed = any(
            position.account_id == account_id
            and position.protection_status != "CONFIRMED"
            for position in self.positions.values()
        )
        account = self.account(account_id)
        prior_gates = {item.entry_gate_before for item in account_work}
        if "STOPPED" in prior_gates:
            prior_gate = "STOPPED"
        elif "FENCE_PENDING" in prior_gates:
            prior_gate = "FENCE_PENDING"
        else:
            prior_gate = "OPEN"
        if (
            not unresolved
            and not protection_unconfirmed
            and not account.interlock_reasons
            and not account.quarantine_requires_command
            and account.exposure_gate == "QUARANTINED"
        ):
            if prior_gate == "OPEN":
                account.exposure_gate = "OPEN"
                account.runtime_interlock = "ELIGIBLE"
                self._audit(account_id, "execution.reconciliation.recovered")
            elif prior_gate in {"FENCE_PENDING", "STOPPED"}:
                account.exposure_gate = prior_gate

    def _complete_work_if_converged(self, account_id: str, order_id: str) -> None:
        order = self.orders.get(order_id)
        if order is None or order.account_id != account_id or order.status == "UNKNOWN":
            return
        work = self._work_for(account_id, order_id, "ORDER")
        if work is not None and work.status == "PENDING":
            work.status = "RECOVERED"
            self._remove_runtime_interlock_reasons(
                account_id, {"RECONCILIATION_PENDING"}
            )
            self._audit(
                account_id, "execution.reconciliation.converged",
                order_id=order_id, status=order.status,
            )
        self._restore_temporary_entry_eligibility(account_id)

    def _complete_non_order_work_if_converged(
        self, account_id: str, subject_id: str,
        subject_kind: Literal["POSITION_COMMAND", "COMMAND"],
    ) -> None:
        work = self._work_for(account_id, subject_id, subject_kind)
        if work is None or work.status != "PENDING":
            return
        work.status = "RECOVERED"
        self._remove_runtime_interlock_reasons(
            account_id, {"RECONCILIATION_PENDING"}
        )
        self._audit(
            account_id, "execution.reconciliation.converged",
            subject_id=subject_id, subject_kind=subject_kind,
        )
        self._restore_temporary_entry_eligibility(account_id)

    def advance_recovery_deadlines(
        self, *, now: datetime | None = None, account_id: str | None = None,
    ) -> tuple[ReconciliationWork, ...]:
        """Escalate overdue work even while its connector is disconnected."""
        at = now or _now()
        escalated: list[ReconciliationWork] = []
        with self._mutation():
            for work in self.reconciliation_work.values():
                if (
                    (account_id is None or work.account_id == account_id)
                    and work.status == "PENDING"
                    and at >= work.deadline_at
                ):
                    work.status = "ESCALATED"
                    escalated.append(work)
                    self._audit(
                        work.account_id, "execution.reconciliation.escalated",
                        subject_id=work.subject_id,
                        subject_kind=work.subject_kind,
                        deadline_at=work.deadline_at.isoformat(),
                    )
        return tuple(escalated)

    def recovery_records(self, account_id: str) -> list[dict[str, Any]]:
        """Dashboard-safe, account-scoped recovery progress."""
        with self._lock_for(account_id):
            records: list[dict[str, Any]] = []
            for item in self.reconciliation_work.values():
                if item.account_id != account_id:
                    continue
                record = {
                    "kind": item.subject_kind,
                    "order_id": item.subject_id,
                    "subject_id": item.subject_id,
                    "status": item.status,
                    "reason": item.reason,
                    "first_seen_at": item.first_seen_at.isoformat(),
                    "deadline_at": item.deadline_at.isoformat(),
                    "attempts": item.attempts,
                    "critical": item.status == "ESCALATED",
                    "recovery_legal": item.status == "PENDING",
                }
                # Backend recovery work is keyed by the domain Order ID. The
                # connector journal is keyed by the dispatched command ID.
                # Carry both identifiers so reconciliation can resolve the
                # local row without changing the account-facing subject.
                if item.subject_kind == "ORDER":
                    order = self.orders.get(item.subject_id)
                    if order is not None and order.account_id == account_id:
                        command_id = order.command_id or order.id
                        record["command_id"] = command_id
                records.append(record)
            return records

    def _accept_execution(
        self,
        *,
        account_id: str,
        signal_id: str,
        idempotency_key: str,
        canonical_hash: str,
        execution_epoch: int,
        risk_assessment: RiskAssessment | None,
        signal_revision: int,
        order_payload: dict[str, Any],
        risk_amount: str,
    ) -> PreOrderResult:
        if risk_assessment is None:
            raise ExecutionError("PRE_ORDER_ASSESSMENT_REQUIRED")
        return self.accept_execution(
            account_id=account_id,
            signal_id=signal_id,
            idempotency_key=idempotency_key,
            canonical_hash=canonical_hash,
            execution_epoch=execution_epoch,
            risk_assessment=risk_assessment,
            signal_revision=signal_revision,
            order_payload=order_payload,
            risk_amount=risk_amount,
        )

    def accept_execution(
        self,
        *,
        account_id: str,
        signal_id: str,
        idempotency_key: str,
        canonical_hash: str,
        execution_epoch: int,
        risk_assessment: RiskAssessment,
        order_payload: dict[str, Any],
        signal_revision: int,
        risk_amount: str | Decimal = "0",
        now: datetime | None = None,
    ) -> PreOrderResult:
        """Atomically accept a fresh assessment, reservation, and Order intent."""
        with self._mutation(), self._lock_for(account_id):
            prior = self._idempotent_result(
                account_id, idempotency_key, canonical_hash
            )
            if prior is not None:
                return prior
            result = self.pre_order(
                account_id=account_id,
                signal_id=signal_id,
                idempotency_key=idempotency_key,
                canonical_hash=canonical_hash,
                execution_epoch=execution_epoch,
                risk_assessment=risk_assessment,
                signal_revision=signal_revision,
                order_payload=order_payload,
                risk_amount=str(risk_amount),
                now=now,
            )
            requested_volume = order_payload.get(
                "requested_volume", order_payload.get("volume")
            )
            if requested_volume is not None:
                result.order.requested_volume = str(requested_volume)
                result.order.remaining_volume = str(requested_volume)
            result.order.risk_assessment_id = getattr(risk_assessment, "id", None)
            assessment_id = result.order.risk_assessment_id or str(uuid4())
            result.order.risk_assessment_id = assessment_id
            self.risk_assessments[assessment_id] = {
                "id": assessment_id,
                "account_id": account_id,
                "signal_id": signal_id,
                "signal_revision": risk_assessment.signal_revision,
                "purpose": risk_assessment.purpose,
                "approved": risk_assessment.approved,
                "assessed_at": risk_assessment.assessed_at,
                "valid_until": risk_assessment.valid_until,
                "reason_codes": tuple(risk_assessment.reason_codes),
            }
            command = self._command(
                account_id=account_id,
                signal_id=signal_id,
                kind="EXECUTE_SIGNAL",
                idempotency_key=idempotency_key,
                reason="Execution Coordination accepted the order",
                confirmed=True,
            )
            command.order_id = result.order.id
            result.order.command_id = command.id
            self._audit(
                account_id,
                "execution.intent.accepted",
                command_id=command.id,
                order_id=result.order.id,
            )
            return result

    def pre_order(self, **kwargs: Any) -> PreOrderResult:
        with self._mutation():
            return super().pre_order(**kwargs)

    def approve_signal(self, **kwargs: Any) -> OperatorCommand:
        with self._mutation():
            return super().approve_signal(**kwargs)

    def execute_signal(self, **kwargs: Any) -> PreOrderResult:
        with self._mutation():
            result = super().execute_signal(**kwargs)
            command = self.commands.get(
                self._command_keys.get(
                    (kwargs["account_id"], kwargs["idempotency_key"]), ""
                )
            )
            if command is not None:
                result.order.command_id = command.id
            return result

    def schedule_automated_signal(self, **kwargs: Any) -> PreOrderResult:
        with self._mutation():
            return super().schedule_automated_signal(**kwargs)

    def prepare_connector_dispatch(
        self,
        account_id: str,
        order_id: str,
        *,
        identity: dict[str, str],
        generation: int,
    ) -> ConnectorDispatchRecord:
        """Create the durable delivery row after the execution intent commits."""
        with self._mutation(), self._lock_for(account_id):
            order = self.orders.get(order_id)
            if order is None or order.account_id != account_id:
                raise ExecutionError("WRONG_ACCOUNT")
            self._validate_connector_identity(account_id, identity)
            command_id = order.command_id or order.id
            existing = self.dispatch_records.get(command_id)
            if existing is not None:
                if (
                    existing.account_id != account_id
                    or existing.idempotency_key != order.idempotency_key
                    or existing.request_hash != order.canonical_hash
                ):
                    raise ExecutionError("COMMAND_CONTEXT_MISMATCH")
                return existing
            record = ConnectorDispatchRecord(
                command_id=command_id,
                account_id=account_id,
                identity=dict(identity),
                command_type="order.submit_market",
                dispatch_sequence=order.dispatch_sequence,
                generation=generation,
                execution_epoch=order.execution_epoch,
                idempotency_key=order.idempotency_key,
                request_hash=order.canonical_hash,
                payload=json.loads(json.dumps(order.payload)),
            )
            self.dispatch_records[command_id] = record
            self._audit(account_id, "execution.dispatch.queued", command_id=command_id)
            return record

    def prepare_connector_position_dispatch(
        self,
        account_id: str,
        command_id: str,
        *,
        identity: dict[str, str],
        generation: int,
    ) -> ConnectorDispatchRecord:
        """Create the same durable delivery row for a position command."""
        with self._mutation(), self._lock_for(account_id):
            command = next((item for item in self.position_commands if item.id == command_id), None)
            if command is None or command.account_id != account_id:
                raise ExecutionError("WRONG_ACCOUNT")
            self._validate_connector_identity(account_id, identity)
            existing = self.dispatch_records.get(command_id)
            position = self.position(account_id, command.order_id)
            command_types = {"PROTECTION": "position.modify_protection", "CLOSE": "position.close"}
            command_type = command_types.get(command.command_type)
            if command_type is None:
                raise ExecutionError("UNSUPPORTED_POSITION_COMMAND")
            position_ticket = position.external_position_id
            if position_ticket is None or not str(position_ticket).strip():
                raise ExecutionError("POSITION_TICKET_REQUIRED")
            if command_type == "position.modify_protection":
                payload = {
                    "position_ticket": str(position_ticket),
                    "symbol": position.pair or "",
                    "sl": command.requested_stop,
                    "tp": command.requested_take_profit,
                    "expected_position_version": position.version,
                }
            else:
                payload = {
                    "position_ticket": str(position_ticket),
                    "symbol": position.pair or "",
                    "direction": position.direction or "LONG",
                    "volume": command.requested_volume or position.remaining_volume,
                    "expected_position_volume": position.remaining_volume,
                    "comment": "te:" + command.id,
                }
            request_hash = hashlib.sha256(
                json.dumps(
                    {"command_type": command_type, "payload": payload},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            if existing is not None:
                if existing.account_id != account_id or existing.request_hash != request_hash:
                    raise ExecutionError("COMMAND_CONTEXT_MISMATCH")
                return existing
            account = self.account(account_id)
            dispatch_sequence = account.next_dispatch_sequence
            account.next_dispatch_sequence += 1
            record = ConnectorDispatchRecord(
                command_id=command.id, account_id=account_id, identity=dict(identity),
                command_type=command_type, dispatch_sequence=dispatch_sequence,
                generation=generation, execution_epoch=account.execution_epoch,
                idempotency_key=command.idempotency_key or command.id,
                request_hash=request_hash, payload=payload,
            )
            self.dispatch_records[command.id] = record
            self._audit(account_id, "execution.position_dispatch.queued", command_id=command.id)
            return record

    def connector_dispatch_result(
        self,
        account_id: str,
        command_id: str,
        state: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> ConnectorDispatchRecord:
        """Persist a validated connector result before transport acknowledgement."""
        with self._mutation(), self._lock_for(account_id):
            record = self.dispatch_records.get(command_id)
            if record is None or record.account_id != account_id:
                raise ExecutionError("UNKNOWN_COMMAND")
            if state not in {"ACCEPTED", "REJECTED", "UNKNOWN"}:
                raise ExecutionError("INVALID_COMMAND_RESULT")
            if record.state in {"ACCEPTED", "REJECTED", "UNKNOWN"}:
                if record.state == state:
                    return record
                raise ExecutionError("STALE_COMMAND_RESULT")
            result_payload = dict(payload or {})
            if (
                state == "ACCEPTED"
                and record.command_type == "position.modify_protection"
                and result_payload.get("protection_confirmed") is not True
            ):
                state = "UNKNOWN"
                result_payload.setdefault("code", "PROTECTION_READBACK_REQUIRED")
            if (
                state == "ACCEPTED"
                and record.command_type == "position.close"
                and result_payload.get("filled_volume") in (None, "", 0, "0")
            ):
                # A requested close volume is intent, not broker evidence.
                # Keep the command fenced until a deal/read-back supplies the
                # actual reduction.
                state = "UNKNOWN"
                result_payload.setdefault("code", "CLOSE_READBACK_REQUIRED")
            record.state = state
            record.result_payload = result_payload
            position_command = next(
                (item for item in self.position_commands if item.id == command_id), None,
            )
            if position_command is not None and position_command.account_id == account_id:
                position = self.position(account_id, position_command.order_id)
                if state == "ACCEPTED":
                    position_command.status = "CONFIRMED"
                    if record.command_type == "position.modify_protection":
                        position.protection_status = "CONFIRMED"
                        if position_command.requested_stop is not None:
                            position.native_stop_loss = position_command.requested_stop
                            position.last_confirmed_stop = position_command.requested_stop
                        if position_command.requested_take_profit is not None:
                            position.native_take_profit = position_command.requested_take_profit
                        position.version += 1
                    else:
                        volume = result_payload.get("filled_volume", position_command.requested_volume)
                        if volume is not None:
                            remaining = max(
                                Decimal("0"),
                                self._decimal(position.remaining_volume or position.volume) - self._decimal(str(volume)),
                            )
                            position.remaining_volume = self._decimal_string(remaining)
                            if remaining == 0:
                                position.stage = "CLOSED"
                        position.version += 1
                elif state == "REJECTED":
                    position_command.status = "REJECTED"
                else:
                    position_command.status = "UNKNOWN"
                    self._record_unknown(account_id, command_id, subject_kind="POSITION_COMMAND")
                    if record.command_type == "position.modify_protection":
                        self.mark_protection_unknown(account_id, position_command.order_id)
                self._audit(account_id, "execution.position_dispatch.result", command_id=command_id, state=state)
                return record
            order_id = next(
                (item.id for item in self.orders.values() if item.command_id == command_id),
                command_id,
            )
            order = self.orders.get(order_id)
            if order is not None and order.account_id == account_id:
                journal = self.journal.get(order.id)
                if state == "ACCEPTED":
                    order.status = "SUBMITTED"
                    self._event_for(order.id).status = "PUBLISHED"
                    if journal is not None:
                        journal.state = "ACCEPTED"
                        external_id = result_payload.get("external_id")
                        journal.external_id = str(external_id) if external_id is not None else None
                elif state == "REJECTED":
                    order.status = "REJECTED"
                    self._event_for(order.id).status = "ABORTED"
                    self._reservation_for(order.id).status = "RELEASED"
                    if journal is not None:
                        journal.state = "REJECTED"
                else:
                    order.status = "UNKNOWN"
                    self._record_unknown(account_id, order.id)
            self._audit(account_id, "execution.dispatch.result", command_id=command_id, state=state)
            return record

    def pending_connector_dispatches(self, account_id: str) -> tuple[ConnectorDispatchRecord, ...]:
        with self._lock_for(account_id):
            return tuple(sorted(
                (item for item in self.dispatch_records.values()
                 if item.account_id == account_id and item.state == "QUEUED"),
                key=lambda item: item.dispatch_sequence,
            ))

    def mark_connector_dispatch_sent(self, account_id: str, command_id: str) -> ConnectorDispatchRecord:
        with self._mutation(), self._lock_for(account_id):
            record = self.dispatch_records.get(command_id)
            if record is None or record.account_id != account_id:
                raise ExecutionError("UNKNOWN_COMMAND")
            if record.state == "QUEUED":
                record.state = "SENT"
            elif record.state != "SENT":
                raise ExecutionError("INVALID_PENDING_COMMAND")
            return record

    def mark_connector_session_lost(self, account_id: str) -> tuple[ConnectorDispatchRecord, ...]:
        """Fence sent commands after disconnect; never replay an ambiguous send."""
        with self._mutation(), self._lock_for(account_id):
            changed: list[ConnectorDispatchRecord] = []
            for record in self.dispatch_records.values():
                if record.account_id == account_id and record.state == "SENT":
                    self.connector_dispatch_result(account_id, record.command_id, "UNKNOWN", payload={"reason": "SESSION_LOST"})
                    changed.append(record)
            return tuple(changed)

    def dispatch_next(self, account_id: str, connector: BrokerAdapter) -> DispatchResult:
        with self._mutation():
            result = super().dispatch_next(account_id, connector)
            if result.status == "UNKNOWN":
                self._record_unknown(account_id, result.order_id)
            self._audit(
                account_id,
                "execution.dispatch",
                order_id=result.order_id,
                status=result.status,
            )
            return result

    def dispatch_position_command(
        self, account_id: str, command_id: str, connector: Any,
    ) -> DispatchResult:
        with self._mutation():
            result = super().dispatch_position_command(account_id, command_id, connector)
            if result.status == "UNKNOWN":
                self._record_unknown(
                    account_id, command_id, subject_kind="POSITION_COMMAND",
                )
            else:
                self._complete_non_order_work_if_converged(
                    account_id, command_id, "POSITION_COMMAND",
                )
            self._audit(
                account_id, "execution.position_command.dispatch",
                command_id=command_id, status=result.status,
            )
            return result

    def recover(self, account_id: str, order_id: str, connector: Any) -> DispatchResult:
        with self._mutation():
            result = super().recover(account_id, order_id, connector)
            if result.status != "UNKNOWN":
                self._complete_work_if_converged(account_id, order_id)
                self._complete_restart_recovery(account_id)
            self._audit(
                account_id,
                "execution.reconciled",
                order_id=order_id,
                status=result.status,
            )
            return result

    def recover_position_command(
        self, account_id: str, command_id: str, connector: Any,
    ) -> DispatchResult:
        with self._mutation():
            result = super().recover_position_command(account_id, command_id, connector)
            if result.status != "UNKNOWN":
                self._complete_non_order_work_if_converged(
                    account_id, command_id, "POSITION_COMMAND",
                )
                self._complete_restart_recovery(account_id)
            self._audit(
                account_id, "execution.position_command.reconciled",
                command_id=command_id, status=result.status,
            )
            return result

    def recover_operator_command(
        self, account_id: str, command_id: str, connector: Any,
    ) -> DispatchResult:
        with self._mutation():
            result = super().recover_operator_command(account_id, command_id, connector)
            if result.status != "UNKNOWN":
                self._complete_non_order_work_if_converged(
                    account_id, command_id, "COMMAND",
                )
                self._complete_restart_recovery(account_id)
            self._audit(
                account_id, "execution.command.reconciled",
                command_id=command_id, status=result.status,
            )
            return result

    def reconcile_due(
        self, account_id: str, connector: Any, *, now: datetime | None = None,
    ) -> tuple[DispatchResult, ...]:
        """Run persisted UNKNOWN work without ever resending a broker command.

        A scheduler or reconnect handler may call this repeatedly.  It only
        reads journal/broker truth through ``recover``; dispatch is deliberately
        absent from this path.
        """
        at = now or _now()
        results: list[DispatchResult] = []
        with self._mutation(), self._lock_for(account_id):
            due = [
                item for item in self.reconciliation_work.values()
                if item.account_id == account_id and item.status == "PENDING"
            ]
            for work in due:
                if at >= work.deadline_at:
                    self.advance_recovery_deadlines(account_id=account_id, now=at)
                    results.append(DispatchResult(work.subject_id, "UNKNOWN", "ESCALATION_DEADLINE_EXCEEDED"))
                    continue
                work.attempts += 1
                work.last_attempt_at = at
                if work.subject_kind != "ORDER":
                    if work.subject_kind == "POSITION_COMMAND":
                        result = self.recover_position_command(
                            account_id, work.subject_id, connector,
                        )
                    else:
                        result = self.recover_operator_command(
                            account_id, work.subject_id, connector,
                        )
                    results.append(result)
                    continue
                result = self.recover(account_id, work.subject_id, connector)
                results.append(result)
            return tuple(results)

    def record_fill(self, *args: Any, **kwargs: Any) -> Fill:
        with self._mutation():
            return super().record_fill(*args, **kwargs)

    def observe_runtime_health(self, *args: Any, **kwargs: Any) -> RuntimeInterlockDecision:
        with self._mutation():
            decision = super().observe_runtime_health(*args, **kwargs)
            self._audit(
                decision.account_id,
                "execution.interlock.observed",
                status=decision.status,
                reasons=decision.reasons,
                evidence=decision.evidence,
            )
            return decision

    def observe_connector_health(self, *args: Any, **kwargs: Any) -> RuntimeInterlockDecision:
        with self._mutation():
            decision = super().observe_connector_health(*args, **kwargs)
            self._audit(
                decision.account_id,
                "execution.connector.health",
                status=decision.status,
                reasons=decision.reasons,
            )
            return decision

    def recover_runtime_interlock(self, *args: Any, **kwargs: Any) -> RuntimeInterlockDecision:
        with self._mutation():
            return super().recover_runtime_interlock(*args, **kwargs)

    def repair_native_protection(
        self, account_id: str, position_id: str, connector: Any, **kwargs: Any
    ) -> ProtectionRepairResult:
        with self._mutation():
            result = super().repair_native_protection(
                account_id,
                position_id,
                connector,
                max_attempts=kwargs.pop(
                    "max_attempts", self.max_protection_repair_attempts
                ),
                **kwargs,
            )
            self._audit(
                account_id,
                "execution.protection.repair",
                position_id=position_id,
                status=result.status,
                attempts=result.attempts,
                reason_code=result.reason_code,
                evidence=result.evidence,
            )
            return result

    def record_exit_fill(self, *args: Any, **kwargs: Any) -> PositionCommand:
        with self._mutation():
            return super().record_exit_fill(*args, **kwargs)

    def install_fence(self, *args: Any, **kwargs: Any) -> SafetyFence:
        with self._mutation():
            return super().install_fence(*args, **kwargs)

    def close_all(self, **kwargs: Any) -> OperatorCommand:
        with self._mutation():
            command = super().close_all(**kwargs)
            if command.status == "UNKNOWN":
                self._record_unknown(
                    command.account_id, command.id, subject_kind="COMMAND",
                )
            return command

    def cancel_order(self, **kwargs: Any) -> OperatorCommand:
        with self._mutation():
            command = super().cancel_order(**kwargs)
            if command.status == "UNKNOWN":
                self._record_unknown(
                    command.account_id, command.id, subject_kind="COMMAND",
                )
            return command

    def begin_global_emergency(self, *args: Any, **kwargs: Any) -> GlobalEmergencyOperation:
        with self._mutation():
            return super().begin_global_emergency(*args, **kwargs)

    def converge_global_target(self, *args: Any, **kwargs: Any) -> GlobalEmergencyOperation:
        with self._mutation():
            return super().converge_global_target(*args, **kwargs)

    def emergency_stop(self, *args: Any, **kwargs: Any) -> SafetyFence:
        with self._mutation():
            return super().emergency_stop(*args, **kwargs)

    def confirm_trailing(self, *args: Any, **kwargs: Any) -> PositionCommand:
        with self._mutation():
            return super().confirm_trailing(*args, **kwargs)

    def mark_protection_unknown(self, *args: Any, **kwargs: Any) -> Position:
        with self._mutation():
            return super().mark_protection_unknown(*args, **kwargs)

    def mark_position_command_unknown(self, *args: Any, **kwargs: Any) -> PositionCommand:
        with self._mutation():
            command = super().mark_position_command_unknown(*args, **kwargs)
            self._record_unknown(
                command.account_id, command.id,
                subject_kind="POSITION_COMMAND",
            )
            return command

    def confirm_protection(self, *args: Any, **kwargs: Any) -> Position:
        with self._mutation():
            account_id = args[0] if args else kwargs["account_id"]
            recovery_pending = self._has_unresolved_reconciliation(account_id)
            result = super().confirm_protection(*args, **kwargs)
            if recovery_pending:
                self.account(account_id).exposure_gate = "QUARANTINED"
            else:
                self._restore_temporary_entry_eligibility(account_id)
            return result

    def request_position_close(self, *args: Any, **kwargs: Any) -> PositionCommand:
        with self._mutation():
            return super().request_position_close(*args, **kwargs)

    def request_position_modify_protection(self, *args: Any, **kwargs: Any) -> PositionCommand:
        with self._mutation():
            return super().request_position_modify_protection(*args, **kwargs)

    request_position_protection = request_position_modify_protection

    def request_trailing(self, *args: Any, **kwargs: Any) -> PositionCommand | None:
        with self._mutation():
            return super().request_trailing(*args, **kwargs)

    def connector_disconnected(self, *args: Any, **kwargs: Any) -> Position:
        with self._mutation():
            return super().connector_disconnected(*args, **kwargs)

    def position(self, account_id: str, position_id: str) -> Position:
        position = self.positions.get((account_id, position_id))
        if position is not None:
            return position
        position = next(
            (
                item for item in self.positions.values()
                if item.account_id == account_id and item.order_id == position_id
            ),
            None,
        )
        if position is None:
            raise ExecutionError("POSITION_NOT_FOUND")
        return position

    def _find_position_key(
        self, account_id: str, position_id: str, pair: str, mode: str
    ) -> tuple[str, str]:
        if mode == "NETTING":
            for key, position in self.positions.items():
                if (
                    key[0] == account_id and position.accounting_mode == "NETTING"
                    and position.pair == pair
                ):
                    return key
        return account_id, position_id

    def _project_position(self, account_id: str, item: dict[str, Any]) -> Position:
        position_id = str(item.get("position_id", item.get("external_position_id", "")))
        if not position_id:
            raise ExecutionError("POSITION_ID_REQUIRED")
        order_id = str(item.get("order_id", position_id))
        order = self.orders.get(order_id)
        if order is None or order.account_id != account_id:
            raise ExecutionError("WRONG_ACCOUNT")
        mode = str(item.get("accounting_mode", "NETTING"))
        pair = str(item.get("symbol", item.get("pair", "UNKNOWN")))
        key = self._find_position_key(account_id, position_id, pair, mode)
        position = self.positions.get(key)
        volume = str(item.get("volume", item.get("remaining_volume", "0")))
        observed_stop = item.get("sl", item.get("stop_loss"))
        protection = "CONFIRMED" if observed_stop is not None else "UNCONFIRMED"
        if position is None:
            side = str(item.get("side", item.get("direction", "BUY"))).upper()
            position = Position(
                account_id=account_id, order_id=order_id, volume=volume,
                remaining_volume=volume, protection_status=protection,
                native_stop_loss=str(observed_stop) if observed_stop is not None else None,
                last_confirmed_stop=str(observed_stop) if observed_stop is not None else None,
                native_take_profit=str(item.get("tp", item.get("take_profit"))) if item.get("tp", item.get("take_profit")) is not None else None,
                accounting_mode=mode if mode in {"NETTING", "HEDGING"} else "NETTING",
                external_position_id=position_id, pair=pair,
                direction="LONG" if side in {"BUY", "LONG"} else "SHORT",
                entry_price=str(item["entry_price"]) if item.get("entry_price") is not None else "UNKNOWN",
                current_pnl=str(item["current_pnl"]) if item.get("current_pnl") is not None else None,
                data_status="CONFIRMED",
            )
            self.positions[key] = position
        else:
            expected_stop = position.last_confirmed_stop or position.native_stop_loss
            if observed_stop is None or (
                expected_stop is not None and str(observed_stop) != str(expected_stop)
            ):
                protection = "UNCONFIRMED"
            position.volume = volume
            position.remaining_volume = str(item.get("remaining_volume", volume))
            position.protection_status = protection
            position.data_status = "CONFIRMED"
            if protection == "CONFIRMED":
                position.native_stop_loss = str(observed_stop)
                position.last_confirmed_stop = str(observed_stop)
            if item.get("current_pnl") is not None:
                position.current_pnl = str(item["current_pnl"])
        if protection != "CONFIRMED":
            self.account(account_id).exposure_gate = "QUARANTINED"
            self._set_runtime_interlock(
                account_id,
                reasons=("NATIVE_PROTECTION_CHANGED",),
                evidence={"position_id": position_id, "observed_stop_loss": observed_stop},
            )
        return position

    @staticmethod
    def _apply_order_observation(order: OrderIntent, observed_status: str) -> bool:
        """Apply broker status without allowing an older snapshot to regress it."""
        if order.status == "FILLED" and observed_status != "FILLED":
            return False
        if order.status in {"REJECTED", "CANCELLED"} and observed_status not in {
            order.status, "FILLED"
        }:
            return False
        if order.status == "PARTIALLY_FILLED" and observed_status in {
            "SUBMITTED", "CHECKED", "DISPATCHING", "INTENT"
        }:
            return False
        if observed_status == "UNKNOWN" and order.status not in {
            "UNKNOWN", "INTENT", "CHECKED", "DISPATCHING"
        }:
            return False
        order.status = observed_status
        return True

    def reconcile_observation(
        self, account_id: str, observation: dict[str, Any]
    ) -> ReconciliationResult:
        """Apply an account-bound broker snapshot exactly once per deal."""
        observed_account = observation.get("account_id", observation.get("broker_account_id"))
        if observed_account is not None and str(observed_account) != account_id:
            raise ExecutionError("WRONG_ACCOUNT")
        if observation.get("complete"):
            if not COMPLETE_RECONCILIATION_SECTIONS.issubset(observation):
                raise ExecutionError("INCOMPLETE_RECONCILIATION")
        applied: list[str] = []
        duplicates: list[str] = []
        position_ids: list[str] = []
        unknown_order_ids: list[str] = []
        with self._mutation(), self._lock_for(account_id):
            for item in observation.get("orders", ()):
                order_id = str(item.get("order_id", ""))
                order = self.orders.get(order_id)
                if order is None or order.account_id != account_id:
                    raise ExecutionError("WRONG_ACCOUNT")
                observed_status = str(item.get("status", "")).upper()
                if observed_status in {"SUBMITTED", "PARTIALLY_FILLED", "FILLED", "REJECTED", "CANCELLED", "UNKNOWN"}:
                    applied_status = self._apply_order_observation(order, observed_status)
                    if applied_status and observed_status == "UNKNOWN":
                        unknown_order_ids.append(order_id)
                    if applied_status and observed_status == "FILLED":
                        self._reservation_for(order_id).status = "CONSUMED"
                    elif applied_status and observed_status in {"REJECTED", "CANCELLED"}:
                        self._reservation_for(order_id).status = "RELEASED"
                if item.get("external_id") is not None:
                    order.external_id = str(item["external_id"])
            for item in observation.get("commands", ()):
                command_id = str(item.get("command_id", ""))
                command = self.commands.get(command_id)
                if command is None or command.account_id != account_id:
                    raise ExecutionError("WRONG_ACCOUNT")
                observed_status = str(item.get("status", "")).upper()
                if command.status in {"EXECUTED", "REJECTED"} and observed_status not in {
                    "CONFIRMED", "EXECUTED"
                }:
                    continue
                if observed_status in {"CONFIRMED", "EXECUTED"}:
                    command.status = "EXECUTED"
                    command.rejection_code = None
                    self._apply_operator_command(command)
                    self._complete_non_order_work_if_converged(
                        account_id, command.id, "COMMAND"
                    )
                elif observed_status == "UNKNOWN":
                    command.status = "UNKNOWN"
                    self._record_unknown(account_id, command.id, subject_kind="COMMAND")
                elif observed_status in {"REJECTED", "CANCELLED", "NOT_FOUND"}:
                    command.status = "REJECTED"
                    self._complete_non_order_work_if_converged(
                        account_id, command.id, "COMMAND"
                    )
            for item in observation.get("position_commands", ()):
                command_id = str(item.get("command_id", ""))
                command = next(
                    (value for value in self.position_commands if value.id == command_id),
                    None,
                )
                if command is None or command.account_id != account_id:
                    raise ExecutionError("WRONG_ACCOUNT")
                observed_status = str(item.get("status", "")).upper()
                if command.status in {"CONFIRMED", "REJECTED"} and observed_status not in {
                    "CONFIRMED", "EXECUTED"
                }:
                    continue
                if observed_status in {"CONFIRMED", "EXECUTED"}:
                    command.status = "CONFIRMED"
                    self._apply_confirmed_trailing_stop(
                        account_id, command, item, "confirmed_stop", "stop"
                    )
                    self._complete_non_order_work_if_converged(
                        account_id, command.id, "POSITION_COMMAND"
                    )
                elif observed_status == "UNKNOWN":
                    command.status = "UNKNOWN"
                    self._record_unknown(
                        account_id, command.id, subject_kind="POSITION_COMMAND"
                    )
                elif observed_status in {"REJECTED", "CANCELLED", "NOT_FOUND"}:
                    command.status = "REJECTED"
                    self._complete_non_order_work_if_converged(
                        account_id, command.id, "POSITION_COMMAND"
                    )
            for item in observation.get("fills", ()):
                deal_id = str(item.get("deal_id", item.get("external_deal_id", "")))
                order_id = str(item.get("order_id", ""))
                order = self.orders.get(order_id)
                if not deal_id or order is None or order.account_id != account_id:
                    raise ExecutionError("WRONG_ACCOUNT")
                if self._fill_for_deal(account_id, deal_id) is not None:
                    duplicates.append(deal_id)
                    continue
                entry = str(item.get("entry", "IN")).upper()
                fill = Fill(
                    str(uuid4()), account_id, order_id, deal_id,
                    str(item.get("volume", "0")),
                    bool(item.get("native_protection_confirmed", False)),
                )
                self.fills[fill.id] = fill
                applied.append(deal_id)
                if entry in {"IN", "INOUT"}:
                    requested = Decimal(str(order.requested_volume or order.payload.get("volume", item.get("volume", "0"))))
                    cumulative = sum(
                        (self._decimal(other.volume) for other in self.fills.values()
                         if other.account_id == account_id and other.order_id == order_id),
                        Decimal("0"),
                    )
                    order.cumulative_filled_volume = self._decimal_string(cumulative)
                    order.remaining_volume = self._decimal_string(max(requested - cumulative, Decimal("0")))
                    order.status = "FILLED" if requested and cumulative >= requested else "PARTIALLY_FILLED"
                    reservation = self._reservation_for(order_id)
                    reservation.status = "CONSUMED" if order.status == "FILLED" else "ACTIVE"
                elif entry in {"OUT", "OUT_BY"}:
                    order.status = "FILLED"
            for item in observation.get("positions", ()):
                position = self._project_position(account_id, item)
                position_ids.append(position.external_position_id or position.order_id)
            for order_id in unknown_order_ids:
                self._record_unknown(account_id, order_id)
            order_ids = {str(item.get("order_id")) for item in observation.get("fills", ())}
            statuses = [self.orders[item].status for item in order_ids if item in self.orders]
            status = statuses[0] if statuses else "RECONCILED"
            self._audit(
                account_id, "execution.observation.reconciled",
                applied_fill_ids=tuple(applied), duplicate_fill_ids=tuple(duplicates),
            )
            reconciled_order_ids = {
                str(item.get("order_id", "")) for item in observation.get("orders", ())
            }
            reconciled_order_ids.update(
                str(item.get("order_id", "")) for item in observation.get("fills", ())
            )
            for order_id in reconciled_order_ids:
                self._complete_work_if_converged(account_id, order_id)
            self._restore_temporary_entry_eligibility(account_id)
            if observation.get("complete"):
                self._complete_restart_recovery(
                    account_id,
                    observed_at=_state_datetime(observation["observed_at"])
                    if observation.get("observed_at")
                    else None,
                )
            return ReconciliationResult(
                account_id,
                status,
                tuple(applied),
                tuple(duplicates),
                tuple(position_ids),
            )

    def observe_broker(self, account_id: str, observation: dict[str, Any]) -> ReconciliationResult:
        return self.reconcile_observation(account_id, observation)
