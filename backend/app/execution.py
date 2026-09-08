"""Account-local execution safety substrate.

The broker connector is deliberately an injected dependency.  This module owns
the durable-domain decisions around it: reservations and intents are created
before dispatch, broker ambiguity is journaled as UNKNOWN, and recovery asks
the journal/broker for truth before any further side effect.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
        "DISPATCHING",
        "SUBMITTED",
        "REJECTED",
        "UNKNOWN",
        "FILLED",
        "CANCELLED",
    ] = "INTENT"
    external_id: str | None = None


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


OperatorCommandKind = Literal["APPROVE_SIGNAL", "EXECUTE_SIGNAL", "CLOSE_ALL"]


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
        self._idempotency: dict[tuple[str, str], tuple[str, str]] = {}
        self._order_reservations: dict[str, str] = {}
        self.commands: dict[str, OperatorCommand] = {}
        self._command_keys: dict[tuple[str, str], str] = {}
        self._approved_signals: dict[tuple[str, str], str] = {}

    def _lock_for(self, account_id: str) -> threading.RLock:
        return self._locks.setdefault(account_id, threading.RLock())

    def account(self, account_id: str) -> AccountExecutionState:
        return self._accounts.setdefault(account_id, AccountExecutionState(account_id))

    def pre_order(
        self, *, account_id: str, signal_id: str, idempotency_key: str,
        canonical_hash: str, risk_approved: bool, execution_epoch: int,
        order_payload: dict[str, Any], risk_amount: str = "0",
    ) -> PreOrderResult:
        with self._lock_for(account_id):
            account = self.account(account_id)
            prior = self._idempotency.get((account_id, idempotency_key))
            if prior:
                prior_order_id, prior_hash = prior
                if prior_hash != canonical_hash:
                    raise ExecutionError("IDEMPOTENCY_KEY_REUSED")
                order = self.orders[prior_order_id]
                return PreOrderResult(
                    self._reservation_for(order.id), order, self._event_for(order.id)
                )
            if not risk_approved:
                raise ExecutionError("PRE_ORDER_RISK_REJECTED")
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
        risk_approved: bool, signal_fresh: bool, fence_safe: bool,
        account_state: str, live_lock: bool, execution_epoch: int,
        order_payload: dict[str, Any], risk_amount: str = "0",
    ) -> PreOrderResult:
        """Execute an already approved Signal after every last-mile gate."""
        with self._lock_for(account_id):
            approval_id = self._approved_signals.get((account_id, signal_id))
            if approval_id is None:
                raise ExecutionError("SIGNAL_APPROVAL_REQUIRED")
            if not all((risk_approved, signal_fresh, fence_safe, live_lock)):
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
            result = self.pre_order(
                account_id=account_id,
                signal_id=signal_id,
                idempotency_key=idempotency_key,
                canonical_hash=json.dumps(order_payload, sort_keys=True),
                risk_approved=True,
                execution_epoch=execution_epoch,
                order_payload=order_payload,
                risk_amount=risk_amount,
            )
            command.status, command.order_id = "EXECUTED", result.order.id
            return result

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
    ) -> Fill:
        with self._lock_for(account_id):
            order = self.orders.get(order_id)
            if order is None or order.account_id != account_id:
                raise ExecutionError("WRONG_ACCOUNT")
            existing_fill = next(
                (
                    fill
                    for fill in self.fills.values()
                    if fill.external_deal_id == external_deal_id
                ),
                None,
            )
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
            protection = "CONFIRMED" if native_protection_confirmed else "UNCONFIRMED"
            self.positions[(account_id, order_id)] = Position(account_id, order_id, str(volume), protection)
            if not native_protection_confirmed:
                self.account(account_id).exposure_gate = "QUARANTINED"
                self.install_fence(account_id, "SAFETY_FENCE")
                self.positions[(account_id, order_id)].protection_status = "UNCONFIRMED"
            return fill

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

    def install_fence(self, account_id: str, kind: str = "SAFETY_FENCE") -> SafetyFence:
        account = self.account(account_id)
        account.fence_sequence += 1
        if account.exposure_gate != "QUARANTINED":
            account.exposure_gate = "FENCE_PENDING"
        account.execution_epoch += 1
        return SafetyFence(account_id, account.fence_sequence, kind)
