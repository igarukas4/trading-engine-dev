"""Immutable, explainable Signal lifecycle and deterministic risk assessment."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal
from uuid import uuid4

from .risk_calendar import RiskAssessment, RiskEngine, RiskLimits


SignalStatus = Literal[
    "BLOCKED_RISK",
    "PENDING_APPROVAL",
    "ELIGIBLE",
    "APPROVED",
    "INVALIDATED",
    "EXPIRED",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return deepcopy(value)


@dataclass(frozen=True)
class Signal:
    id: str
    account_id: str
    opportunity: Any
    market_snapshot_id: str
    strategy_config_version_id: str
    policy_version: int
    revision: int
    created_at: datetime
    expires_at: datetime
    entry_zone: Any
    stop_loss: Decimal
    take_profit: tuple[Decimal, ...]
    risk_assessment: RiskAssessment
    status: SignalStatus
    reason_codes: tuple[str, ...]
    supersedes_signal_id: str | None = None
    risk_context: Any = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.account_id != self.opportunity.get("account_id"):
            raise ValueError("ACCOUNT_CONTEXT_MISMATCH")
        if self.strategy_config_version_id != self.opportunity.get("strategy_config_version_id"):
            raise ValueError("CONFIG_CONTEXT_MISMATCH")

    def as_dict(self) -> dict[str, Any]:
        assessment = self.risk_assessment
        status = self.status
        reasons = self.reason_codes
        if status in {"ELIGIBLE", "PENDING_APPROVAL", "APPROVED"} and _now() >= self.expires_at:
            status = "EXPIRED"
            reasons = tuple(dict.fromkeys((*reasons, "SIGNAL_EXPIRED")))
        return {
            "id": self.id,
            "account_id": self.account_id,
            "opportunity": _thaw(self.opportunity),
            "market_snapshot_id": self.market_snapshot_id,
            "strategy_config_version_id": self.strategy_config_version_id,
            "policy_version": self.policy_version,
            "revision": self.revision,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "entry_zone": _thaw(self.entry_zone),
            "stop_loss": str(self.stop_loss),
            "take_profit": [str(value) for value in self.take_profit],
            "status": status,
            "reason_codes": list(reasons),
            "supersedes_signal_id": self.supersedes_signal_id,
            "risk_assessment": {
                "approved": assessment.approved,
                "purpose": assessment.purpose,
                "risk_limits_version": assessment.risk_limits_version,
                "reason_codes": list(assessment.reason_codes),
                "valid_until": assessment.valid_until.isoformat() if assessment.valid_until else None,
                "evidence": dict(assessment.evidence),
            },
        }


class SignalStore:
    def __init__(self, risk_engine: RiskEngine | None = None) -> None:
        self.risk_engine = risk_engine or RiskEngine()
        self.signals: dict[str, Signal] = {}
        self._latest_by_opportunity: dict[str, str] = {}

    def create(
        self, *, account_id: str, opportunity: dict[str, Any], market_snapshot_id: str,
        policy_version: int, created_at: datetime | None = None, ttl: timedelta = timedelta(minutes=30),
        entry_zone: dict[str, Any] | None = None, stop_loss: Decimal | str = "0",
        take_profit: tuple[Decimal | str, ...] = (), limits: RiskLimits | None = None,
        risk_kwargs: dict[str, Any] | None = None,
    ) -> Signal:
        if opportunity.get("account_id") != account_id:
            raise ValueError("ACCOUNT_CONTEXT_MISMATCH")
        if opportunity.get("strategy_config_account_id", account_id) != account_id:
            raise ValueError("CONFIG_CONTEXT_MISMATCH")
        config_id = str(opportunity.get("strategy_config_version_id", ""))
        if config_id.startswith("config-") and "-v" in config_id:
            encoded_account = config_id[len("config-"):].rsplit("-v", 1)[0]
            if encoded_account.startswith("account-") and encoded_account != account_id:
                raise ValueError("CONFIG_CONTEXT_MISMATCH")
        if opportunity.get("market_snapshot_account_id", account_id) != account_id:
            raise ValueError("MARKET_SNAPSHOT_CONTEXT_MISMATCH")
        expected_snapshot = opportunity.get("market_snapshot_id")
        if expected_snapshot is not None and str(expected_snapshot) != market_snapshot_id:
            raise ValueError("MARKET_SNAPSHOT_CONTEXT_MISMATCH")
        if limits is not None and limits.broker_account_id != account_id:
            raise ValueError("RISK_LIMITS_ACCOUNT_MISMATCH")
        opportunity = _freeze(deepcopy(opportunity))
        at = created_at or _now()
        expires_at = at + ttl
        assessment = self.risk_engine.assess(
            account_id,
            limits,
            now=at,
            signal_expires_at=expires_at,
            **(risk_kwargs or {}),
        )
        signal = Signal(
            id=f"signal-{uuid4()}",
            account_id=account_id,
            opportunity=opportunity,
            market_snapshot_id=market_snapshot_id,
            strategy_config_version_id=str(opportunity["strategy_config_version_id"]),
            policy_version=policy_version,
            revision=1,
            created_at=at,
            expires_at=expires_at,
            entry_zone=_freeze(deepcopy(entry_zone or {})),
            stop_loss=Decimal(str(stop_loss)),
            take_profit=tuple(Decimal(str(item)) for item in take_profit),
            risk_assessment=assessment,
            status="ELIGIBLE" if assessment.approved else "BLOCKED_RISK",
            reason_codes=assessment.reason_codes,
            risk_context=_freeze(deepcopy(risk_kwargs or {})),
        )
        self.signals[signal.id] = signal
        self._latest_by_opportunity[str(opportunity.get("id") or opportunity.get("evaluation_key"))] = signal.id
        return signal

    def get(self, signal_id: str) -> Signal:
        return self.signals[signal_id]

    def approve(self, signal_id: str, *, account_id: str, revision: int) -> Signal:
        signal = self.get(signal_id)
        if signal.account_id != account_id:
            raise ValueError("ACCOUNT_CONTEXT_MISMATCH")
        if signal.revision != revision:
            raise ValueError("SIGNAL_REVISION_CHANGED")
        view = signal.as_dict()
        if view["status"] == "APPROVED":
            return signal
        if view["status"] != "ELIGIBLE":
            raise ValueError("SIGNAL_NOT_ELIGIBLE")
        approved = replace(signal, status="APPROVED")
        self.signals[signal_id] = approved
        return approved

    def create_revision(self, signal_id: str, *, policy_version: int, created_at: datetime | None = None,
                        limits: RiskLimits | None = None) -> Signal:
        prior = self.get(signal_id)
        invalidated_reasons = tuple(
            dict.fromkeys((*prior.reason_codes, "SIGNAL_REVISION_SUPERSEDED"))
        )
        invalidated = replace(
            prior,
            status="INVALIDATED",
            reason_codes=invalidated_reasons,
        )
        self.signals[signal_id] = invalidated
        created = self.create(
            account_id=prior.account_id,
            opportunity=_thaw(prior.opportunity),
            market_snapshot_id=prior.market_snapshot_id,
            policy_version=policy_version,
            created_at=created_at,
            ttl=prior.expires_at - prior.created_at,
            entry_zone=_thaw(prior.entry_zone),
            stop_loss=prior.stop_loss,
            take_profit=prior.take_profit,
            limits=limits,
            risk_kwargs=_thaw(prior.risk_context),
        )
        revised = replace(
            created,
            revision=prior.revision + 1,
            supersedes_signal_id=signal_id,
            risk_assessment=replace(created.risk_assessment, signal_revision=prior.revision + 1),
        )
        self.signals[revised.id] = revised
        return revised
