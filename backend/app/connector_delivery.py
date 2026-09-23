"""In-memory account-scoped connector command delivery for the MVP gate.

This registry is transport state only. It is intentionally not crash durable and
must not be confused with the connector SQLite journal or a backend outbox.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from .execution import ConnectorDispatchRecord


COMMAND_TYPES = frozenset({
    "account_snapshot.request",
    "market_snapshot.request",
    "candle_batch.request",
    "order.submit_market",
    "position.modify_protection",
    "position.close",
    "reconcile.request",
})
CONTROL_UPDATE_TYPES = frozenset({"execution_gate.update"})
CONTROL_ACK_TYPES = frozenset({"execution_gate.ack"})
SIDE_EFFECTING_COMMANDS = frozenset({
    "order.submit_market", "position.modify_protection", "position.close",
})
IDENTITY_FIELDS = frozenset({"provider", "broker_server", "external_account_id"})
RESULT_STATES = frozenset({"ACCEPTED", "REJECTED", "UNKNOWN"})
DEFERRED_DELIVERY_ERRORS = frozenset({
    "SESSION_NOT_ACTIVE", "RECONCILIATION_REQUIRED", "DUPLICATE_PENDING_COMMAND",
})


class DeliveryError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class OutboundEnvelope:
    account_id: str
    identity: dict[str, str]
    connector_generation: int
    dispatch_sequence: int
    sequence: int
    execution_epoch: int
    command_id: str
    idempotency_key: str
    request_hash: str
    type: str
    payload: dict[str, Any]
    sent_at: str

    def as_message(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "type": self.type,
            "message_id": f"dispatch:{self.command_id}",
            "account_id": self.account_id,
            "provider": self.identity["provider"],
            "broker_server": self.identity["broker_server"],
            "external_account_id": self.identity["external_account_id"],
            "generation": self.connector_generation,
            "sequence": self.sequence,
            "execution_epoch": self.execution_epoch,
            "command_id": self.command_id,
            "idempotency_key": self.idempotency_key,
            "request_hash": self.request_hash,
            "sent_at": self.sent_at,
            "payload": self.payload,
        }


@dataclass
class _Pending:
    envelope: OutboundEnvelope
    state: Literal["QUEUED", "SENT", "ACCEPTED", "REJECTED", "UNKNOWN", "FENCED"] = "QUEUED"
    control_ack: asyncio.Event | None = None


@dataclass
class _Session:
    account_id: str
    generation: int
    session_id: str
    queue: asyncio.Queue[OutboundEnvelope]
    identity: dict[str, str] | None = None
    execution_epoch: int | None = None
    last_received_sequence: int = 0
    seen_message_ids: set[str] = field(default_factory=set)
    reconciliation_required: bool = False
    execution_allowed: bool = False
    loop: asyncio.AbstractEventLoop | None = None


def validate_hello(message: dict[str, Any]) -> dict[str, Any]:
    """Validate the exact nine-field opening frame without echoing its secret."""
    fields = {
        "type", "account_id", "provider", "broker_server", "external_account_id",
        "key_id", "secret", "generation", "session_id",
    }
    if not isinstance(message, dict) or set(message) != fields or message.get("type") != "hello":
        raise DeliveryError("MALFORMED_FRAME")
    for field in fields - {"generation", "type"}:
        if not isinstance(message.get(field), str) or not message[field]:
            raise DeliveryError("MALFORMED_FRAME")
    generation = message["generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise DeliveryError("MALFORMED_FRAME")
    return dict(message)


class ConnectorDeliveryRegistry:
    """One active transport session and ordered delivery stream per account."""

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._pending: dict[str, dict[str, _Pending]] = {}
        self._last_sequence: dict[str, int] = {}
        self._last_dispatch_sequence: dict[str, int] = {}
        self._received_generation: dict[str, int] = {}
        self._received_sequence: dict[str, int] = {}
        self._received_message_ids: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    async def open_session(
        self,
        account_id: str,
        generation: int,
        session_id: str,
        *,
        identity: dict[str, str] | None = None,
        execution_epoch: int | None = None,
        reconciliation_required: bool = False,
        execution_gate_open: bool = False,
    ) -> None:
        if not account_id or not session_id or isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise DeliveryError("INVALID_SESSION")
        if identity is not None and set(identity) != IDENTITY_FIELDS:
            raise DeliveryError("INVALID_ACCOUNT_IDENTITY")
        if not isinstance(execution_gate_open, bool):
            raise DeliveryError("INVALID_EXECUTION_GATE")
        if execution_epoch is not None and (isinstance(execution_epoch, bool) or execution_epoch < 0):
            raise DeliveryError("INVALID_COMMAND_CONTEXT")
        async with self._lock:
            if account_id in self._sessions:
                raise DeliveryError("SESSION_ALREADY_ACTIVE")
            if self._received_generation.get(account_id) != generation:
                self._received_generation[account_id] = generation
                self._received_sequence[account_id] = 0
                self._received_message_ids[account_id] = set()
            self._sessions[account_id] = _Session(
                account_id, generation, session_id, asyncio.Queue(),
                dict(identity) if identity else None, execution_epoch,
                self._received_sequence.get(account_id, 0),
                set(self._received_message_ids.get(account_id, set())),
                reconciliation_required,
                bool(execution_gate_open and not reconciliation_required),
                asyncio.get_running_loop(),
            )
            self._pending.setdefault(account_id, {})

    async def close_session(self, account_id: str, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(account_id)
            if session and session.session_id == session_id:
                del self._sessions[account_id]
                for pending in self._pending.get(account_id, {}).values():
                    if pending.control_ack is not None:
                        pending.control_ack.set()

    def active_session(
        self, account_id: str, *, generation: int, session_id: str | None = None,
    ) -> bool:
        """Return the transport authority without trusting a durable health flag."""
        session = self._sessions.get(account_id)
        return bool(
            session is not None
            and session.generation == generation
            and (session_id is None or session.session_id == session_id)
        )

    def execution_gate(self, account_id: str) -> bool:
        session = self._sessions.get(account_id)
        return bool(session and session.execution_allowed)

    def session_epoch(self, account_id: str) -> int | None:
        session = self._sessions.get(account_id)
        return session.execution_epoch if session else None

    async def publish_execution_gate(
        self, account_id: str, *, generation: int, execution_epoch: int,
        allowed: bool,
    ) -> OutboundEnvelope:
        """Queue an account-local gate update and close the gate until it is acked."""
        if not isinstance(allowed, bool):
            raise DeliveryError("INVALID_EXECUTION_GATE")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise DeliveryError("STALE_GENERATION")
        if isinstance(execution_epoch, bool) or not isinstance(execution_epoch, int) or execution_epoch < 0:
            raise DeliveryError("INVALID_COMMAND_CONTEXT")
        async with self._lock:
            session = self._sessions.get(account_id)
            if not session or session.generation != generation:
                raise DeliveryError("SESSION_NOT_ACTIVE")
            session.execution_epoch = execution_epoch
            session.execution_allowed = False
            command_id = "gate:" + str(uuid4())
            sequence = self._last_sequence.get(account_id, 0) + 1
            envelope = OutboundEnvelope(
                account_id,
                dict(session.identity or {}),
                generation,
                0,
                sequence,
                execution_epoch,
                command_id,
                "gate:" + command_id,
                "gate:" + command_id,
                "execution_gate.update",
                {"backend_execution_gate": bool(allowed), "execution_epoch": execution_epoch},
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            self._last_sequence[account_id] = sequence
            self._pending.setdefault(account_id, {})[command_id] = _Pending(
                envelope, control_ack=asyncio.Event(),
            )
            await session.queue.put(envelope)
            return envelope

    async def wait_for_control_ack(
        self, account_id: str, control_id: str, *, timeout: float = 5.0,
    ) -> None:
        """Wait until an active connector has consumed a gate update.

        Restrictive lifecycle commands may not report success while the old
        connector-side gate is still in force.  Session loss is safe because
        the transport authority is gone; every other failure is surfaced to
        the caller instead of silently proceeding.
        """
        async with self._lock:
            pending = self._pending.get(account_id, {}).get(control_id)
            session = self._sessions.get(account_id)
            if pending is None or pending.envelope.type not in CONTROL_UPDATE_TYPES:
                raise DeliveryError("UNKNOWN_CONTROL")
            if pending.state == "ACCEPTED":
                return
            if session is None or pending.control_ack is None:
                raise DeliveryError("SESSION_NOT_ACTIVE")
            acknowledged = pending.control_ack
        try:
            await asyncio.wait_for(acknowledged.wait(), timeout=timeout)
        except asyncio.TimeoutError as error:
            raise DeliveryError("CONTROL_ACK_TIMEOUT") from error
        async with self._lock:
            pending = self._pending.get(account_id, {}).get(control_id)
            session = self._sessions.get(account_id)
            if pending is not None and pending.state == "ACCEPTED":
                return
            if session is None:
                raise DeliveryError("SESSION_NOT_ACTIVE")
            raise DeliveryError("CONTROL_ACK_REJECTED")

    async def next_for_session(self, account_id: str, session_id: str) -> OutboundEnvelope:
        session = self._sessions.get(account_id)
        if not session or session.session_id != session_id:
            raise DeliveryError("SESSION_NOT_ACTIVE")
        while True:
            envelope = await session.queue.get()
            pending = self._pending.get(account_id, {}).get(envelope.command_id)
            if pending and pending.state == "QUEUED":
                return envelope
            session.queue.task_done()

    async def fence_account(self, account_id: str, execution_epoch: int) -> None:
        """Invalidate queued exposure-increasing envelopes before a stop succeeds."""
        async with self._lock:
            for pending in self._pending.get(account_id, {}).values():
                if (
                    pending.state in {"QUEUED", "SENT"}
                    and pending.envelope.execution_epoch < execution_epoch
                    and pending.envelope.type == "order.submit_market"
                ):
                    pending.state = "FENCED"

    async def advance_epoch(self, account_id: str, execution_epoch: int) -> None:
        """Move the active session to the committed epoch without fencing exits."""
        async with self._lock:
            session = self._sessions.get(account_id)
            if session is not None:
                session.execution_epoch = max(
                    session.execution_epoch or 0, execution_epoch
                )

    async def mark_reconciled(self, account_id: str, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(account_id)
            if not session or session.session_id != session_id:
                raise DeliveryError("SESSION_NOT_ACTIVE")
            session.reconciliation_required = False

    async def accept_inbound(self, account_id: str, session_id: str, message: dict[str, Any]) -> dict[str, Any]:
        """Validate a connector frame against the already-authenticated session."""
        async with self._lock:
            session = self._sessions.get(account_id)
            if not session or session.session_id != session_id:
                raise DeliveryError("SESSION_NOT_ACTIVE")
            if not isinstance(message, dict):
                raise DeliveryError("MALFORMED_FRAME")
            if "dispatch_sequence" in message or "connector_generation" in message:
                raise DeliveryError("MALFORMED_FRAME")
            if message.get("account_id") != account_id:
                raise DeliveryError("WRONG_ACCOUNT")
            schema_version = message.get("schema_version")
            generation = message.get("generation")
            if "schema_version" in message and (
                isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1
            ):
                raise DeliveryError("MALFORMED_FRAME")
            if (
                isinstance(generation, bool) or not isinstance(generation, int) or generation < 0
            ):
                raise DeliveryError("MALFORMED_FRAME")
            if generation != session.generation:
                raise DeliveryError("STALE_GENERATION")
            if schema_version == 1:
                required = {
                    "schema_version", "type", "message_id", "account_id", *IDENTITY_FIELDS,
                    "generation", "sequence", "execution_epoch", "command_id",
                    "idempotency_key", "sent_at", "payload",
                }
                allowed = required | {"request_hash"}
                if not required.issubset(message) or not set(message).issubset(allowed) or not isinstance(message.get("payload"), dict):
                    raise DeliveryError("MALFORMED_FRAME")
                if session.identity is not None and any(
                    message.get(field) != session.identity[field] for field in IDENTITY_FIELDS
                ):
                    raise DeliveryError("WRONG_ACCOUNT")
                typ = message["type"]
                message_id = message["message_id"]
                command_id = message["command_id"]
                request_hash = message.get("request_hash")
                if not isinstance(typ, str) or not typ or not isinstance(message_id, str) or not message_id:
                    raise DeliveryError("MALFORMED_FRAME")
                if command_id is not None and (not isinstance(command_id, str) or not command_id):
                    raise DeliveryError("MALFORMED_FRAME")
                if typ in COMMAND_TYPES or typ == "command.result":
                    if command_id is None or not isinstance(request_hash, str) or not request_hash:
                        raise DeliveryError("MALFORMED_FRAME")
                elif typ in CONTROL_ACK_TYPES:
                    if command_id is not None or request_hash is not None:
                        raise DeliveryError("MALFORMED_FRAME")
                    if not isinstance(message["payload"].get("control_id"), str):
                        raise DeliveryError("INVALID_CONTROL_ACK")
                elif command_id is not None or request_hash is not None:
                    raise DeliveryError("MALFORMED_FRAME")
                sequence = message["sequence"]
                if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
                    raise DeliveryError("MALFORMED_FRAME")
                if message["message_id"] in session.seen_message_ids:
                    raise DeliveryError("REPLAYED_SEQUENCE")
                if sequence != session.last_received_sequence + 1:
                    raise DeliveryError("OUT_OF_ORDER_SEQUENCE")
                epoch = message["execution_epoch"]
                if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
                    raise DeliveryError("MALFORMED_FRAME")
                if message["type"] in SIDE_EFFECTING_COMMANDS and session.execution_epoch is not None and epoch != session.execution_epoch:
                    raise DeliveryError("STALE_EPOCH")
                if not isinstance(message["idempotency_key"], str) or not message["idempotency_key"]:
                    raise DeliveryError("MALFORMED_FRAME")
                if not isinstance(message["sent_at"], str):
                    raise DeliveryError("MALFORMED_FRAME")
                try:
                    if datetime.fromisoformat(message["sent_at"].replace("Z", "+00:00")).tzinfo is None:
                        raise ValueError
                except ValueError as error:
                    raise DeliveryError("MALFORMED_FRAME") from error
                session.last_received_sequence = sequence
                session.seen_message_ids.add(message["message_id"])
                self._received_sequence[account_id] = sequence
                self._received_message_ids[account_id] = set(session.seen_message_ids)
                return message
            if message.get("type") == "heartbeat" and message.get("session_id") not in (None, session.session_id):
                raise DeliveryError("STALE_GENERATION")
            if message.get("type") == "heartbeat":
                if not set(message).issubset({"type", "account_id", "generation", "session_id"}):
                    raise DeliveryError("MALFORMED_FRAME")
                return message
            legacy_allowed = {
                "reconciliation_observation": {"type", "account_id", "generation", "observation"},
                "account_snapshot": {"type", "account_id", "generation", "payload", "snapshot"},
                "market_snapshot": {"type", "account_id", "generation", "payload", "snapshot"},
                "candle_batch": {"type", "account_id", "generation", "payload", "snapshot"},
            }
            if message.get("type") in legacy_allowed:
                if not set(message).issubset(legacy_allowed[message["type"]]):
                    raise DeliveryError("MALFORMED_FRAME")
                return message
            raise DeliveryError("MALFORMED_FRAME")

    async def current_sequence(self, account_id: str) -> int:
        async with self._lock:
            return self._last_sequence.get(account_id, 0)

    async def reserve_sequence(self, account_id: str, session_id: str) -> int:
        async with self._lock:
            session = self._sessions.get(account_id)
            if not session or session.session_id != session_id:
                raise DeliveryError("SESSION_NOT_ACTIVE")
            sequence = self._last_sequence.get(account_id, 0) + 1
            self._last_sequence[account_id] = sequence
            return sequence

    async def enqueue(
        self,
        *,
        account_id: str,
        identity: dict[str, str],
        connector_generation: int,
        dispatch_sequence: int,
        execution_epoch: int,
        command_id: str,
        idempotency_key: str,
        request_hash: str,
        type: str,
        payload: dict[str, Any],
    ) -> OutboundEnvelope:
        if type not in COMMAND_TYPES:
            raise DeliveryError("UNSUPPORTED_COMMAND_TYPE")
        if not account_id or not command_id or not idempotency_key or not request_hash:
            raise DeliveryError("MISSING_COMMAND_CONTEXT")
        if not isinstance(identity, dict) or set(identity) != IDENTITY_FIELDS or any(not isinstance(v, str) or not v for v in identity.values()):
            raise DeliveryError("INVALID_ACCOUNT_IDENTITY")
        if not isinstance(payload, dict):
            raise DeliveryError("INVALID_COMMAND_PAYLOAD")
        if (
            isinstance(dispatch_sequence, bool) or not isinstance(dispatch_sequence, int)
            or isinstance(connector_generation, bool) or not isinstance(connector_generation, int)
            or isinstance(execution_epoch, bool) or not isinstance(execution_epoch, int)
            or dispatch_sequence < 1 or connector_generation < 0 or execution_epoch < 0
        ):
            raise DeliveryError("INVALID_COMMAND_CONTEXT")
        async with self._lock:
            session = self._sessions.get(account_id)
            if not session:
                raise DeliveryError("SESSION_NOT_ACTIVE")
            if connector_generation != session.generation:
                raise DeliveryError("STALE_GENERATION")
            if session.identity is not None and any(
                identity[field] != session.identity[field] for field in IDENTITY_FIELDS
            ):
                raise DeliveryError("WRONG_ACCOUNT")
            if type in SIDE_EFFECTING_COMMANDS and session.reconciliation_required:
                raise DeliveryError("RECONCILIATION_REQUIRED")
            if type == "order.submit_market" and not session.execution_allowed:
                raise DeliveryError("EXECUTION_GATE_CLOSED")
            if (
                type in SIDE_EFFECTING_COMMANDS
                and session.execution_epoch is not None
                and execution_epoch != session.execution_epoch
            ):
                raise DeliveryError("STALE_EPOCH")
            pending = self._pending.setdefault(account_id, {})
            existing = pending.get(command_id)
            if existing is not None:
                if existing.envelope.idempotency_key == idempotency_key and existing.envelope.request_hash == request_hash:
                    raise DeliveryError("DUPLICATE_PENDING_COMMAND")
                raise DeliveryError("COMMAND_ID_REUSED")
            if any(item.envelope.idempotency_key == idempotency_key for item in pending.values()):
                raise DeliveryError("DUPLICATE_PENDING_COMMAND")
            last_dispatch = self._last_dispatch_sequence.get(account_id, 0)
            if dispatch_sequence <= last_dispatch:
                raise DeliveryError("OUT_OF_ORDER_DISPATCH")
            sequence = self._last_sequence.get(account_id, 0) + 1
            envelope = OutboundEnvelope(
                account_id, dict(identity), connector_generation, dispatch_sequence, sequence,
                execution_epoch, command_id, idempotency_key, request_hash, type, dict(payload),
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            self._last_dispatch_sequence[account_id] = dispatch_sequence
            self._last_sequence[account_id] = sequence
            pending[command_id] = _Pending(envelope)
            await session.queue.put(envelope)
            return envelope

    async def record_result(
        self,
        message: dict[str, Any],
        *,
        authenticated_account_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        if not isinstance(message, dict) or message.get("type") != "command.result":
            raise DeliveryError("MALFORMED_FRAME")
        allowed = {
            "schema_version", "type", "message_id", "account_id", *IDENTITY_FIELDS,
            "generation", "sequence", "execution_epoch", "command_id",
            "idempotency_key", "request_hash", "sent_at", "payload",
        }
        if not set(message).issubset(allowed):
            raise DeliveryError("MALFORMED_FRAME")
        if (
            isinstance(message.get("schema_version"), bool)
            or not isinstance(message.get("schema_version"), int)
            or message.get("schema_version") != 1
        ):
            raise DeliveryError("MALFORMED_FRAME")
        if any(field not in message for field in (
            "message_id", "sent_at", "sequence", "generation", *IDENTITY_FIELDS, "execution_epoch", "payload",
        )):
            raise DeliveryError("INVALID_COMMAND_RESULT")
        if (
            not isinstance(message["message_id"], str) or not message["message_id"]
            or not isinstance(message["sequence"], int) or isinstance(message["sequence"], bool)
            or message["sequence"] < 1
            or not isinstance(message["generation"], int) or isinstance(message["generation"], bool)
            or message["generation"] < 0
            or not isinstance(message["execution_epoch"], int) or isinstance(message["execution_epoch"], bool)
            or message["execution_epoch"] < 0
            or not isinstance(message["sent_at"], str)
        ):
            raise DeliveryError("INVALID_COMMAND_RESULT")
        if not isinstance(message.get("payload"), dict):
            raise DeliveryError("INVALID_COMMAND_RESULT")
        try:
            if datetime.fromisoformat(message["sent_at"].replace("Z", "+00:00")).tzinfo is None:
                raise ValueError
        except ValueError as error:
            raise DeliveryError("INVALID_COMMAND_RESULT") from error
        payload = message["payload"]
        result = payload.get("state")
        if result not in RESULT_STATES:
            raise DeliveryError("INVALID_COMMAND_RESULT")
        for field in ("account_id", "command_id", "idempotency_key", "request_hash"):
            if field not in message:
                raise DeliveryError("INVALID_COMMAND_RESULT")
            if not isinstance(message[field], str) or not message[field]:
                raise DeliveryError("MALFORMED_FRAME")
        generation = message["generation"]
        sequence = message["sequence"]
        account_id = message["account_id"]
        if authenticated_account_id is not None and account_id != authenticated_account_id:
            raise DeliveryError("WRONG_ACCOUNT")
        async with self._lock:
            session_account_id = authenticated_account_id or account_id
            session = self._sessions.get(session_account_id)
            if session is None or (session_id is not None and session.session_id != session_id):
                raise DeliveryError("SESSION_NOT_ACTIVE")
            pending = self._pending.get(account_id, {})
            item = pending.get(message["command_id"])
            if item is None:
                raise DeliveryError("UNKNOWN_COMMAND")
            envelope = item.envelope
            if any(message.get(field) != envelope.identity[field] for field in IDENTITY_FIELDS):
                raise DeliveryError("COMMAND_CONTEXT_MISMATCH")
            if generation != envelope.connector_generation:
                raise DeliveryError("STALE_GENERATION")
            if message["execution_epoch"] != envelope.execution_epoch:
                raise DeliveryError("STALE_EPOCH")
            if message["idempotency_key"] != envelope.idempotency_key:
                raise DeliveryError("COMMAND_CONTEXT_MISMATCH")
            if message["request_hash"] != envelope.request_hash:
                raise DeliveryError("COMMAND_CONTEXT_MISMATCH")
            if message["message_id"] in session.seen_message_ids:
                raise DeliveryError("REPLAYED_SEQUENCE")
            if message["sequence"] != session.last_received_sequence + 1:
                raise DeliveryError("OUT_OF_ORDER_SEQUENCE")
            session.last_received_sequence = message["sequence"]
            session.seen_message_ids.add(message["message_id"])
            self._received_sequence[session.account_id] = message["sequence"]
            self._received_message_ids[session.account_id] = set(session.seen_message_ids)
            if (
                result == "ACCEPTED"
                and envelope.type in SIDE_EFFECTING_COMMANDS
                and payload.get("readback_confirmed") is not True
            ):
                result = "UNKNOWN"
                payload["state"] = result
                payload.setdefault("code", "EFFECT_READBACK_REQUIRED")
            if (
                result == "ACCEPTED"
                and envelope.type == "position.modify_protection"
                and payload.get("protection_confirmed") is not True
            ):
                result = "UNKNOWN"
                payload["state"] = result
                payload.setdefault("code", "PROTECTION_READBACK_REQUIRED")
            if item.state != "SENT":
                if item.state == result:
                    return result
                raise DeliveryError("STALE_COMMAND_RESULT")
            item.state = result
            return result

    async def acknowledge_execution_gate(
        self, account_id: str, session_id: str, control_id: str, *,
        generation: int, execution_epoch: int,
    ) -> None:
        async with self._lock:
            session = self._sessions.get(account_id)
            if not session or session.session_id != session_id:
                raise DeliveryError("SESSION_NOT_ACTIVE")
            pending = self._pending.get(account_id, {}).get(control_id)
            if pending is None or pending.envelope.type not in CONTROL_UPDATE_TYPES:
                raise DeliveryError("UNKNOWN_CONTROL")
            if generation != pending.envelope.connector_generation:
                raise DeliveryError("STALE_GENERATION")
            if execution_epoch != pending.envelope.execution_epoch:
                raise DeliveryError("STALE_EPOCH")
            if pending.state != "SENT":
                raise DeliveryError("CONTROL_NOT_SENT")
            pending.state = "ACCEPTED"
            session.execution_epoch = execution_epoch
            session.execution_allowed = bool(pending.envelope.payload["backend_execution_gate"])
            if pending.control_ack is not None:
                pending.control_ack.set()

    async def record_control_ack(
        self, account_id: str, session_id: str, message: dict[str, Any],
    ) -> None:
        payload = message.get("payload") if isinstance(message, dict) else None
        if not isinstance(payload, dict) or not isinstance(payload.get("control_id"), str):
            raise DeliveryError("INVALID_CONTROL_ACK")
        await self.acknowledge_execution_gate(
            account_id, session_id, payload["control_id"],
            generation=message.get("generation"),
            execution_epoch=message.get("execution_epoch"),
        )

    async def mark_sent(
        self, account_id: str, command_id: str, session_id: str,
        *, entry_allowed: Callable[[OutboundEnvelope], bool] | None = None,
    ) -> OutboundEnvelope:
        async with self._lock:
            session = self._sessions.get(account_id)
            item = self._pending.get(account_id, {}).get(command_id)
            if not session or session.session_id != session_id:
                raise DeliveryError("SESSION_NOT_ACTIVE")
            if not item or item.state != "QUEUED":
                raise DeliveryError("INVALID_PENDING_COMMAND")
            if (
                item.envelope.type == "order.submit_market"
                and entry_allowed is not None
                and not entry_allowed(item.envelope)
            ):
                raise DeliveryError("STALE_EPOCH")
            item.state = "SENT"
            return item.envelope

    async def ensure_wire_write(
        self, account_id: str, command_id: str, session_id: str,
        *, entry_allowed: Callable[[OutboundEnvelope], bool] | None = None,
    ) -> OutboundEnvelope:
        """Recheck an entry after mark_sent and immediately before the wire write."""
        async with self._lock:
            session = self._sessions.get(account_id)
            item = self._pending.get(account_id, {}).get(command_id)
            if not session or session.session_id != session_id:
                raise DeliveryError("SESSION_NOT_ACTIVE")
            if item is None:
                raise DeliveryError("UNKNOWN_COMMAND")
            if item.state == "FENCED":
                raise DeliveryError("STALE_EPOCH")
            if item.state != "SENT":
                raise DeliveryError("INVALID_PENDING_COMMAND")
            if (
                item.envelope.type == "order.submit_market"
                and entry_allowed is not None
                and not entry_allowed(item.envelope)
            ):
                item.state = "FENCED"
                raise DeliveryError("STALE_EPOCH")
            return item.envelope

    def pending_state(self, account_id: str, command_id: str) -> str | None:
        item = self._pending.get(account_id, {}).get(command_id)
        return item.state if item else None


class ConnectorDeliveryBridge:
    """Connect durable Execution Coordination records to the WSS registry."""

    def __init__(self, coordinator: Any, registry: ConnectorDeliveryRegistry | None = None) -> None:
        self.coordinator = coordinator
        self.registry = registry or ConnectorDeliveryRegistry()

    async def _enqueue_record(self, record: ConnectorDispatchRecord) -> OutboundEnvelope:
        return await self.registry.enqueue(
            account_id=record.account_id,
            identity=record.identity,
            connector_generation=record.generation,
            dispatch_sequence=record.dispatch_sequence,
            execution_epoch=record.execution_epoch,
            command_id=record.command_id,
            idempotency_key=record.idempotency_key,
            request_hash=record.request_hash,
            type=record.command_type,
            payload=record.payload,
        )

    async def enqueue_order(
        self,
        account_id: str,
        order_id: str,
        *,
        identity: dict[str, str],
        generation: int,
    ) -> ConnectorDispatchRecord:
        record = self.coordinator.prepare_connector_dispatch(
            account_id, order_id, identity=identity, generation=generation,
        )
        try:
            await self._enqueue_record(record)
        except DeliveryError as error:
            if error.code not in DEFERRED_DELIVERY_ERRORS:
                raise
        return record

    async def enqueue_position_command(
        self,
        account_id: str,
        command_id: str,
        *,
        identity: dict[str, str],
        generation: int,
    ) -> ConnectorDispatchRecord:
        record = self.coordinator.prepare_connector_position_dispatch(
            account_id, command_id, identity=identity, generation=generation,
        )
        try:
            await self._enqueue_record(record)
        except DeliveryError as error:
            if error.code not in DEFERRED_DELIVERY_ERRORS:
                raise
        return record

    enqueue_position_modify_protection = enqueue_position_command
    enqueue_position_close = enqueue_position_command

    async def replay_unsent(self, account_id: str) -> tuple[OutboundEnvelope, ...]:
        """Replay only durable QUEUED records; SENT/UNKNOWN are reconciliation work."""
        delivered: list[OutboundEnvelope] = []
        for record in self.coordinator.pending_connector_dispatches(account_id):
            try:
                envelope = await self._enqueue_record(record)
            except DeliveryError as error:
                if error.code in DEFERRED_DELIVERY_ERRORS:
                    continue
                raise
            delivered.append(envelope)
        return tuple(delivered)

    async def mark_sent(self, account_id: str, command_id: str, session_id: str) -> OutboundEnvelope:
        with self.coordinator.account_lock(account_id):
            envelope = await self.registry.mark_sent(
                account_id, command_id, session_id,
                entry_allowed=lambda candidate: (
                    self.coordinator.account(account_id).exposure_gate == "OPEN"
                    and candidate.execution_epoch
                    == self.coordinator.account(account_id).execution_epoch
                ),
            )
            if envelope.type in CONTROL_UPDATE_TYPES:
                return envelope
            self.coordinator.mark_connector_dispatch_sent(account_id, command_id)
            return envelope

    async def ensure_wire_write(
        self, account_id: str, command_id: str, session_id: str,
    ) -> OutboundEnvelope:
        """Authorize the actual websocket write after the send checkpoint."""
        with self.coordinator.account_lock(account_id):
            try:
                return await self.registry.ensure_wire_write(
                    account_id, command_id, session_id,
                    entry_allowed=lambda candidate: (
                        self.coordinator.account(account_id).exposure_gate == "OPEN"
                        and candidate.execution_epoch
                        == self.coordinator.account(account_id).execution_epoch
                    ),
                )
            except DeliveryError as error:
                if error.code == "STALE_EPOCH":
                    record = self.coordinator.dispatch_records.get(command_id)
                    if record is not None and record.state in {"QUEUED", "SENT"}:
                        self.coordinator._fence_entry_record(record)
                raise

    def session_lost(self, account_id: str) -> tuple[ConnectorDispatchRecord, ...]:
        return self.coordinator.mark_connector_session_lost(account_id)

    async def record_result(
        self,
        message: dict[str, Any],
        *,
        authenticated_account_id: str,
        session_id: str,
    ) -> str:
        result = await self.registry.record_result(
            message,
            authenticated_account_id=authenticated_account_id,
            session_id=session_id,
        )
        try:
            self.coordinator.connector_dispatch_result(
                authenticated_account_id,
                message["command_id"],
                result,
                payload=message.get("payload"),
            )
        except Exception as error:
            raise DeliveryError(getattr(error, "code", "PROJECTION_FAILED")) from error
        return result


connector_delivery = ConnectorDeliveryRegistry()
