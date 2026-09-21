"""Durable, account-scoped SQLite command journal."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import threading
from typing import Any, Mapping


STATES = frozenset({"RECEIVED", "INVOKING", "ACCEPTED", "REJECTED", "UNKNOWN"})


def _compact_json(value: Any, *, ensure_ascii: bool = True) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=ensure_ascii)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_request_hash(command_type: str, payload: Mapping[str, Any]) -> str:
    """Hash the command type and JSON payload deterministically."""
    document = {"command_type": command_type, "payload": payload}
    encoded = _compact_json(document, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class JournalCommand:
    account_id: str
    command_id: str
    generation: int
    dispatch_sequence: int
    idempotency_key: str
    request_hash: str
    execution_epoch: int
    command_type: str
    request_json: str
    state: str
    phase: str
    mt5_invoked: bool
    retcode: int | None
    external_order_id: str | None
    external_deal_id: str | None
    external_position_ids_json: str
    result_json: str | None
    error_code: str | None
    created_at: str
    updated_at: str
    resolved_at: str | None

    @property
    def request(self) -> dict[str, Any]:
        return json.loads(self.request_json)

    @property
    def result(self) -> dict[str, Any] | None:
        return json.loads(self.result_json) if self.result_json else None


class JournalError(RuntimeError):
    pass


class IdempotencyConflict(JournalError):
    pass


class SQLiteJournal:
    def __init__(self, path: str, account_id: str, *, clock=utc_now):
        self.path = path
        self.account_id = account_id
        self.clock = clock
        self._lock = threading.RLock()
        try:
            self.connection = sqlite3.connect(path, check_same_thread=False)
            self.connection.row_factory = sqlite3.Row
            self._configure()
            self._create_schema()
            self._integrity_check()
        except (sqlite3.Error, OSError) as exc:
            raise JournalError("JOURNAL_UNAVAILABLE") from exc

    def _configure(self) -> None:
        for pragma in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=FULL", "PRAGMA foreign_keys=ON"):
            self.connection.execute(pragma)

    def _create_schema(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS connector_commands (
            account_id TEXT NOT NULL, command_id TEXT NOT NULL, generation INTEGER NOT NULL,
            dispatch_sequence INTEGER NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
            execution_epoch INTEGER NOT NULL, command_type TEXT NOT NULL, request_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('RECEIVED','INVOKING','ACCEPTED','REJECTED','UNKNOWN')),
            phase TEXT NOT NULL DEFAULT 'RECEIVED', mt5_invoked INTEGER NOT NULL DEFAULT 0,
            retcode INTEGER, external_order_id TEXT, external_deal_id TEXT,
            external_position_ids_json TEXT NOT NULL DEFAULT '[]', result_json TEXT, error_code TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT,
            PRIMARY KEY (account_id, command_id), UNIQUE (account_id, idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS connector_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, command_id TEXT,
            observed_at TEXT NOT NULL, source TEXT NOT NULL, snapshot_json TEXT NOT NULL,
            match_status TEXT, UNIQUE (account_id, command_id, observed_at, source)
        );
        CREATE TABLE IF NOT EXISTS connector_meta (
            account_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, next_client_sequence INTEGER NOT NULL,
            last_server_sequence INTEGER NOT NULL, last_reconciliation_watermark TEXT, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS connector_commands_account_sequence
            ON connector_commands(account_id, dispatch_sequence);
        """)
        self.connection.commit()

    def _integrity_check(self) -> None:
        row = self.connection.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise JournalError("JOURNAL_CORRUPT")

    def _row(self, row) -> JournalCommand | None:
        return JournalCommand(**dict(row)) if row else None

    def _lookup(self, column: str, value: str) -> JournalCommand | None:
        if column not in {"command_id", "idempotency_key"}:
            raise ValueError("unsupported journal lookup")
        return self._row(self.connection.execute(
            f"SELECT * FROM connector_commands WHERE account_id=? AND {column}=?",
            (self.account_id, value),
        ).fetchone())

    def get(self, command_id: str) -> JournalCommand | None:
        with self._lock:
            return self._lookup("command_id", command_id)

    def get_by_idempotency(self, key: str) -> JournalCommand | None:
        with self._lock:
            return self._lookup("idempotency_key", key)

    def last_dispatch_sequence(self) -> int:
        with self._lock:
            row = self.connection.execute(
                "SELECT MAX(dispatch_sequence) FROM connector_commands WHERE account_id=?",
                (self.account_id,),
            ).fetchone()
            return int(row[0] or 0)

    def receive(self, *, command_id: str, generation: int, dispatch_sequence: int,
                idempotency_key: str, request_hash: str, execution_epoch: int,
                command_type: str, request: Mapping[str, Any]) -> JournalCommand:
        with self._lock:
            existing = self.get(command_id)
            by_key = self.get_by_idempotency(idempotency_key)
            if existing or by_key:
                prior = existing or by_key
                if prior.request_hash != request_hash or prior.command_id != command_id or prior.idempotency_key != idempotency_key:
                    raise IdempotencyConflict("IDEMPOTENCY_KEY_REUSED")
                return prior
            request_json = _compact_json(dict(request), ensure_ascii=False)
            now = self.clock()
            try:
                self.connection.execute("""INSERT INTO connector_commands
                    (account_id,command_id,generation,dispatch_sequence,idempotency_key,request_hash,
                     execution_epoch,command_type,request_json,state,phase,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,'RECEIVED','RECEIVED',?,?)""",
                    (self.account_id, command_id, generation, dispatch_sequence, idempotency_key,
                     request_hash, execution_epoch, command_type, request_json, now, now))
                self.connection.commit()
            except sqlite3.IntegrityError as exc:
                self.connection.rollback()
                raise IdempotencyConflict("IDEMPOTENCY_KEY_REUSED") from exc
            return self.get(command_id)  # type: ignore[return-value]

    def transition(self, command_id: str, *, state: str | None = None, phase: str | None = None,
                   mt5_invoked: bool | None = None, result: Mapping[str, Any] | None = None,
                   error_code: str | None = None, retcode: int | None = None,
                   external_order_id: str | None = None, external_deal_id: str | None = None,
                   external_position_ids: list[str] | None = None) -> JournalCommand:
        if state is not None and state not in STATES:
            raise JournalError("INVALID_STATE")
        with self._lock:
            current = self.get(command_id)
            if current is None:
                raise JournalError("COMMAND_NOT_FOUND")
            # A terminal command is immutable except for an idempotent replay.
            if current.state in {"ACCEPTED", "REJECTED"} and state not in (None, current.state):
                raise JournalError("TERMINAL_COMMAND")
            if current.state == "UNKNOWN" and state not in (None, "UNKNOWN", "ACCEPTED", "REJECTED"):
                raise JournalError("INVALID_TRANSITION")
            next_state = state or current.state
            now = self.clock()
            values = {
                "state": next_state, "phase": phase or current.phase,
                "mt5_invoked": int(current.mt5_invoked if mt5_invoked is None else mt5_invoked),
                "result_json": current.result_json if result is None else _compact_json(dict(result)),
                "error_code": error_code if error_code is not None else current.error_code,
                "retcode": retcode if retcode is not None else current.retcode,
                "external_order_id": external_order_id if external_order_id is not None else current.external_order_id,
                "external_deal_id": external_deal_id if external_deal_id is not None else current.external_deal_id,
                "external_position_ids_json": current.external_position_ids_json if external_position_ids is None else json.dumps(external_position_ids),
                "updated_at": now,
                "resolved_at": now if next_state in {"ACCEPTED", "REJECTED"} else current.resolved_at,
                "account_id": self.account_id,
                "command_id": command_id,
            }
            self.connection.execute("""UPDATE connector_commands SET state=:state,phase=:phase,mt5_invoked=:mt5_invoked,result_json=:result_json,
                error_code=:error_code,retcode=:retcode,external_order_id=:external_order_id,external_deal_id=:external_deal_id,
                external_position_ids_json=:external_position_ids_json,updated_at=:updated_at,resolved_at=:resolved_at
                WHERE account_id=:account_id AND command_id=:command_id""", values)
            self.connection.commit()
            return self.get(command_id)  # type: ignore[return-value]

    def recover(self) -> list[JournalCommand]:
        """Resolve in-flight pre-side-effect work and fence post-side-effect work."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT command_id,mt5_invoked FROM connector_commands "
                "WHERE account_id=? AND state IN ('RECEIVED','INVOKING')",
                (self.account_id,),
            ).fetchall()
            recovered = []
            for row in rows:
                if row["mt5_invoked"]:
                    recovered.append(self.transition(row["command_id"], state="UNKNOWN", phase="RECONCILING"))
                else:
                    recovered.append(self.transition(
                        row["command_id"], state="REJECTED", phase="RECOVERED",
                        error_code="NOT_INVOKED_AFTER_RESTART",
                    ))
            return recovered

    def record_observation(self, *, snapshot: Mapping[str, Any], source: str,
                           command_id: str | None = None, observed_at: str | None = None,
                           match_status: str | None = None) -> None:
        with self._lock:
            self.connection.execute("""INSERT OR IGNORE INTO connector_observations
                (account_id,command_id,observed_at,source,snapshot_json,match_status) VALUES (?,?,?,?,?,?)""",
                (self.account_id, command_id, observed_at or self.clock(), source,
                 _compact_json(dict(snapshot)), match_status))
            self.connection.commit()

    def has_unknown(self) -> bool:
        with self._lock:
            return self.connection.execute(
                "SELECT 1 FROM connector_commands WHERE account_id=? AND state='UNKNOWN' LIMIT 1",
                (self.account_id,),
            ).fetchone() is not None

    def unknown(self) -> list[JournalCommand]:
        with self._lock:
            return [self._row(row) for row in self.connection.execute(
                "SELECT * FROM connector_commands WHERE account_id=? AND state='UNKNOWN' ORDER BY dispatch_sequence", (self.account_id,)
            )]

    def resolve_unknown(self, command_id: str, *, snapshot: Mapping[str, Any], source: str,
                        match_status: str, state: str, result: Mapping[str, Any] | None = None,
                        error_code: str | None = None) -> JournalCommand:
        """Persist reconciliation evidence before resolving an UNKNOWN command."""
        if state not in {"ACCEPTED", "REJECTED", "UNKNOWN"}:
            raise JournalError("INVALID_RECONCILIATION_STATE")
        with self._lock:
            command = self.get(command_id)
            if command is None or command.state != "UNKNOWN":
                raise JournalError("UNKNOWN_COMMAND_NOT_FOUND")
            self.record_observation(snapshot=snapshot, source=source, command_id=command_id,
                                    match_status=match_status)
            return self.transition(command_id, state=state,
                                   phase="RESOLVED" if state != "UNKNOWN" else "RECONCILING",
                                   result=result, error_code=error_code)

    def compact(self, before: str, *, acknowledged_unknown: set[str] | None = None) -> int:
        """Delete old resolved rows and observations, never unresolved proof.

        ``acknowledged_unknown`` is retained for API compatibility; UNKNOWN rows
        are never eligible for this delete.
        """
        with self._lock:
            cur = self.connection.execute(
                "DELETE FROM connector_commands WHERE account_id=? "
                "AND state IN ('ACCEPTED','REJECTED') AND resolved_at < ?",
                (self.account_id, before),
            )
            self.connection.execute(
                "DELETE FROM connector_observations WHERE account_id=? AND observed_at < ? AND "
                "(command_id IS NULL OR command_id NOT IN (SELECT command_id FROM connector_commands WHERE account_id=? AND state='UNKNOWN'))",
                (self.account_id, before, self.account_id),
            )
            self.connection.commit()
            return cur.rowcount

    def close(self) -> None:
        self.connection.close()
