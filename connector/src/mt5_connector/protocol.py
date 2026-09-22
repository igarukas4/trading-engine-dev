"""Connector-side validation and command protocol."""
from __future__ import annotations

from datetime import datetime, timezone
import random
import time
import uuid
from typing import Any, Mapping

from .config import ConnectorConfig
from .models import (
    COMMAND_TYPES,
    SIDE_EFFECTING_TYPES,
    ContractError,
    HelloFrame,
    PostHandshakeEnvelope,
)

READ_ONLY = frozenset(COMMAND_TYPES - SIDE_EFFECTING_TYPES)
CONTROL_TYPES = frozenset({
    "heartbeat_ack", "reconciliation_observed", "command_result_ack", "error",
})


class ProtocolError(ContractError):
    """A safe, stable protocol error; never includes raw credential payloads."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(code, f"{code}: {message or code}")


class ConnectorProtocol:
    def __init__(self, cfg, adapter, transport=None, clock=time.time, random_fn=random.random,
                 dispatcher=None):
        self.cfg = cfg
        self.adapter = adapter
        self.dispatcher = dispatcher
        self.transport = transport
        self.clock = clock
        self.random = random_fn
        self.generation: int | None = None
        self.execution_epoch = 0
        self.sequence = 0
        self.last_server_sequence = 0
        self.session_id = str(uuid.uuid4())
        self._seen_server_messages: set[str] = set()
        self._results: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self._idempotency: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self._context_results: dict[tuple[str, str, str, int, str], dict[str, Any]] = {}

    def begin_session(self) -> str:
        """Rotate the transport session identity while retaining sequence state."""
        self.session_id = str(uuid.uuid4())
        return self.session_id

    def validate_hello(self, message: Mapping[str, Any]) -> HelloFrame:
        try:
            return HelloFrame.from_mapping(message)
        except ContractError as error:
            raise ProtocolError(error.code, str(error)) from error

    def hello(self, secret):
        if not secret:
            raise ProtocolError("MALFORMED_FRAME", "secret is required")
        frame = HelloFrame(
            account_id=self.cfg.account_id,
            identity=self._identity(),
            key_id=self.cfg.key_id,
            secret=secret,
            generation=self.cfg.backend_generation,
            session_id=self.session_id,
        )
        return frame.to_dict()

    def _identity(self):
        from .models import Identity
        return Identity(
            self.cfg.provider,
            self.cfg.broker_server,
            self.cfg.external_account_id,
        )

    def accept_snapshot(self, message):
        if not isinstance(message, dict) or message.get("type") != "snapshot":
            raise ProtocolError("MALFORMED_FRAME", "snapshot response is required")
        snapshot = message.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ProtocolError("MALFORMED_FRAME", "snapshot must be an object")
        account_id = snapshot.get("account_id")
        if account_id not in (None, self.cfg.account_id):
            raise ProtocolError("WRONG_ACCOUNT", "wrong account snapshot")
        incoming = message.get("generation", self.cfg.backend_generation)
        if isinstance(incoming, bool) or not isinstance(incoming, int) or incoming < 0:
            raise ProtocolError("MALFORMED_FRAME", "invalid generation")
        if self.generation is None:
            self.generation = incoming
        elif incoming != self.generation:
            raise ProtocolError("STALE_GENERATION", "generation mismatch")
        epoch = message.get("execution_epoch", snapshot.get("execution_epoch", 0))
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ProtocolError("MALFORMED_FRAME", "invalid execution_epoch")
        self.execution_epoch = epoch
        server_sequence = message.get("server_sequence")
        if server_sequence is not None:
            if isinstance(server_sequence, bool) or not isinstance(server_sequence, int) or server_sequence < 0:
                raise ProtocolError("MALFORMED_FRAME", "invalid server_sequence")
            if server_sequence < self.last_server_sequence:
                raise ProtocolError("OUT_OF_ORDER_SEQUENCE", "server sequence moved backwards")
            self.last_server_sequence = server_sequence
        return snapshot

    def _sent_at(self) -> str:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc).isoformat().replace("+00:00", "Z")

    def _envelope(self, typ, payload=None, command_id=None, *, execution_epoch=None, request_hash=None):
        self.sequence += 1
        message_id = str(uuid.uuid4())
        result = {
            "schema_version": 1,
            "type": typ,
            "message_id": message_id,
            "account_id": self.cfg.account_id,
            **self._identity().to_dict(),
            "generation": self.generation if self.generation is not None else self.cfg.backend_generation,
            "sequence": self.sequence,
            "execution_epoch": self.execution_epoch if execution_epoch is None else execution_epoch,
            "command_id": command_id,
            "idempotency_key": "msg:" + message_id,
            "sent_at": self._sent_at(),
            "payload": payload or {},
        }
        if request_hash is not None:
            result["request_hash"] = request_hash
        return result

    def heartbeat(self):
        return self._envelope(
            "heartbeat",
            {"terminal_connected": True, "last_mt5_error": None, "journal_state": "READY"},
        )

    def _validate_inbound(self, message: Mapping[str, Any]) -> PostHandshakeEnvelope:
        try:
            envelope = PostHandshakeEnvelope.from_mapping(message)
        except ContractError as error:
            raise ProtocolError(error.code, str(error)) from error
        if envelope.account_id != self.cfg.account_id:
            raise ProtocolError("WRONG_ACCOUNT", "wrong account")
        if envelope.identity.to_dict() != self._identity().to_dict():
            raise ProtocolError("WRONG_ACCOUNT", "wrong account identity")
        expected_generation = self.generation if self.generation is not None else self.cfg.backend_generation
        if envelope.generation != expected_generation:
            raise ProtocolError("STALE_GENERATION", "generation mismatch")
        if envelope.message_id in self._seen_server_messages:
            raise ProtocolError("REPLAYED_SEQUENCE", "replayed sequence")
        if envelope.sequence != self.last_server_sequence + 1:
            raise ProtocolError("OUT_OF_ORDER_SEQUENCE", "out-of-order sequence")
        self.last_server_sequence = envelope.sequence
        self._seen_server_messages.add(envelope.message_id)
        return envelope

    def handle(self, message):
        if not isinstance(message, dict):
            raise ProtocolError("MALFORMED_FRAME", "invalid frame")
        # The pre-contract backend used a small heartbeat acknowledgement. Keep
        # that compatibility form, but never use it to authorize a command.
        legacy_context = set(message) <= {"type", "account_id", "generation", "session_id"}
        if legacy_context:
            self._validate_legacy_context(message)
            typ = message.get("type")
            if typ == "heartbeat_ack":
                return message
            if typ in SIDE_EFFECTING_TYPES:
                return self._legacy_result(message)
            raise ProtocolError("MALFORMED_FRAME", "post-handshake envelope is required")

        envelope = self._validate_inbound(message)
        if envelope.type == "reconciliation.required":
            return self._reconciliation_responses(envelope.payload)
        if envelope.type in CONTROL_TYPES:
            return message
        if envelope.type not in COMMAND_TYPES:
            raise ProtocolError("UNSUPPORTED_COMMAND", "unsupported command")
        if envelope.type in SIDE_EFFECTING_TYPES:
            if envelope.execution_epoch != self.execution_epoch:
                return self._context_rejection(envelope, "STALE_EPOCH")
            if self.dispatcher is None:
                return self._result_for(envelope, "REJECTED", "EXECUTION_DISABLED")
            return self._dispatch_side_effect(envelope)
        return self._read_only_response(envelope)

    def _validate_legacy_context(self, message: Mapping[str, Any]) -> None:
        if message.get("account_id") not in (None, self.cfg.account_id):
            raise ProtocolError("WRONG_ACCOUNT", "wrong account")
        expected_generation = self.generation if self.generation is not None else self.cfg.backend_generation
        generation = message.get("generation", expected_generation)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ProtocolError("MALFORMED_FRAME", "invalid generation")
        if generation != expected_generation:
            raise ProtocolError("STALE_GENERATION", "generation mismatch")

    def _legacy_result(self, message: Mapping[str, Any]) -> dict[str, Any]:
        command_id = message.get("command_id")
        if not command_id:
            raise ProtocolError("MALFORMED_FRAME", "command_id is required")
        key = message.get("idempotency_key", "legacy:" + command_id)
        request_hash = message.get("request_hash", "legacy:" + command_id)
        return self._legacy_result_for(command_id, key, request_hash)

    @staticmethod
    def _recovery_match(record: Any, rows: list[Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Match only an explicit durable correlation, never broker heuristics."""
        if not isinstance(record, dict):
            return ({"status": "UNRESOLVED", "reason": "INVALID_RECOVERY_RECORD"}, None)
        subject_id = record.get("subject_id", record.get("order_id"))
        kind = record.get("kind")
        if (
            not isinstance(subject_id, str)
            or not subject_id
            or not isinstance(kind, str)
            or kind not in {"ORDER", "POSITION_COMMAND", "COMMAND"}
        ):
            return ({"status": "UNRESOLVED", "reason": "INVALID_RECOVERY_RECORD"}, None)
        matches: list[tuple[str, dict[str, Any]]] = []
        for source, row in rows:
            if not isinstance(row, dict):
                continue
            for field in ("command_id", "correlation_id", "position_id", "order_id"):
                if str(row.get(field, "")) == subject_id:
                    matches.append((source, row))
                    break
        evidence = {"subject_id": subject_id, "kind": kind}
        if len(matches) != 1:
            evidence.update({
                "status": "UNRESOLVED",
                "reason": "CORRELATION_ABSENT" if not matches else "MULTIPLE_CORRELATIONS",
            })
            return evidence, None
        source, row = matches[0]
        evidence.update({
            "status": "MATCHED",
            "source": source,
            "matched_by": next(
                field for field in ("command_id", "correlation_id", "position_id", "order_id")
                if str(row.get(field, "")) == subject_id
            ),
        })
        return evidence, row

    def _reconciliation_responses(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        from_server_time = payload.get("from_server_time")
        try:
            account = self.adapter.account_snapshot().to_dict()
            orders = self.adapter.open_orders().to_dict()
            positions = self.adapter.open_positions().to_dict()
            history_orders = self.adapter.history_orders(from_server_time).to_dict()
            history_deals = self.adapter.history_deals(from_server_time).to_dict()
        except Exception as error:
            raise ProtocolError("ADAPTER_ERROR", "reconciliation read failed") from error
        order_rows = [
            ("open_orders", row) for row in orders.get("items", [])
        ] + [
            ("history_orders", row) for row in history_orders.get("items", [])
        ] + [
            ("history_deals", row) for row in history_deals.get("items", [])
        ]
        recovery_matches: list[dict[str, Any]] = []
        commands: list[dict[str, Any]] = []
        position_commands: list[dict[str, Any]] = []
        broker_order_ids: dict[str, str] = {}
        recovered_orders = list(orders.get("items", []))
        for record in payload.get("recovery", []):
            match, row = self._recovery_match(record, order_rows)
            recovery_matches.append(match)
            if row is None:
                continue
            subject_id = match["subject_id"]
            status = str(row.get("status", row.get("state", "UNKNOWN"))).upper()
            kind = match["kind"]
            if kind == "ORDER":
                mapped = dict(row)
                mapped["order_id"] = subject_id
                mapped.setdefault("external_id", row.get("ticket", row.get("order")))
                recovered_orders.append(mapped)
                for field in ("ticket", "order", "order_id"):
                    if row.get(field) is not None:
                        broker_order_ids[str(row[field])] = subject_id
            elif kind == "POSITION_COMMAND":
                position_commands.append({"command_id": subject_id, "status": status, "evidence": match})
            elif kind == "COMMAND":
                commands.append({"command_id": subject_id, "status": status, "evidence": match})
        deals = history_deals.get("items", [])
        fills = []
        for deal in deals:
            if not isinstance(deal, dict):
                continue
            fill = dict(deal)
            fill.setdefault("deal_id", fill.get("ticket"))
            fill.setdefault("order_id", fill.get("order"))
            if str(fill.get("order_id", "")) in broker_order_ids:
                fill["order_id"] = broker_order_ids[str(fill["order_id"])]
            fill.setdefault("volume", fill.get("volume", "0"))
            if fill.get("deal_id") and fill.get("order_id"):
                fills.append(fill)
        observation = {
            "account_id": self.cfg.account_id,
            "complete": True,
            "from_server_time": from_server_time,
            "account": account,
            "orders": recovered_orders,
            "positions": positions.get("items", []),
            "history_orders": history_orders.get("items", []),
            "deals": deals,
            "fills": fills,
            "commands": commands,
            "position_commands": position_commands,
            "recovery": payload.get("recovery", []),
            "recovery_matches": recovery_matches,
            "sequence_watermark": self.sequence,
        }
        return [
            self._envelope("account_snapshot", account),
            self._envelope("reconciliation_observation", {"observation": observation}),
        ]

    def _replay_result(self, result: dict[str, Any]) -> dict[str, Any]:
        replay = dict(result)
        replay["message_id"] = str(uuid.uuid4())
        self.sequence += 1
        replay["sequence"] = self.sequence
        replay["sent_at"] = self._sent_at()
        return replay

    def _legacy_result_for(self, command_id: str, key: str, request_hash: str) -> dict[str, Any]:
        prior = self._results.get(command_id)
        if prior is not None:
            prior_key, prior_hash, result = prior
            if prior_key == key and prior_hash == request_hash:
                return self._replay_result(result)
            raise ProtocolError("IDEMPOTENCY_KEY_REUSED", "idempotency key was reused")
        result = self._envelope(
            "command.result",
            {"state": "REJECTED", "code": "EXECUTION_DISABLED"},
            command_id,
            request_hash=request_hash,
        )
        result["idempotency_key"] = key
        self._results[command_id] = (key, request_hash, result)
        self._idempotency[key] = (command_id, request_hash, result)
        return result

    def _context_rejection(self, envelope: PostHandshakeEnvelope, code: str) -> dict[str, Any]:
        key = (
            envelope.command_id or "",
            envelope.idempotency_key,
            envelope.request_hash or "",
            envelope.execution_epoch,
            code,
        )
        prior = self._context_results.get(key)
        if prior is not None:
            return self._replay_result(prior)
        result = self._envelope(
            "command.result",
            {"state": "REJECTED", "code": code},
            envelope.command_id,
            execution_epoch=envelope.execution_epoch,
            request_hash=envelope.request_hash,
        )
        result["idempotency_key"] = envelope.idempotency_key
        self._context_results[key] = result
        return result

    def _result_for(self, envelope: PostHandshakeEnvelope, state: str, code: str) -> dict[str, Any]:
        prior = self._results.get(envelope.command_id or "")
        if prior is not None:
            prior_key, prior_hash, result = prior
            if prior_key == envelope.idempotency_key and prior_hash == envelope.request_hash:
                return self._replay_result(result)
            raise ProtocolError("IDEMPOTENCY_KEY_REUSED", "idempotency key was reused")
        prior_key = self._idempotency.get(envelope.idempotency_key)
        if prior_key is not None:
            command_id, prior_hash, result = prior_key
            if command_id == envelope.command_id and prior_hash == envelope.request_hash:
                return self._replay_result(result)
            raise ProtocolError("IDEMPOTENCY_KEY_REUSED", "idempotency key was reused")
        return self._record_result(envelope, {"state": state, "code": code})

    def _record_result(self, envelope: PostHandshakeEnvelope,
                       payload: Mapping[str, Any]) -> dict[str, Any]:
        result = self._envelope(
            "command.result",
            payload,
            envelope.command_id,
            execution_epoch=envelope.execution_epoch,
            request_hash=envelope.request_hash,
        )
        result["idempotency_key"] = envelope.idempotency_key
        self._results[envelope.command_id or ""] = (
            envelope.idempotency_key, envelope.request_hash or "", result,
        )
        self._idempotency[envelope.idempotency_key] = (
            envelope.command_id or "", envelope.request_hash or "", result,
        )
        return result

    def _dispatch_side_effect(self, envelope: PostHandshakeEnvelope) -> dict[str, Any]:
        """Pass one validated command through the journal-first dispatcher."""
        command = envelope.to_dict()
        try:
            result_payload = self.dispatcher.dispatch(command)
        except Exception as error:
            raise ProtocolError("DISPATCH_ERROR", "command dispatch failed") from error
        if result_payload.get("code") == "IDEMPOTENCY_KEY_REUSED":
            raise ProtocolError("IDEMPOTENCY_KEY_REUSED", "idempotency key was reused")
        return self._record_result(envelope, result_payload)

    def _read_only_response(self, envelope: PostHandshakeEnvelope) -> dict[str, Any]:
        payload = envelope.payload
        prior = self._results.get(envelope.command_id or "")
        if prior is not None:
            prior_key, prior_hash, result = prior
            if prior_key == envelope.idempotency_key and prior_hash == envelope.request_hash:
                return self._replay_result(result)
            raise ProtocolError("IDEMPOTENCY_KEY_REUSED", "idempotency key was reused")
        prior_key = self._idempotency.get(envelope.idempotency_key)
        if prior_key is not None:
            command_id, prior_hash, result = prior_key
            if command_id == envelope.command_id and prior_hash == envelope.request_hash:
                return self._replay_result(result)
            raise ProtocolError("IDEMPOTENCY_KEY_REUSED", "idempotency key was reused")
        if envelope.type == "account_snapshot.request":
            data = self.adapter.account_snapshot().to_dict()
            result_payload = {"state": "ACCEPTED", "code": "SNAPSHOT_READY", "snapshot": data}
        elif envelope.type == "market_snapshot.request":
            symbols = payload.get("symbols")
            if not isinstance(symbols, list) or any(not isinstance(symbol, str) or not symbol for symbol in symbols):
                raise ProtocolError("MALFORMED_FRAME", "symbols must be a list of non-empty strings")
            data = {symbol: self.adapter.symbol_info(symbol).to_dict() for symbol in symbols}
            result_payload = {"state": "ACCEPTED", "code": "SNAPSHOT_READY", "snapshot": data}
        elif envelope.type == "candle_batch.request":
            if payload.get("closed_only") is not True:
                raise ProtocolError("MALFORMED_FRAME", "closed_only is mandatory")
            symbol = payload.get("symbol")
            count = payload.get("count", 200)
            if not isinstance(symbol, str) or not symbol or isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ProtocolError("MALFORMED_FRAME", "invalid candle batch request")
            data = self.adapter.closed_m1(symbol, count).to_dict()
            result_payload = {"state": "ACCEPTED", "code": "SNAPSHOT_READY", "snapshot": data}
        elif envelope.type == "reconcile.request":
            data = self.adapter.account_snapshot().to_dict()
            result_payload = {"state": "ACCEPTED", "code": "RECONCILIATION_READY", "snapshot": data}
        else:
            raise ProtocolError("UNSUPPORTED_COMMAND", "unsupported command")
        return self._record_result(envelope, result_payload)

    def backoff(self, attempt):
        return min(
            self.cfg.reconnect_max,
            max(0.0, self.cfg.reconnect_initial * (2 ** attempt) *
                (1 + self.cfg.jitter * (self.random() * 2 - 1))),
        )
