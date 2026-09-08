"""Account-isolated dashboard stream and audit projections.

The reference implementation keeps the projection in memory so the API and
frontend contracts can be exercised without a running PostgreSQL deployment.
The identifiers and cursor rules mirror the durable stream contract: sequence
numbers are meaningful only inside one account stream or the system stream.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from threading import RLock
from typing import Any
from uuid import uuid4

ACCOUNT_STREAM = "account"
SYSTEM_STREAM = "system"
VALID_STREAMS = {ACCOUNT_STREAM, SYSTEM_STREAM}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DashboardHub:
    def __init__(self, replay_limit: int = 512) -> None:
        self.replay_limit = replay_limit
        self._events: dict[tuple[str, str | None], deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=replay_limit)
        )
        self._sequences: dict[tuple[str, str | None], int] = defaultdict(int)
        self._seen_event_ids: set[str] = set()
        self._lock = RLock()

    def publish(
        self,
        stream: str,
        account_id: str | None,
        event_type: str,
        payload: dict[str, Any],
        *,
        aggregate_id: str | None = None,
        aggregate_version: int = 1,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        if stream not in VALID_STREAMS:
            raise ValueError("stream must be account or system")
        if stream == ACCOUNT_STREAM and not account_id:
            raise ValueError("account stream requires broker_account_id")
        if stream == SYSTEM_STREAM and account_id is not None:
            raise ValueError("system stream cannot name an account")
        with self._lock:
            key = (stream, account_id)
            self._sequences[key] += 1
            event = {
                "event_id": str(uuid4()),
                "stream": stream,
                "stream_sequence": self._sequences[key],
                "type": event_type,
                "occurred_at": _now(),
                "broker_account_id": account_id,
                "aggregate_id": aggregate_id,
                "aggregate_version": aggregate_version,
                "correlation_id": correlation_id or str(uuid4()),
                "payload": payload,
            }
            self._events[key].append(event)
            return event

    def connect(
        self,
        cursors: dict[str, Any],
        *,
        account_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Build a stream-local replay; invalid cursors only affect that stream."""
        account_cursors = cursors.get("account_cursors", {})
        system_cursor = cursors.get("system_cursor", 0)
        events: list[dict[str, Any]] = []
        with self._lock:
            events.extend(self._replay(SYSTEM_STREAM, None, system_cursor))
            for account_id, cursor in account_cursors.items():
                if account_ids is not None and account_id not in account_ids:
                    events.append(
                        self._snapshot_required(
                            ACCOUNT_STREAM, account_id, "UNAUTHORIZED_CURSOR"
                        )
                    )
                else:
                    events.extend(self._replay(ACCOUNT_STREAM, account_id, cursor))

        events.sort(
            key=lambda event: (
                event["stream"] != SYSTEM_STREAM,
                event.get("stream_sequence", 0),
            )
        )
        return {"events": events, "connected_at": _now()}

    def _replay(
        self, stream: str, account_id: str | None, cursor: Any
    ) -> list[dict[str, Any]]:
        key = (stream, account_id)
        try:
            requested = int(cursor)
        except (TypeError, ValueError):
            return [self._snapshot_required(stream, account_id, "MALFORMED_CURSOR")]
        events = list(self._events[key])
        latest = self._sequences[key]
        earliest = events[0]["stream_sequence"] if events else latest + 1
        if requested > latest or (events and requested < earliest - 1):
            return [self._snapshot_required(stream, account_id, "CURSOR_GAP")]
        replay = [event for event in events if event["stream_sequence"] > requested]
        replay.append(
            {
                "type": "replay.complete",
                "stream": stream,
                "broker_account_id": account_id,
                "stream_sequence": latest,
            }
        )
        return replay

    @staticmethod
    def _snapshot_required(
        stream: str, account_id: str | None, reason: str
    ) -> dict[str, Any]:
        return {
            "type": "snapshot.required",
            "stream": stream,
            "broker_account_id": account_id,
            "reason": reason,
        }

    def watermark(self, stream: str, account_id: str | None = None) -> int:
        """Return the latest sequence for one validated stream."""
        if stream not in VALID_STREAMS:
            raise ValueError("stream must be account or system")
        if stream == ACCOUNT_STREAM and not account_id:
            raise ValueError("account stream requires broker_account_id")
        if stream == SYSTEM_STREAM and account_id is not None:
            raise ValueError("system stream cannot name an account")
        with self._lock:
            return self._sequences[(stream, account_id)]

    def apply_event(self, event: dict[str, Any]) -> bool:
        event_id = event.get("event_id")
        if not event_id:
            return True
        with self._lock:
            if event_id in self._seen_event_ids:
                return False
            self._seen_event_ids.add(event_id)
            return True


class AuditHub:
    def __init__(self) -> None:
        self.events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._lock = RLock()

    def record(self, account_id: str, event_type: str, reason: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "id": str(uuid4()),
            "broker_account_id": account_id,
            "event_type": event_type,
            "reason": reason,
            "payload": payload or {},
            "created_at": _now(),
        }
        with self._lock:
            self.events[account_id].append(event)
        return event

    def list(self, account_id: str, limit: int = 50, cursor: int = 0) -> dict[str, Any]:
        with self._lock:
            events = list(reversed(self.events[account_id]))
        page = events[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < len(events) else None
        return {
            "account_id": account_id,
            "audit_events": page,
            "next_cursor": str(next_cursor) if next_cursor is not None else None,
            "has_more": next_cursor is not None,
        }


dashboard_hub = DashboardHub()
audit_hub = AuditHub()
