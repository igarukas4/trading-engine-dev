"""Account Discovery Pairing state and its account-scoped security rules."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from psycopg import connect
from psycopg.types.json import Jsonb

from .broker_accounts import AccountError, AccountRegistry, BrokerAccount


PairingStatus = Literal[
    "OPEN", "CANDIDATE_SUBMITTED", "CONFIRMED", "CANCELLED", "EXPIRED"
]


class PairingError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CandidateReport:
    provider: str
    broker_server: str
    external_account_id: str
    environment: Literal["DEMO", "LIVE"]
    facts: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all((self.provider, self.broker_server, self.external_account_id)):
            raise PairingError("INVALID_CANDIDATE", "candidate identity is incomplete")
        if self.environment not in ("DEMO", "LIVE"):
            raise PairingError("INVALID_CANDIDATE", "candidate environment is invalid")
        if not isinstance(self.facts, dict):
            raise PairingError("INVALID_CANDIDATE", "candidate facts must be an object")

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "broker_server": self.broker_server,
            "external_account_id": self.external_account_id,
            "environment": self.environment,
            "facts": dict(self.facts),
        }


@dataclass(frozen=True)
class PairingStart:
    session_id: str
    custodian_session_id: str
    device_code: str
    expires_at: datetime


@dataclass(frozen=True)
class PairingConfirmation:
    session_id: str
    account: BrokerAccount
    key_id: str
    connector_secret: str


@dataclass
class _PairingSession:
    session_id: str
    custodian_session_id: str
    code_salt: str
    code_hash: str
    created_at: datetime
    expires_at: datetime
    status: PairingStatus = "OPEN"
    candidate: CandidateReport | None = None
    connector_session_id: str | None = None
    failed_attempts: int = 0
    account_id: str | None = None
    key_id: str | None = None
    connector_secret: str | None = None
    key_delivered: bool = False


class PairingRegistry:
    """Small public interface for discovery, confirmation, and key delivery.

    Device codes and connector secrets exist only at their one-time delivery
    points. The optional PostgreSQL adapter stores only a salted code hash and
    the candidate report, never either secret.
    """

    def __init__(
        self,
        accounts: AccountRegistry,
        database_url: str = "",
        *,
        lifetime: timedelta = timedelta(minutes=10),
        max_failed_attempts: int = 5,
    ) -> None:
        self.accounts = accounts
        self.database_url = database_url
        self.lifetime = lifetime
        self.max_failed_attempts = max_failed_attempts
        self._sessions: dict[str, _PairingSession] = {}
        self._lock = RLock()
        if database_url:
            self._load()

    def start(self, custodian_session_id: str, *, now: datetime | None = None) -> PairingStart:
        if not custodian_session_id:
            raise PairingError("CUSTODIAN_SESSION_REQUIRED", "custodian session is required")
        created_at = now or _utcnow()
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(10))
        salt = secrets.token_hex(16)
        session = _PairingSession(
            session_id=str(uuid4()),
            custodian_session_id=custodian_session_id,
            code_salt=salt,
            code_hash=self._hash_code(code, salt),
            created_at=created_at,
            expires_at=created_at + self.lifetime,
        )
        with self._lock:
            self._sessions[session.session_id] = session
            self._persist(session)
        return PairingStart(session.session_id, custodian_session_id, code, session.expires_at)

    def view(
        self,
        session_id: str,
        custodian_session_id: str | None = None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self._get(session_id)
            if custodian_session_id is not None:
                self._assert_custodian(session, custodian_session_id)
            if self._expire_if_needed(session, now or _utcnow()):
                self._persist(session)
            return self._view(session)

    def submit_candidate(
        self,
        device_code: str,
        connector_session_id: str,
        candidate: CandidateReport,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not connector_session_id:
            raise PairingError("CONNECTOR_SESSION_REQUIRED", "connector session is required")
        current = now or _utcnow()
        with self._lock:
            session = self._find_by_code(device_code, current)
            if session.status != "OPEN":
                raise PairingError("PAIRING_CODE_USED", "device code has already been used")
            session.status = "CANDIDATE_SUBMITTED"
            session.candidate = candidate
            session.connector_session_id = connector_session_id
            self._persist(session)
            return self._view(session)

    def cancel(
        self,
        session_id: str,
        custodian_session_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self._get(session_id)
            self._assert_custodian(session, custodian_session_id)
            self._expire_if_needed(session, now or _utcnow())
            if session.status in ("CONFIRMED", "CANCELLED", "EXPIRED"):
                raise PairingError("PAIRING_NOT_OPEN", "pairing session is no longer open")
            session.status = "CANCELLED"
            self._persist(session)
            return self._view(session)

    def confirm(
        self,
        session_id: str,
        custodian_session_id: str,
        *,
        display_name: str,
        now: datetime | None = None,
    ) -> PairingConfirmation:
        with self._lock:
            session = self._get(session_id)
            self._assert_custodian(session, custodian_session_id)
            self._expire_if_needed(session, now or _utcnow())
            if session.status != "CANDIDATE_SUBMITTED" or session.candidate is None:
                raise PairingError("CANDIDATE_REQUIRED", "a candidate must be submitted before confirmation")
            if not display_name:
                raise PairingError("DISPLAY_NAME_REQUIRED", "display name is required")

            candidate = session.candidate
            connector_secret = secrets.token_urlsafe(48)
            try:
                account, key_id = self.accounts.create_bound_account(
                    provider=candidate.provider,
                    broker_server=candidate.broker_server,
                    external_account_id=candidate.external_account_id,
                    display_name=display_name,
                    environment=candidate.environment,
                    secret=connector_secret,
                )
            except AccountError as error:
                raise PairingError(error.code, str(error)) from error
            session.status = "CONFIRMED"
            session.account_id = account.id
            session.key_id = key_id
            session.connector_secret = connector_secret
            self._persist(session)
            return PairingConfirmation(session.session_id, account, key_id, connector_secret)

    def consume_key(self, session_id: str, connector_session_id: str) -> PairingConfirmation:
        with self._lock:
            session = self._get(session_id)
            if session.status != "CONFIRMED" or session.account_id is None:
                raise PairingError("PAIRING_NOT_CONFIRMED", "pairing is not confirmed")
            if session.connector_session_id != connector_session_id:
                raise PairingError("WRONG_CONNECTOR_SESSION", "connector session does not own pairing")
            if session.key_delivered or not session.connector_secret or not session.key_id:
                raise PairingError("KEY_ALREADY_DELIVERED", "connector key has already been delivered")
            account = self.accounts.accounts[session.account_id]
            connector_secret = session.connector_secret
            key_id = session.key_id
            session.key_delivered = True
            session.connector_secret = None
            self._persist(session)
            return PairingConfirmation(session.session_id, account, key_id, connector_secret)

    def _find_by_code(self, device_code: str, now: datetime) -> _PairingSession:
        for session in self._sessions.values():
            if hmac.compare_digest(
                session.code_hash, self._hash_code(device_code, session.code_salt)
            ):
                if self._expire_if_needed(session, now):
                    self._persist(session)
                    raise PairingError("PAIRING_EXPIRED", "device code has expired")
                if session.status != "OPEN":
                    raise PairingError("PAIRING_CODE_USED", "device code has already been used")
                return session
        for session in self._sessions.values():
            if session.status == "OPEN":
                session.failed_attempts += 1
                if session.failed_attempts >= self.max_failed_attempts:
                    session.status = "EXPIRED"
                    self._persist(session)
                    raise PairingError("PAIRING_ATTEMPTS_EXCEEDED", "too many invalid device codes")
                self._persist(session)
        raise PairingError("INVALID_DEVICE_CODE", "device code is invalid")

    def _get(self, session_id: str) -> _PairingSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise PairingError("PAIRING_NOT_FOUND", "pairing session not found")
        return session

    @staticmethod
    def _assert_custodian(session: _PairingSession, custodian_session_id: str) -> None:
        if session.custodian_session_id != custodian_session_id:
            raise PairingError("WRONG_CUSTODIAN_SESSION", "custodian session does not own pairing")

    @staticmethod
    def _expire_if_needed(session: _PairingSession, now: datetime) -> bool:
        if now >= session.expires_at and session.status in ("OPEN", "CANDIDATE_SUBMITTED"):
            session.status = "EXPIRED"
            return True
        return False

    def _view(self, session: _PairingSession) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "status": session.status,
            "created_at": session.created_at.isoformat(),
            "expires_at": session.expires_at.isoformat(),
            "candidate": session.candidate.as_dict() if session.candidate else None,
            "connector_session_id": session.connector_session_id,
            "account_id": session.account_id,
            "key_id": session.key_id,
            "key_delivered": session.key_delivered,
        }

    def _hash_code(self, code: str, salt: str) -> str:
        return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()

    def _persist(self, session: _PairingSession) -> None:
        if not self.database_url:
            return
        with connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO pairing_sessions
                        (id, custodian_session_id, code_salt, code_hash, status,
                        candidate, connector_session_id, failed_attempts, expires_at,
                        account_id, key_id, key_delivered, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status,
                         candidate = EXCLUDED.candidate,
                         connector_session_id = EXCLUDED.connector_session_id,
                         failed_attempts = EXCLUDED.failed_attempts,
                         account_id = EXCLUDED.account_id, key_id = EXCLUDED.key_id,
                         key_delivered = EXCLUDED.key_delivered""",
                    (
                        session.session_id,
                        session.custodian_session_id,
                        session.code_salt,
                        session.code_hash,
                        session.status,
                        Jsonb(session.candidate.as_dict()) if session.candidate else None,
                        session.connector_session_id,
                        session.failed_attempts,
                        session.expires_at,
                        session.account_id,
                        session.key_id,
                        session.key_delivered,
                        session.created_at,
                    ),
                )

    def _load(self) -> None:
        with connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT id, custodian_session_id, code_salt, code_hash, status,
                              candidate, connector_session_id, failed_attempts, expires_at,
                              account_id, key_id, key_delivered, created_at
                         FROM pairing_sessions
                        WHERE status IN ('OPEN', 'CANDIDATE_SUBMITTED')"""
                )
                for row in cursor.fetchall():
                    candidate_payload = row[5]
                    candidate = CandidateReport(**candidate_payload) if candidate_payload else None
                    session = _PairingSession(
                        session_id=str(row[0]), custodian_session_id=row[1],
                        code_salt=row[2], code_hash=row[3], status=row[4],
                        candidate=candidate, connector_session_id=row[6],
                        failed_attempts=row[7], expires_at=row[8],
                        created_at=row[12], account_id=str(row[9]) if row[9] else None,
                        key_id=row[10], key_delivered=row[11],
                    )
                    self._sessions[session.session_id] = session
