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
from decimal import Decimal
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
    remaining_volume: str | None = None
    stage: Literal["ENTRY", "TP1_CONFIRMED", "TP2_CONFIRMED", "CLOSING", "CLOSED"] = "ENTRY"
    runner_volume: str | None = None
    native_stop_loss: str | None = None
    native_take_profit: str | None = None
    last_confirmed_stop: str | None = None
    accounting_mode: Literal["NETTING", "HEDGING"] = "NETTING"
    external_position_id: str | None = None

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
        self.position_commands: list[PositionCommand] = []
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
                position = Position(
                    account_id, order_id, str(volume), protection,
                    native_stop_loss=str(payload.get("stop_loss")) if payload.get("stop_loss") is not None else None,
                    native_take_profit=str(payload.get("take_profit")) if payload.get("take_profit") is not None else None,
                    last_confirmed_stop=str(payload.get("stop_loss")) if payload.get("stop_loss") is not None else None,
                    accounting_mode=accounting_mode,
                    external_position_id=external_position_id,
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

    def install_fence(self, account_id: str, kind: str = "SAFETY_FENCE") -> SafetyFence:
        account = self.account(account_id)
        account.fence_sequence += 1
        if account.exposure_gate != "QUARANTINED":
            account.exposure_gate = "FENCE_PENDING"
        account.execution_epoch += 1
        return SafetyFence(account_id, account.fence_sequence, kind)
