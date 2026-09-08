"""Immutable, explainable Signal lifecycle and deterministic risk assessment."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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


@dataclass(frozen=True)
class Signal:
    id: str
    account_id: str
    opportunity: dict[str, Any]
    market_snapshot_id: str
    strategy_config_version_id: str
    policy_version: int
    revision: int
    created_at: datetime
    expires_at: datetime
    entry_zone: dict[str, Any]
    stop_loss: Decimal
    take_profit: tuple[Decimal, ...]
    risk_assessment: RiskAssessment
    status: SignalStatus
    reason_codes: tuple[str, ...]
    supersedes_signal_id: str | None = None

    def __post_init__(self) -> None:
        if self.account_id != self.opportunity.get("account_id"):
            raise ValueError("ACCOUNT_CONTEXT_MISMATCH")
        if self.strategy_config_version_id != self.opportunity.get("strategy_config_version_id"):
            raise ValueError("CONFIG_CONTEXT_MISMATCH")

    def as_dict(self) -> dict[str, Any]:
        assessment = self.risk_assessment
        return {
            "id": self.id,
            "account_id": self.account_id,
            "opportunity": dict(self.opportunity),
            "market_snapshot_id": self.market_snapshot_id,
            "strategy_config_version_id": self.strategy_config_version_id,
            "policy_version": self.policy_version,
            "revision": self.revision,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "entry_zone": dict(self.entry_zone),
            "stop_loss": str(self.stop_loss),
            "take_profit": [str(value) for value in self.take_profit],
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "supersedes_signal_id": self.supersedes_signal_id,
            "risk_assessment": {
                "approved": assessment.approved,
                "purpose": assessment.purpose,
                "risk_limits_version": assessment.risk_limits_version,
                "reason_codes": list(assessment.reason_codes),
                "valid_until": assessment.valid_until.isoformat() if assessment.valid_until else None,
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
            opportunity=dict(opportunity),
            market_snapshot_id=market_snapshot_id,
            strategy_config_version_id=str(opportunity["strategy_config_version_id"]),
            policy_version=policy_version,
            revision=1,
            created_at=at,
            expires_at=expires_at,
            entry_zone=dict(entry_zone or {}),
            stop_loss=Decimal(str(stop_loss)),
            take_profit=tuple(Decimal(str(item)) for item in take_profit),
            risk_assessment=assessment,
            status="ELIGIBLE" if assessment.approved else "BLOCKED_RISK",
            reason_codes=assessment.reason_codes,
        )
        self.signals[signal.id] = signal
        self._latest_by_opportunity[str(opportunity.get("id") or opportunity.get("evaluation_key"))] = signal.id
        return signal

    def get(self, signal_id: str) -> Signal:
        return self.signals[signal_id]

    def create_revision(self, signal_id: str, *, policy_version: int, created_at: datetime | None = None) -> Signal:
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
            opportunity=prior.opportunity,
            market_snapshot_id=prior.market_snapshot_id,
            policy_version=policy_version,
            created_at=created_at,
            ttl=prior.expires_at - prior.created_at,
            entry_zone=prior.entry_zone,
            stop_loss=prior.stop_loss,
            take_profit=prior.take_profit,
        )
        revised = replace(created, revision=prior.revision + 1, supersedes_signal_id=signal_id)
        self.signals[revised.id] = revised
        return revised
