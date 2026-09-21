"""Typed, redaction-safe wire models for the connector contract."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
import json


IDENTITY_FIELDS = ("provider", "broker_server", "external_account_id")
HELLO_FIELDS = frozenset({
    "type", "account_id", "provider", "broker_server", "external_account_id",
    "key_id", "secret", "generation", "session_id",
})
COMMAND_TYPES = frozenset({
    "account_snapshot.request",
    "market_snapshot.request",
    "candle_batch.request",
    "order.submit_market",
    "position.modify_protection",
    "position.close",
    "reconcile.request",
})
SIDE_EFFECTING_TYPES = frozenset({
    "order.submit_market", "position.modify_protection", "position.close",
})


class ContractError(ValueError):
    """A safe protocol validation error identified by a stable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def _nonempty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError("MALFORMED_FRAME", f"invalid {field_name}")
    return value


def _generation(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError("MALFORMED_FRAME", "invalid generation")
    return value


def _sequence(value: Any, field_name: str = "sequence") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError("MALFORMED_FRAME", f"invalid {field_name}")
    return value


def _utc_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise ContractError("MALFORMED_FRAME", "invalid sent_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("MALFORMED_FRAME", "invalid sent_at") from exc
    if parsed.tzinfo is None:
        raise ContractError("MALFORMED_FRAME", "sent_at must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, repr=False)
class Identity:
    provider: str
    broker_server: str
    external_account_id: str

    def __post_init__(self) -> None:
        for name in IDENTITY_FIELDS:
            _nonempty_string(getattr(self, name), name)

    def to_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in IDENTITY_FIELDS}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Identity":
        if not isinstance(raw, Mapping) or any(name not in raw for name in IDENTITY_FIELDS):
            raise ContractError("MALFORMED_FRAME", "incomplete account identity")
        return cls(*(raw[name] for name in IDENTITY_FIELDS))


@dataclass(frozen=True, repr=False)
class HelloFrame:
    account_id: str
    identity: Identity
    key_id: str
    secret: str = field(repr=False)
    generation: int = 0
    session_id: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "HelloFrame":
        if not isinstance(raw, Mapping) or set(raw) != HELLO_FIELDS or raw.get("type") != "hello":
            raise ContractError("MALFORMED_FRAME", "hello must contain exactly nine fields")
        return cls(
            account_id=_nonempty_string(raw["account_id"], "account_id"),
            identity=Identity(raw["provider"], raw["broker_server"], raw["external_account_id"]),
            key_id=_nonempty_string(raw["key_id"], "key_id"),
            secret=_nonempty_string(raw["secret"], "secret"),
            generation=_generation(raw["generation"]),
            session_id=_nonempty_string(raw["session_id"], "session_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "account_id": self.account_id,
            **self.identity.to_dict(),
            "key_id": self.key_id,
            "secret": self.secret,
            "generation": self.generation,
            "session_id": self.session_id,
        }

    def redacted_dict(self) -> dict[str, Any]:
        result = self.to_dict()
        result["secret"] = "[REDACTED]"
        return result

    def __repr__(self) -> str:
        return f"HelloFrame(account_id={self.account_id!r}, generation={self.generation!r}, session_id={self.session_id!r})"


@dataclass(frozen=True, repr=False)
class PostHandshakeEnvelope:
    schema_version: int
    type: str
    message_id: str
    account_id: str
    identity: Identity
    generation: int
    sequence: int
    execution_epoch: int
    command_id: str | None
    idempotency_key: str
    sent_at: str
    payload: dict[str, Any]
    request_hash: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PostHandshakeEnvelope":
        required = {
            "schema_version", "type", "message_id", "account_id", *IDENTITY_FIELDS,
            "generation", "sequence", "execution_epoch", "command_id",
            "idempotency_key", "sent_at", "payload",
        }
        allowed = required | {"request_hash"}
        if not isinstance(raw, Mapping) or not required.issubset(raw) or not set(raw).issubset(allowed):
            raise ContractError("MALFORMED_FRAME", "incomplete or unknown envelope fields")
        if isinstance(raw.get("schema_version"), bool) or raw.get("schema_version") != 1:
            raise ContractError("MALFORMED_FRAME", "unsupported schema version")
        typ = _nonempty_string(raw["type"], "type")
        command_id = raw["command_id"]
        if command_id is not None:
            command_id = _nonempty_string(command_id, "command_id")
        key = _nonempty_string(raw["idempotency_key"], "idempotency_key")
        request_hash = raw.get("request_hash")
        if typ in COMMAND_TYPES or typ == "command.result":
            if not isinstance(request_hash, str) or not request_hash:
                raise ContractError("MISSING_REQUEST_HASH", "command request_hash is required")
            if command_id is None:
                raise ContractError("MALFORMED_FRAME", "command_id is required")
        elif command_id is not None or request_hash is not None:
            raise ContractError("MALFORMED_FRAME", "telemetry command context must be null")
        if not isinstance(raw["payload"], dict):
            raise ContractError("MALFORMED_FRAME", "payload must be an object")
        epoch = raw["execution_epoch"]
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ContractError("MALFORMED_FRAME", "invalid execution_epoch")
        return cls(
            schema_version=1,
            type=typ,
            message_id=_nonempty_string(raw["message_id"], "message_id"),
            account_id=_nonempty_string(raw["account_id"], "account_id"),
            identity=Identity(raw["provider"], raw["broker_server"], raw["external_account_id"]),
            generation=_generation(raw["generation"]),
            sequence=_sequence(raw["sequence"]),
            execution_epoch=epoch,
            command_id=command_id,
            idempotency_key=key,
            sent_at=_utc_timestamp(raw["sent_at"]),
            payload=dict(raw["payload"]),
            request_hash=request_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "type": self.type,
            "message_id": self.message_id,
            "account_id": self.account_id,
            **self.identity.to_dict(),
            "generation": self.generation,
            "sequence": self.sequence,
            "execution_epoch": self.execution_epoch,
            "command_id": self.command_id,
            "idempotency_key": self.idempotency_key,
            "sent_at": self.sent_at,
            "payload": self.payload,
        }
        if self.request_hash is not None:
            result["request_hash"] = self.request_hash
        return result

    def __repr__(self) -> str:
        return f"PostHandshakeEnvelope(type={self.type!r}, account_id={self.account_id!r}, sequence={self.sequence!r}, command_id={self.command_id!r})"


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    identity: Identity
    login: str
    currency: str
    leverage: int
    balance: str
    equity: str
    trade_allowed: bool
    margin_mode: str
    orders: tuple[dict[str, Any], ...] = ()
    positions: tuple[dict[str, Any], ...] = ()
    deals: tuple[dict[str, Any], ...] = ()

    def to_dict(self):
        d = asdict(self)
        d["identity"] = self.identity.to_dict()
        return d

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True)


@dataclass(frozen=True)
class ReadSnapshot:
    kind: str
    data: dict[str, Any]

    def to_dict(self):
        return {"kind": self.kind, **self.data}
