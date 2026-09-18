"""In-memory account-scoped connector command delivery for the MVP gate.

This registry is transport state only. It is intentionally not crash durable and
must not be confused with the connector SQLite journal or a backend outbox.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal


COMMAND_TYPES = frozenset({
    "account_snapshot.request",
    "market_snapshot.request",
    "candle_batch.request",
    "order.submit_market",
    "position.modify_protection",
    "position.close",
    "reconcile.request",
})
RESULT_STATES = frozenset({"ACCEPTED", "REJECTED", "UNKNOWN"})


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
    execution_epoch: int
    command_id: str
    idempotency_key: str
    request_hash: str
    type: str
    payload: dict[str, Any]

    def as_message(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "type": self.type,
            "message_id": f"dispatch:{self.command_id}",
            "account_id": self.account_id,
            "provider": self.identity.get("provider", ""),
            "broker_server": self.identity.get("broker_server", ""),
            "external_account_id": self.identity.get("external_account_id", ""),
            "generation": self.connector_generation,
            "connector_generation": self.connector_generation,
            "dispatch_sequence": self.dispatch_sequence,
            "sequence": self.dispatch_sequence,
            "execution_epoch": self.execution_epoch,
            "command_id": self.command_id,
            "idempotency_key": self.idempotency_key,
            "request_hash": self.request_hash,
            "payload": self.payload,
        }


@dataclass
class _Pending:
    envelope: OutboundEnvelope
    state: Literal["QUEUED", "SENT", "ACCEPTED", "REJECTED", "UNKNOWN"] = "QUEUED"


@dataclass
class _Session:
    account_id: str
    generation: int
    session_id: str
    queue: asyncio.Queue[OutboundEnvelope]


class ConnectorDeliveryRegistry:
    """One active transport session and ordered delivery stream per account."""

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._pending: dict[str, dict[str, _Pending]] = {}
        self._last_sequence: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def open_session(self, account_id: str, generation: int, session_id: str) -> None:
        if not account_id or not session_id or generation < 0:
            raise DeliveryError("INVALID_SESSION")
        async with self._lock:
            if account_id in self._sessions:
                raise DeliveryError("SESSION_ALREADY_ACTIVE")
            self._sessions[account_id] = _Session(
                account_id, generation, session_id, asyncio.Queue()
            )
            self._pending.setdefault(account_id, {})

    async def close_session(self, account_id: str, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(account_id)
            if session and session.session_id == session_id:
                del self._sessions[account_id]

    async def next_for_session(self, account_id: str, session_id: str) -> OutboundEnvelope:
        session = self._sessions.get(account_id)
        if not session or session.session_id != session_id:
            raise DeliveryError("SESSION_NOT_ACTIVE")
        return await session.queue.get()

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
        if not isinstance(identity, dict) or not identity or any(not k or not v for k, v in identity.items()):
            raise DeliveryError("INVALID_ACCOUNT_IDENTITY")
        if dispatch_sequence < 1 or connector_generation < 0 or execution_epoch < 0:
            raise DeliveryError("INVALID_COMMAND_CONTEXT")
        async with self._lock:
            session = self._sessions.get(account_id)
            if not session:
                raise DeliveryError("SESSION_NOT_ACTIVE")
            if connector_generation != session.generation:
                raise DeliveryError("STALE_GENERATION")
            pending = self._pending.setdefault(account_id, {})
            existing = pending.get(command_id)
            if existing is not None:
                if existing.envelope.idempotency_key == idempotency_key and existing.envelope.request_hash == request_hash:
                    raise DeliveryError("DUPLICATE_PENDING_COMMAND")
                raise DeliveryError("COMMAND_ID_REUSED")
            if any(item.envelope.idempotency_key == idempotency_key for item in pending.values()):
                raise DeliveryError("DUPLICATE_PENDING_COMMAND")
            last = self._last_sequence.get(account_id, 0)
            if dispatch_sequence <= last:
                raise DeliveryError("OUT_OF_ORDER_DISPATCH")
            envelope = OutboundEnvelope(
                account_id, dict(identity), connector_generation, dispatch_sequence,
                execution_epoch, command_id, idempotency_key, request_hash, type, dict(payload),
            )
            self._last_sequence[account_id] = dispatch_sequence
            pending[command_id] = _Pending(envelope)
            await session.queue.put(envelope)
            return envelope

    async def record_result(self, message: dict[str, Any]) -> str:
        if message.get("type") not in {"command.result", "command_result"}:
            raise DeliveryError("INVALID_COMMAND_RESULT")
        payload = message.get("payload")
        result = payload.get("state") if isinstance(payload, dict) else message.get("result")
        required = ("account_id", "command_id", "idempotency_key", "request_hash")
        if any(not message.get(field) for field in required):
            raise DeliveryError("INVALID_COMMAND_RESULT")
        generation = message.get("connector_generation", message.get("generation"))
        sequence = message.get("dispatch_sequence", message.get("sequence"))
        if generation is None or sequence is None or result not in RESULT_STATES:
            raise DeliveryError("INVALID_COMMAND_RESULT")
        account_id = message["account_id"]
        async with self._lock:
            pending = self._pending.get(account_id, {})
            item = pending.get(message["command_id"])
            if item is None:
                raise DeliveryError("UNKNOWN_COMMAND")
            envelope = item.envelope
            if (generation != envelope.connector_generation or sequence != envelope.dispatch_sequence
                    or message["idempotency_key"] != envelope.idempotency_key
                    or message["request_hash"] != envelope.request_hash):
                raise DeliveryError("COMMAND_CONTEXT_MISMATCH")
            if item.state != "SENT":
                raise DeliveryError("STALE_COMMAND_RESULT")
            item.state = result
            return result

    async def mark_sent(self, account_id: str, command_id: str, session_id: str) -> OutboundEnvelope:
        async with self._lock:
            session = self._sessions.get(account_id)
            item = self._pending.get(account_id, {}).get(command_id)
            if not session or session.session_id != session_id:
                raise DeliveryError("SESSION_NOT_ACTIVE")
            if not item or item.state != "QUEUED":
                raise DeliveryError("INVALID_PENDING_COMMAND")
            item.state = "SENT"
            return item.envelope

    def pending_state(self, account_id: str, command_id: str) -> str | None:
        item = self._pending.get(account_id, {}).get(command_id)
        return item.state if item else None


connector_delivery = ConnectorDeliveryRegistry()
