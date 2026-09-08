"""Account-scoped risk, calendar, and StrategyConfig activation gates.

The objects in this module are deliberately immutable.  Stores append versions
and activation evaluates a point-in-time account projection, so a later policy
change cannot rewrite the facts behind an earlier decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RiskLimits:
    broker_account_id: str
    version: int = 1
    max_risk_per_trade: Decimal = Decimal("0.005")
    daily_loss_limit: Decimal = Decimal("0.02")
    max_open_positions: int = 3
    max_total_open_risk: Decimal = Decimal("0.02")
    max_currency_exposure: Decimal = Decimal("0.01")
    max_spread_multiple: Decimal = Decimal("2")
    max_slippage_r: Decimal = Decimal("0.15")
    max_volatility_atr_multiple: Decimal = Decimal("2.5")
    baseline_window_sessions: int = 20
    baseline_minimum_samples: int = 20
    protection_confirmation_timeout_seconds: int = 30
    max_protection_repair_attempts: int = 3
    reason: str = ""


@dataclass(frozen=True)
class EnrichmentPolicy:
    strategy_config_id: str
    version: int = 1
    source_rules: dict[str, Literal["REQUIRED", "ADVISORY", "DISABLED"]] = field(default_factory=lambda: {"calendar": "REQUIRED"})
    freshness_ttl_seconds: dict[str, int] = field(default_factory=lambda: {"calendar": 3600})
    required_currencies: tuple[str, ...] = ()
    event_kinds: tuple[str, ...] = ()
    blackout_before_minutes: int | None = None
    blackout_after_minutes: int | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.source_rules.get("calendar") == "REQUIRED" and (
            self.blackout_before_minutes is None or self.blackout_after_minutes is None
        ):
            raise ValueError("required calendar policy needs explicit blackout windows")


@dataclass(frozen=True)
class CalendarHealth:
    currency: str
    provider: str
    observed_at: datetime
    healthy: bool = True
    covered: bool = True
    source_revision: str = ""
    clock_skew_seconds: int = 0

    def is_fresh(self, now: datetime | None = None, ttl_seconds: int = 3600) -> bool:
        current = now or _now()
        return self.healthy and self.covered and abs((current - self.observed_at).total_seconds()) <= ttl_seconds


@dataclass(frozen=True)
class EconomicEvent:
    currency: str
    event_kind: str
    scheduled_at: datetime
    blackout_start: datetime
    blackout_end: datetime
    source_revision: str
    official: bool = True

    def is_blackout(self, at: datetime) -> bool:
        return self.blackout_start <= at < self.blackout_end


@dataclass(frozen=True)
class ActivationDecision:
    allowed: bool
    reason_codes: tuple[str, ...] = ()
    fence: "SafetyFence | None" = None


@dataclass(frozen=True)
class SafetyFence:
    account_id: str
    sequence: int
    kind: str = "CALENDAR_FENCE"
    status: Literal["FENCE_PENDING", "ACKNOWLEDGED"] = "FENCE_PENDING"


class AccountSafety:
    """Per-account ordered safety state; shared calendar changes never cross it."""

    def __init__(self) -> None:
        self._sequence: dict[str, int] = {}
        self.exposure_gate: dict[str, str] = {}
        self.fences: dict[str, SafetyFence] = {}

    def install_fence(self, account_id: str, kind: str = "CALENDAR_FENCE") -> SafetyFence:
        sequence = self._sequence.get(account_id, 0) + 1
        self._sequence[account_id] = sequence
        fence = SafetyFence(account_id, sequence, kind)
        self.fences[account_id] = fence
        self.exposure_gate[account_id] = "FENCE_PENDING"
        return fence

    def acknowledge(self, account_id: str, sequence: int, *, blocked: bool = False) -> SafetyFence:
        current = self.fences.get(account_id)
        if current is None or current.sequence != sequence:
            raise ValueError("unknown safety fence")
        status = "FENCE_PENDING" if blocked else "ACKNOWLEDGED"
        self.exposure_gate[account_id] = "BLACKOUT" if blocked else "OPEN"
        updated = replace(current, status=status)
        self.fences[account_id] = updated
        return updated


class RiskLimitsStore:
    def __init__(self) -> None:
        self._versions: dict[str, list[RiskLimits]] = {}

    def create(self, limits: RiskLimits) -> RiskLimits:
        versions = self._versions.setdefault(limits.broker_account_id, [])
        if versions:
            limits = replace(limits, version=versions[-1].version + 1)
        versions.append(limits)
        return limits

    def active(self, account_id: str) -> RiskLimits | None:
        versions = self._versions.get(account_id, [])
        return versions[-1] if versions else None


@dataclass(frozen=True)
class RiskAssessment:
    broker_account_id: str
    risk_limits_version: int | None
    approved: bool
    reason_codes: tuple[str, ...] = ()
    valid_until: datetime | None = None


class RiskEngine:
    """Small deterministic gate used before a Signal can become executable."""

    def assess(
        self,
        account_id: str,
        limits: RiskLimits | None,
        *,
        baseline_samples: int = 0,
        daily_loss: Decimal = Decimal("0"),
        open_positions: int = 0,
        open_risk: Decimal = Decimal("0"),
        requested_risk: Decimal = Decimal("0"),
    ) -> RiskAssessment:
        reasons: list[str] = []
        if limits is None:
            reasons.append("RISK_LIMITS_MISSING")
        else:
            if baseline_samples < limits.baseline_minimum_samples:
                reasons.append("INSUFFICIENT_BASELINE_DATA")
            if daily_loss >= limits.daily_loss_limit:
                reasons.append("DAILY_LOSS_LIMIT_EXCEEDED")
            if open_positions >= limits.max_open_positions:
                reasons.append("MAX_OPEN_POSITIONS_EXCEEDED")
            if open_risk + requested_risk > limits.max_total_open_risk:
                reasons.append("TOTAL_OPEN_RISK_EXCEEDED")
            if requested_risk > limits.max_risk_per_trade:
                reasons.append("MAX_RISK_PER_TRADE_EXCEEDED")
        valid_until = _now() + timedelta(seconds=30) if not reasons else None
        return RiskAssessment(
            account_id,
            limits.version if limits else None,
            not reasons,
            tuple(reasons),
            valid_until,
        )


class ActivationGate:
    """Fail-closed activation validator for one account and config version."""

    def evaluate(
        self,
        *,
        account_id: str,
        pair: str,
        mapping: Any | None,
        connector_capabilities: dict[str, bool] | None,
        risk_limits: RiskLimits | None,
        policy: EnrichmentPolicy | None,
        calendar_health: dict[str, CalendarHealth],
        session_allowed: bool = True,
        wti_gate: str = "OPEN",
    ) -> ActivationDecision:
        reasons: list[str] = []
        if mapping is None or not getattr(mapping, "broker_symbol", None):
            reasons.append("PAIR_MAPPING_MISSING")
        connector_is_safe = bool(
            connector_capabilities
            and connector_capabilities.get("native_stop_loss")
            and connector_capabilities.get("native_take_profit")
        )
        if not connector_is_safe:
            reasons.append("CONNECTOR_CAPABILITY_UNSAFE")
        if risk_limits is None:
            reasons.append("RISK_LIMITS_MISSING")
        if policy is None:
            reasons.append("ENRICHMENT_POLICY_MISSING")
        else:
            for currency in policy.required_currencies:
                health = calendar_health.get(currency)
                ttl = policy.freshness_ttl_seconds.get("calendar", 3600)
                if health is None or not health.is_fresh(ttl_seconds=ttl):
                    if health is None or not health.covered:
                        reasons.append("CALENDAR_COVERAGE_MISSING")
                    else:
                        reasons.append("CALENDAR_HEALTH_STALE")
        if not session_allowed:
            reasons.append("SESSION_POLICY_UNSAFE")
        if pair == "WTI" and wti_gate != "OPEN":
            if wti_gate == "ROLL_GUARD":
                reasons.append("WTI_ROLL_GUARD_ACTIVE")
            elif wti_gate == "REOPEN_COOLDOWN":
                reasons.append("WTI_REOPEN_COOLDOWN_ACTIVE")
            else:
                reasons.append("WTI_ENTRY_GATE_CLOSED")
        return ActivationDecision(not reasons, tuple(dict.fromkeys(reasons)))
