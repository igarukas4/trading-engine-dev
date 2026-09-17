"""Account-local execution safety substrate.

The broker connector is deliberately an injected dependency.  This module owns
the durable-domain decisions around it: reservations and intents are created
before dispatch, broker ambiguity is journaled as UNKNOWN, and recovery asks
the journal/broker for truth before any further side effect.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Literal, Protocol
from uuid import uuid4

from .risk_calendar import RiskAssessment


def _now() -> datetime:
    return datetime.now(timezone.utc)


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

    def __post_init__(self) -> None:
        if self.remaining_volume is None:
            self.remaining_volume = self.volume


@dataclass
class PositionCommand:
    id: str
    account_id: str
    order_id: str
    command_type: Literal["TP1", "TP2", "TRAIL", "CLOSE"]
    requested_volume: str | None
    reduce_only: bool = True
    status: Literal["RECEIVED", "CONFIRMED", "UNKNOWN", "REJECTED"] = "RECEIVED"
    requested_stop: str | None = None
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


OperatorCommandKind = Literal["APPROVE_SIGNAL", "EXECUTE_SIGNAL", "CLOSE_ALL"]


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
    status: Literal["ACCEPTED", "REJECTED", "EXECUTED"]
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

    def _lock_for(self, account_id: str) -> threading.RLock:
        return self._locks.setdefault(account_id, threading.RLock())

    @contextmanager
    def account_lock(self, account_id: str) -> Iterator[None]:
        """Serialize a handler's fresh risk snapshot with order acceptance."""
        with self._lock_for(account_id):
            yield

    def account(self, account_id: str) -> AccountExecutionState:
        return self._accounts.setdefault(account_id, AccountExecutionState(account_id))

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
            if not all((signal_fresh, fence_safe, live_lock)):
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
                try:
                    response = connector.close_all(account_id)
                except Exception:
                    response = None
                response_is_ambiguous = response is None or (
                    isinstance(response, dict) and response.get("status") == "UNKNOWN"
                )
                if response_is_ambiguous:
                    command.rejection_code = "RECONCILIATION_PENDING"
                else:
                    command.status = "EXECUTED"
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

    def dispatch_next(self, account_id: str, connector: Any) -> DispatchResult:
        with self._lock_for(account_id):
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
            if response == "TIMEOUT" or response is None:
                order.status = "UNKNOWN"
                return DispatchResult(order.id, "UNKNOWN", "CONNECTOR_RESULT_AMBIGUOUS")
            accepted = response if isinstance(response, dict) else {"status": str(response)}
            if accepted.get("status") not in {"ACCEPTED", "SUBMITTED", "FILLED"}:
                return self._reject_dispatch(
                    event, order, reservation, journal, "CONNECTOR_REJECTED"
                )
            if accepted.get("status") == "FILLED":
                order.status = "FILLED"
            else:
                order.status = "SUBMITTED"
            order.external_id = accepted.get("external_id")
            journal.state = "ACCEPTED"
            journal.external_id = order.external_id
            event.status = "PUBLISHED"
            return DispatchResult(order.id, order.status)

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
            observed = None
            try:
                observed = connector.journal(order)
            except Exception:
                observed = None
            if not observed:
                observed = connector.broker_state(order)
            if not observed:
                return DispatchResult(order.id, "UNKNOWN", "RECONCILIATION_PENDING")
            if isinstance(observed, dict):
                status = observed.get("status")
                external_id = observed.get("external_id")
            else:
                status = str(observed)
                external_id = None
            if status == "FILLED":
                order.status = "FILLED"
                order.external_id = external_id
                journal.state = "ACCEPTED"
                self._reservation_for(order.id).status = "CONSUMED"
            elif status in {"REJECTED", "NOT_FOUND"}:
                order.status = "REJECTED"
                journal.state = "REJECTED"
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
            if position is None:
                protection = "CONFIRMED" if native_protection_confirmed else "UNCONFIRMED"
                side = str(payload.get("side", "BUY")).upper()
                position = Position(
                    account_id=account_id,
                    order_id=order_id,
                    volume=str(volume),
                    protection_status=protection,
                    native_stop_loss=str(payload.get("stop_loss")) if payload.get("stop_loss") is not None else None,
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
                if not native_protection_confirmed:
                    position.protection_status = "UNCONFIRMED"
            if not native_protection_confirmed:
                self.account(account_id).exposure_gate = "QUARANTINED"
                self.install_fence(account_id, "SAFETY_FENCE")
                position.protection_status = "UNCONFIRMED"
            return fill

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
            else:
                self.install_fence(account_id, "CONNECTOR_DISCONNECTED")
            return position

    def confirm_protection(self, account_id: str, order_id: str) -> Position:
        """Confirm broker-native SL/TP before releasing a protection quarantine."""
        with self._lock_for(account_id):
            position = self.position(account_id, order_id)
            position.protection_status = "CONFIRMED"
            if self.account(account_id).exposure_gate == "QUARANTINED":
                self.account(account_id).exposure_gate = "OPEN"
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
        return value
    if isinstance(value, dict) and "__datetime__" in value:
        return datetime.fromisoformat(value["__datetime__"])
    return datetime.fromisoformat(value)


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
    ) -> None:
        super().__init__()
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
        self.audit_events: list[AuditEvent] = []
        self.risk_assessments: dict[str, dict[str, Any]] = {}
        if self._state_store is not None:
            self._restore(self._state_store.load())

    def _snapshot(self) -> dict[str, Any]:
        return _serialize_state_value(
            {
                "accounts": {
                    key: value.__dict__ for key, value in self._accounts.items()
                },
                "reservations": {
                    key: value.__dict__ for key, value in self.reservations.items()
                },
                "orders": {key: value.__dict__ for key, value in self.orders.items()},
                "events": {key: value.__dict__ for key, value in self.events.items()},
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
        for key, value in state.get("accounts", {}).items():
            self._accounts[key] = AccountExecutionState(**value)
        for key, value in state.get("reservations", {}).items():
            self.reservations[key] = RiskReservation(**value)
        for key, value in state.get("orders", {}).items():
            self.orders[key] = OrderIntent(**value)
        for key, value in state.get("events", {}).items():
            self.events[key] = OutboxEvent(**value)
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

    def _audit(self, account_id: str, event_type: str, **payload: Any) -> None:
        self.audit_events.append(
            AuditEvent(str(uuid4()), account_id, event_type, payload, _now())
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

    def dispatch_next(self, account_id: str, connector: BrokerAdapter) -> DispatchResult:
        with self._mutation():
            result = super().dispatch_next(account_id, connector)
            if result.status == "UNKNOWN":
                self.account(account_id).exposure_gate = "QUARANTINED"
                self.install_fence(account_id, "UNKNOWN_RECONCILIATION")
            self._audit(
                account_id,
                "execution.dispatch",
                order_id=result.order_id,
                status=result.status,
            )
            return result

    def recover(self, account_id: str, order_id: str, connector: Any) -> DispatchResult:
        with self._mutation():
            result = super().recover(account_id, order_id, connector)
            self._audit(
                account_id,
                "execution.reconciled",
                order_id=order_id,
                status=result.status,
            )
            return result

    def record_fill(self, *args: Any, **kwargs: Any) -> Fill:
        with self._mutation():
            return super().record_fill(*args, **kwargs)

    def record_exit_fill(self, *args: Any, **kwargs: Any) -> PositionCommand:
        with self._mutation():
            return super().record_exit_fill(*args, **kwargs)

    def install_fence(self, *args: Any, **kwargs: Any) -> SafetyFence:
        with self._mutation():
            return super().install_fence(*args, **kwargs)

    def close_all(self, **kwargs: Any) -> OperatorCommand:
        with self._mutation():
            return super().close_all(**kwargs)

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
            return super().mark_position_command_unknown(*args, **kwargs)

    def confirm_protection(self, *args: Any, **kwargs: Any) -> Position:
        with self._mutation():
            return super().confirm_protection(*args, **kwargs)

    def request_position_close(self, *args: Any, **kwargs: Any) -> PositionCommand:
        with self._mutation():
            return super().request_position_close(*args, **kwargs)

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
        protection = "CONFIRMED" if item.get("sl") is not None or item.get("stop_loss") is not None else "UNCONFIRMED"
        if position is None:
            side = str(item.get("side", item.get("direction", "BUY"))).upper()
            position = Position(
                account_id=account_id, order_id=order_id, volume=volume,
                remaining_volume=volume, protection_status=protection,
                native_stop_loss=str(item.get("sl", item.get("stop_loss"))) if item.get("sl", item.get("stop_loss")) is not None else None,
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
            position.volume = volume
            position.remaining_volume = str(item.get("remaining_volume", volume))
            position.protection_status = protection
            position.data_status = "CONFIRMED"
            if item.get("current_pnl") is not None:
                position.current_pnl = str(item["current_pnl"])
        if protection != "CONFIRMED":
            self.account(account_id).exposure_gate = "QUARANTINED"
        return position

    def reconcile_observation(
        self, account_id: str, observation: dict[str, Any]
    ) -> ReconciliationResult:
        """Apply an account-bound broker snapshot exactly once per deal."""
        observed_account = observation.get("account_id", observation.get("broker_account_id"))
        if observed_account is not None and str(observed_account) != account_id:
            raise ExecutionError("WRONG_ACCOUNT")
        applied: list[str] = []
        duplicates: list[str] = []
        position_ids: list[str] = []
        with self._mutation(), self._lock_for(account_id):
            for item in observation.get("orders", ()):
                order_id = str(item.get("order_id", ""))
                order = self.orders.get(order_id)
                if order is None or order.account_id != account_id:
                    raise ExecutionError("WRONG_ACCOUNT")
                if item.get("status") in {"SUBMITTED", "PARTIALLY_FILLED", "FILLED", "REJECTED", "CANCELLED", "UNKNOWN"}:
                    order.status = item["status"]
                if item.get("external_id") is not None:
                    order.external_id = str(item["external_id"])
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
            order_ids = {str(item.get("order_id")) for item in observation.get("fills", ())}
            statuses = [self.orders[item].status for item in order_ids if item in self.orders]
            status = statuses[0] if statuses else "RECONCILED"
            self._audit(
                account_id, "execution.observation.reconciled",
                applied_fill_ids=tuple(applied), duplicate_fill_ids=tuple(duplicates),
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
