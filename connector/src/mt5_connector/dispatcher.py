"""Serialized, journal-first dispatch of MT5 side effects."""
from __future__ import annotations

import threading
from typing import Any, Mapping

from .journal import IdempotencyConflict, SQLiteJournal, canonical_request_hash
from .models import SIDE_EFFECTING_TYPES


class DispatchError(RuntimeError):
    pass


class Dispatcher:
    def __init__(self, journal: SQLiteJournal, adapter: Any, *, account_id: str | None = None,
                 generation: int | None = None, execution_epoch: int | None = None):
        self.journal = journal
        self.adapter = adapter
        self.account_id = account_id or journal.account_id
        self.generation = generation
        self.execution_epoch = execution_epoch
        self._lock = threading.RLock()
        # Recovery is part of construction: no caller can accidentally dispatch
        # while an old ambiguity boundary is still unresolved.
        self.journal.recover()
        self.blocked = bool(journal.unknown())
        row = journal.connection.execute(
            "SELECT MAX(dispatch_sequence) FROM connector_commands WHERE account_id=?",
            (self.account_id,),
        ).fetchone()
        self._last_sequence = int(row[0] or 0)

    def recover(self) -> list[Any]:
        with self._lock:
            rows = self.journal.recover()
            self.blocked = bool(self.journal.unknown())
            return rows

    def dispatch(self, command: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch one command, returning its durable result.

        ``command`` accepts either the WSS envelope or a compact internal mapping.
        No adapter call occurs before the RECEIVED commit.
        """
        with self._lock:
            command_id = str(command.get("command_id") or "")
            key = str(command.get("idempotency_key") or "")
            typ = str(command.get("type") or command.get("command_type") or "")
            payload = command.get("payload", {})
            if command.get("account_id", self.account_id) != self.account_id:
                return self._local_result(command_id, key, "WRONG_ACCOUNT")
            if not command_id or not key or not isinstance(payload, Mapping):
                return self._local_result(command_id, key, "MALFORMED_COMMAND")
            try:
                generation = command.get("generation", 0)
                epoch = command.get("execution_epoch", 0)
                if isinstance(generation, bool) or not isinstance(generation, int):
                    raise ValueError
                if isinstance(epoch, bool) or not isinstance(epoch, int):
                    raise ValueError
                sequence = command.get("dispatch_sequence", command.get("sequence", 0))
                if isinstance(sequence, bool) or not isinstance(sequence, int):
                    raise ValueError
            except (TypeError, ValueError):
                return self._local_result(command_id, key, "MALFORMED_COMMAND")
            existing = self.journal.get(command_id) or self.journal.get_by_idempotency(key)
            if self.generation is not None and generation != self.generation:
                return self._local_result(command_id, key, "STALE_GENERATION")
            if self.execution_epoch is not None and epoch != self.execution_epoch:
                return self._local_result(command_id, key, "STALE_EPOCH")
            if not existing and (sequence < 0 or (sequence and self._last_sequence and sequence != self._last_sequence + 1)):
                return self._local_result(command_id, key, "OUT_OF_ORDER_DISPATCH")
            canonical_hash = canonical_request_hash(typ, payload)
            supplied_hash = command.get("request_hash")
            if supplied_hash is not None and supplied_hash != canonical_hash:
                return self._local_result(command_id, key, "REQUEST_HASH_MISMATCH")
            request_hash = canonical_hash
            try:
                record = self.journal.receive(
                    command_id=command_id, generation=generation,
                    dispatch_sequence=sequence,
                    idempotency_key=key, request_hash=request_hash,
                    execution_epoch=epoch, command_type=typ,
                    request=payload,
                )
            except IdempotencyConflict:
                return self._local_result(command_id, key, "IDEMPOTENCY_KEY_REUSED")
            if not existing:
                self._last_sequence = max(self._last_sequence, sequence)
            if record.state in {"ACCEPTED", "REJECTED"}:
                return record.result or {"state": record.state, "code": record.error_code}
            if record.state == "UNKNOWN":
                self.blocked = True
                return {"state": "UNKNOWN", "code": record.error_code or "RECONCILIATION_REQUIRED"}
            if typ not in SIDE_EFFECTING_TYPES:
                return self._finish(command_id, "REJECTED", "UNSUPPORTED_DISPATCH_TYPE")
            if self.blocked or self.journal.unknown():
                self.blocked = True
                return self._finish(command_id, "REJECTED", "ACCOUNT_FENCED_UNKNOWN")
            error = self._validate(typ, payload)
            if error:
                return self._finish(command_id, "REJECTED", error)
            try:
                self.journal.transition(command_id, state="INVOKING", phase="CHECKING")
                checked = self._check(typ, payload)
            except Exception:
                return self._finish(command_id, "REJECTED", "ADAPTER_CHECK_ERROR")
            if not self._check_passed(checked):
                return self._finish(command_id, "REJECTED", "ORDER_CHECK_REJECTED", checked)
            self.journal.transition(command_id, phase="CHECKED")
            # This commit is the ambiguity boundary. Never move it below invoke().
            self.journal.transition(command_id, state="INVOKING", phase="DISPATCHING", mt5_invoked=True)
            try:
                result = self._invoke(typ, payload)
            except Exception:
                return self._finish(command_id, "UNKNOWN", "TRANSPORT_AMBIGUOUS")
            if result is None:
                return self._finish(command_id, "UNKNOWN", "TRANSPORT_AMBIGUOUS")
            state = self._result_state(result)
            if state == "UNKNOWN":
                self.blocked = True
                return self._finish(command_id, "UNKNOWN", "TRANSPORT_AMBIGUOUS", result)
            return self._finish(command_id, state, None if state == "ACCEPTED" else "BROKER_REJECTED", result)

    def reconcile(self, command_id: str, *, snapshot: Mapping[str, Any], source: str,
                  match_status: str, state: str, result: Mapping[str, Any] | None = None,
                  error_code: str | None = None) -> dict[str, Any]:
        """Resolve an ambiguity only after durable broker evidence is recorded."""
        with self._lock:
            if match_status not in {"UNIQUE_MATCH", "NO_EFFECT", "CONFLICT", "INCOMPLETE"}:
                return {"state": "UNKNOWN", "code": "INVALID_RECONCILIATION_EVIDENCE"}
            if match_status in {"CONFLICT", "INCOMPLETE"}:
                state = "UNKNOWN"
            row = self.journal.resolve_unknown(
                command_id, snapshot=snapshot, source=source,
                match_status=match_status, state=state,
                result=result, error_code=error_code,
            )
            self.blocked = bool(self.journal.unknown())
            return row.result or {"state": row.state, "code": row.error_code}

    def _check(self, typ, payload):
        if not hasattr(self.adapter, "order_check"):
            raise DispatchError("adapter does not implement order_check")
        return self.adapter.order_check(typ, dict(payload))

    def _invoke(self, typ, payload):
        if hasattr(self.adapter, "invoke"):
            return self.adapter.invoke(typ, dict(payload))
        method = {"order.submit_market": "submit_market", "position.modify_protection": "modify_protection", "position.close": "close"}.get(typ)
        if method and hasattr(self.adapter, method):
            return getattr(self.adapter, method)(dict(payload))
        raise DispatchError("adapter does not implement side effect")

    @staticmethod
    def _check_passed(result) -> bool:
        if result is True or result is None:
            return result is True
        if isinstance(result, Mapping):
            if "retcode" in result:
                return result.get("retcode") in (0, "0")
            return result.get("ok") is True
        return getattr(result, "retcode", None) in (0, "0")

    @staticmethod
    def _result_state(result) -> str:
        if not isinstance(result, Mapping):
            state = getattr(result, "state", None)
            retcode = getattr(result, "retcode", None)
        else:
            state, retcode = result.get("state"), result.get("retcode")
        if str(state).upper() in {"UNKNOWN", "AMBIGUOUS"}:
            return "UNKNOWN"
        if str(state).upper() in {"ACCEPTED", "REJECTED"}:
            return str(state).upper()
        if retcode in (10012, "10012"):
            return "UNKNOWN"
        if retcode in (10008, 10009, 10010, "10008", "10009", "10010"):
            return "ACCEPTED"
        if retcode is not None:
            return "REJECTED"
        return "UNKNOWN"

    def _finish(self, command_id, state, code, result=None):
        result_payload = dict(result) if isinstance(result, Mapping) else ({"value": result} if result is not None else {})
        result_payload.update({"state": state, "code": code})
        self.journal.transition(command_id, state=state, phase="RESOLVED" if state != "UNKNOWN" else "RECONCILING",
                                result=result_payload, error_code=code,
                                retcode=result.get("retcode") if isinstance(result, Mapping) else None,
                                external_order_id=str(result["external_order_id"]) if isinstance(result, Mapping) and result.get("external_order_id") is not None else None,
                                external_deal_id=str(result["external_deal_id"]) if isinstance(result, Mapping) and result.get("external_deal_id") is not None else None,
                                external_position_ids=[str(x) for x in result.get("position_tickets", [])] if isinstance(result, Mapping) else None)
        if state == "UNKNOWN":
            self.blocked = True
        return result_payload

    def _local_result(self, command_id, key, code):
        return {"state": "REJECTED", "code": code, "command_id": command_id, "idempotency_key": key}

    def _validate(self, typ, payload):
        if typ == "order.submit_market" and (not payload.get("sl") or not payload.get("tp")):
            return "NATIVE_PROTECTION_REQUIRED"
        if typ == "position.close" and (not payload.get("position_ticket") or not payload.get("volume")):
            return "POSITION_DETAILS_REQUIRED"
        if typ == "position.modify_protection" and not payload.get("position_ticket"):
            return "POSITION_DETAILS_REQUIRED"
        return None
