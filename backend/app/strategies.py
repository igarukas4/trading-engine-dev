"""Pure, deterministic V0 strategy evaluation.

The evaluator accepts a complete account-scoped MarketStateSnapshot projection
and immutable StrategyConfig version.  It has no broker, news, LLM, or mutable
account-state dependency.  The small profile adapter is intentional: it lets
the contract fixtures provide terminal indicator values without pretending
that this slice owns indicator calculation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class StrategyConfig:
    id: str
    account_id: str
    template_key: str
    pair: str
    strategy: str
    version: int = 1
    activation_status: Literal["DISABLED", "ACTIVE"] = "DISABLED"


@dataclass(frozen=True)
class Opportunity:
    account_id: str
    pair: str
    strategy_config_version_id: str
    direction: Direction
    confidence: Decimal
    reason_codes: tuple[str, ...]
    evaluation_key: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "pair": self.pair,
            "strategy_config_version_id": self.strategy_config_version_id,
            "direction": self.direction,
            "confidence": str(self.confidence),
            "reason_codes": list(self.reason_codes),
            "evaluation_key": dict(self.evaluation_key),
        }


@dataclass(frozen=True)
class EvaluationResult:
    opportunity: Opportunity | None
    reason_codes: tuple[str, ...]
    audit_action: Literal["OPPORTUNITY", "NO_OPPORTUNITY", "SKIP_EVALUATION", "REJECT_GRAPH"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.audit_action,
            "reason_codes": list(self.reason_codes),
            "opportunity": self.opportunity.as_dict() if self.opportunity else None,
        }


@dataclass(frozen=True)
class CanonicalTemplate:
    key: str
    pair: str
    strategy: str
    lookback: dict[str, int]


TEMPLATES = (
    CanonicalTemplate(
        "xauusd-trend-pullback-v0",
        "XAUUSD",
        "TrendPullbackContinuationStrategy@0.1.0",
        {"H4": 450, "H1": 150, "M15": 60},
    ),
    CanonicalTemplate(
        "usdjpy-trend-pullback-v0",
        "USDJPY",
        "TrendPullbackContinuationStrategy@0.1.0",
        {"H4": 450, "H1": 150, "M15": 60},
    ),
    CanonicalTemplate(
        "eurusd-snd-ao-qm-bidirectional-v0",
        "EURUSD",
        "SupplyDemandAOQMStrategy@0.1.0",
        {"H4": 200, "M30": 60},
    ),
    CanonicalTemplate(
        "wti-trend-breakout-v0",
        "WTI",
        "TrendFilteredBreakoutStrategy@0.1.0",
        {"H4": 450, "H1": 300, "M15": 500},
    ),
)


def canonical_configs(account_id: str) -> tuple[StrategyConfig, ...]:
    """Materialize independent disabled config versions for one account."""
    return tuple(
        StrategyConfig(
            id=f"config-{account_id}-{template.key}-v1",
            account_id=account_id,
            template_key=template.key,
            pair=template.pair,
            strategy=template.strategy,
        )
        for template in TEMPLATES
    )


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _get(snapshot: Any, key: str, default: Any = None) -> Any:
    if isinstance(snapshot, dict):
        return snapshot.get(key, default)
    return getattr(snapshot, key, default)


def _profile(snapshot: Any, timeframe: str) -> dict[str, Any]:
    value = _get(snapshot, timeframe.lower(), {})
    return value if isinstance(value, dict) else {}


def _lookback_for_pair(pair: str) -> dict[str, int]:
    return next(template.lookback for template in TEMPLATES if template.pair == pair)


def _result(
    snapshot: Any,
    config: StrategyConfig,
    direction: Direction,
    confidence: str,
    reasons: tuple[str, ...],
) -> EvaluationResult:
    trigger = str(_get(snapshot, "trigger_time", ""))
    opportunity = Opportunity(
        account_id=config.account_id,
        pair=config.pair,
        strategy_config_version_id=config.id,
        direction=direction,
        confidence=_d(confidence),
        reason_codes=reasons,
        evaluation_key={
            "strategy_config_version_id": config.id,
            "pair_id": config.pair,
            "trigger_time": trigger,
        },
    )
    return EvaluationResult(opportunity, reasons, "OPPORTUNITY")


def _failure(
    action: Literal["NO_OPPORTUNITY", "SKIP_EVALUATION", "REJECT_GRAPH"],
    reasons: tuple[str, ...],
) -> EvaluationResult:
    return EvaluationResult(None, reasons, action)


def _validate(snapshot: Any, config: StrategyConfig) -> EvaluationResult | None:
    if _get(snapshot, "account_id") != config.account_id or _get(snapshot, "pair") != config.pair:
        return _failure("REJECT_GRAPH", ("ACCOUNT_CONTEXT_MISMATCH",))
    required_lookback = _lookback_for_pair(config.pair)
    candles = _get(snapshot, "candles", ())
    if candles:
        by_timeframe: dict[str, list[Any]] = {}
        for candle in candles:
            timeframe = _get(candle, "timeframe")
            by_timeframe.setdefault(timeframe, []).append(candle)
            if _get(candle, "account_id") != config.account_id or _get(candle, "pair") != config.pair:
                return _failure("REJECT_GRAPH", ("ACCOUNT_CONTEXT_MISMATCH",))
            if not _get(candle, "is_closed", True):
                return _failure("SKIP_EVALUATION", ("OPEN_CANDLE_INPUT",))
        if len({str(_get(candle, "source_revision", "")) for candle in candles}) > 1:
            return _failure("SKIP_EVALUATION", ("REVISION_MISMATCH",))
        for timeframe, minimum in required_lookback.items():
            if len(by_timeframe.get(timeframe, ())) < minimum:
                return _failure("SKIP_EVALUATION", ("INSUFFICIENT_LOOKBACK",))
    if _get(snapshot, "completeness", "COMPLETE") != "COMPLETE":
        reasons = tuple(_get(snapshot, "reasons", ())) or ("INCOMPLETE_MARKET_DATA",)
        return _failure("SKIP_EVALUATION", reasons)
    for timeframe, minimum in required_lookback.items():
        available = _get(snapshot, "available_closed_bars", {}).get(timeframe, minimum)
        if int(available) < minimum:
            return _failure("SKIP_EVALUATION", ("INSUFFICIENT_LOOKBACK",))
    if _get(snapshot, "force_conflict", False):
        return _failure("NO_OPPORTUNITY", ("CONFLICTING_SETUPS",))
    return None


def _trend(snapshot: Any, config: StrategyConfig) -> EvaluationResult:
    h4 = _profile(snapshot, "H4")
    h1 = _profile(snapshot, "H1")
    m15 = _profile(snapshot, "M15")
    h4_close = _d(h4.get("close", 0))
    h4_ema150 = _d(h4.get("ema150", 0))
    direction: Direction = "SHORT" if h4_close < h4_ema150 else "LONG"
    sign = Decimal("-1") if direction == "SHORT" else Decimal("1")
    if not sign * (h4_close - h4_ema150) > 0:
        return _failure("NO_OPPORTUNITY", ("TREND_PRICE_FILTER_FAILED",))
    h4_ema34 = _d(h4.get("ema34", 0))
    ema_separation = sign * (h4_ema34 - h4_ema150)
    if not ema_separation > 0:
        return _failure("NO_OPPORTUNITY", ("EMA_ORDER_FAILED",))
    if not ema_separation >= _d("0.10") * _d(h4.get("atr", 0)):
        return _failure("NO_OPPORTUNITY", ("EMA_SEPARATION_FAILED",))
    if not sign * (h4_ema150 - _d(h4.get("ema150_previous", 0))) > 0:
        return _failure("NO_OPPORTUNITY", ("EMA_SLOPE_FAILED",))
    if _d(h4.get("adx", 0)) < _d("22"):
        return _failure("NO_OPPORTUNITY", ("ADX_FILTER_FAILED",))
    tolerance = _d("0.50") * _d(h1.get("atr", 0))
    touch = _d(h1.get("touch", h1.get("low" if direction == "LONG" else "high", 0)))
    if not sign * (touch - _d(h1.get("ema34", 0))) <= tolerance:
        return _failure("NO_OPPORTUNITY", ("PULLBACK_NOT_TOUCHED",))
    invalidation = _d(h1.get("invalidation_close", h1.get("close", 0)))
    if not sign * (invalidation - _d(h1.get("ema34", 0))) > -tolerance:
        return _failure("NO_OPPORTUNITY", ("PULLBACK_INVALIDATED",))
    close, open_, high, low = map(
        _d,
        (
            h1.get("latest_close", 0),
            h1.get("latest_open", 0),
            h1.get("high", 0),
            h1.get("low", 0),
        ),
    )
    body = Decimal(0) if high == low else abs(close - open_) / (high - low)
    if not sign * (close - _d(h1.get("ema34", 0))) > 0 or not sign * (close - open_) > 0 or body < _d("0.55"):
        return _failure("NO_OPPORTUNITY", ("H1_BODY_FILTER_FAILED",))
    if not sign * (_d(h1.get("rsi_latest", 0)) - _d("50")) >= 0 or not sign * (_d(h1.get("rsi_previous", 0)) - _d("50")) < 0:
        return _failure("NO_OPPORTUNITY", ("RSI_RECLAIM_FAILED",))
    boundary = _d(m15.get("channel_high" if direction == "LONG" else "channel_low", 0))
    trigger_buffer = max(_d("0.10") * _d(m15.get("atr", 0)), _d("1.50") * _d(m15.get("spread", 0)))
    mclose, mopen, mhigh, mlow = map(
        _d,
        (
            m15.get("close", 0),
            m15.get("open", 0),
            m15.get("high", 0),
            m15.get("low", 0),
        ),
    )
    extension = sign * (mclose - boundary)
    if not extension > trigger_buffer:
        return _failure("NO_OPPORTUNITY", ("M15_BREAKOUT_NOT_CLOSED",))
    body = Decimal(0) if mhigh == mlow else abs(mclose - mopen) / (mhigh - mlow)
    if not sign * (mclose - mopen) > 0 or body < _d("0.55"):
        return _failure("NO_OPPORTUNITY", ("M15_BODY_FILTER_FAILED",))
    if extension > _d("1.00") * _d(m15.get("atr", 0)):
        return _failure("NO_OPPORTUNITY", ("TRIGGER_OVEREXTENDED",))
    return _result(snapshot, config, direction, "0.70", ("H4_TREND_CONFIRMED", "H1_PULLBACK_RECLAIMED", "M15_CONTINUATION_CONFIRMED"))


def _wti(snapshot: Any, config: StrategyConfig) -> EvaluationResult:
    gate = _get(snapshot, "gate", "OPEN")
    gate_reasons = {
        "ROLL_GUARD": "WTI_ROLL_GUARD_ACTIVE",
        "REOPEN_COOLDOWN": "WTI_REOPEN_COOLDOWN_ACTIVE",
        "FALSE_BREAKOUT_COOLDOWN": "WTI_COOLDOWN_ACTIVE",
    }
    if gate in gate_reasons:
        return _failure("SKIP_EVALUATION", (gate_reasons[gate],))
    h4 = _profile(snapshot, "H4")
    h1 = _profile(snapshot, "H1")
    m15 = _profile(snapshot, "M15")
    h4_close = _d(h4.get("close", 0))
    h4_ema150 = _d(h4.get("ema150", 0))
    h4_ema34 = _d(h4.get("ema34", 0))
    direction: Direction = "SHORT" if h4_close < h4_ema150 else "LONG"
    sign = Decimal("-1") if direction == "SHORT" else Decimal("1")
    trend_filter_passed = (
        sign * (h4_close - h4_ema150) > 0
        and sign * (h4_ema34 - h4_ema150) > 0
        and sign * (h4_ema34 - h4_ema150) >= _d("0.10") * _d(h4.get("atr", 0))
        and sign * (h4_ema150 - _d(h4.get("ema150_previous", 0))) > 0
        and _d(h4.get("adx", 0)) >= _d("22")
    )
    if not trend_filter_passed:
        return _failure("NO_OPPORTUNITY", ("H4_TREND_FILTER_FAILED",))
    high, low = _d(h1.get("range_high", 0)), _d(h1.get("range_low", 0))
    if not _d("1.00") * _d(h1.get("atr14", 0)) <= high - low <= _d("3.00") * _d(h1.get("atr14", 0)):
        return _failure("NO_OPPORTUNITY", ("WTI_RANGE_WIDTH_FAILED",))
    if _d(h1.get("atr6", 0)) / _d(h1.get("atr30", 1)) > _d("0.65"):
        return _failure("NO_OPPORTUNITY", ("WTI_COMPRESSION_FAILED",))
    boundary = high if direction == "LONG" else low
    extension = sign * (_d(m15.get("close", 0)) - boundary)
    if not extension > _d("0.10") * _d(m15.get("atr", 0)):
        return _failure("NO_OPPORTUNITY", ("WTI_BREAKOUT_NOT_CLOSED",))
    if not sign * (_d(m15.get("close", 0)) - _d(m15.get("open", 0))) >= _d("0.50") * _d(m15.get("atr", 0)):
        return _failure("NO_OPPORTUNITY", ("WTI_BODY_FILTER_FAILED",))
    if extension > _d("1.25") * _d(m15.get("atr", 0)):
        return _failure("NO_OPPORTUNITY", ("TRIGGER_OVEREXTENDED",))
    return _result(
        snapshot,
        config,
        direction,
        "0.68",
        ("H4_TREND_CONFIRMED", "H1_COMPRESSION_CONFIRMED", "M15_RANGE_BREAK_CONFIRMED"),
    )


def _eurusd(snapshot: Any, config: StrategyConfig) -> EvaluationResult:
    h4, m30 = _profile(snapshot, "H4"), _profile(snapshot, "M30")
    if h4.get("regime") not in {"BULLISH_IMPULSIVE", "BULLISH_PULLBACK", "BEARISH_IMPULSIVE", "BEARISH_PULLBACK"}:
        return _failure("NO_OPPORTUNITY", ("H4_REGIME_INELIGIBLE",))
    zone = h4.get("zone", {})
    if zone.get("status") != "VALID":
        return _failure("NO_OPPORTUNITY", ("H4_ZONE_INVALID",))
    if int(zone.get("visit_count", 0)) != 0:
        return _failure("NO_OPPORTUNITY", ("H4_ZONE_NOT_FRESH",))
    direction: Direction = "LONG" if zone.get("kind") == "DEMAND" else "SHORT"
    head, a, b, br = m30.get("c", {}), m30.get("a", {}), m30.get("b", {}), m30.get("break", {})
    atr = _d(m30.get("atr14", 0))
    if abs(_d(head.get("pivot_price", 0)) - _d(a.get("pivot_price", 0))) < _d("0.30") * atr:
        return _failure("NO_OPPORTUNITY", ("M30_AO_PRICE_DIFFERENCE_TOO_SMALL",))
    if abs(_d(m30.get("ao", {}).get("second_pivot_value", 0)) - _d(m30.get("ao", {}).get("first_pivot_value", 0))) < _d("0.30") * atr:
        return _failure("NO_OPPORTUNITY", ("M30_AO_DIFFERENCE_TOO_SMALL",))
    if direction == "SHORT":
        if not _d(head.get("pivot_price", 0)) <= _d(zone.get("high", 0)) or not _d(br.get("close", 0)) < _d(b.get("pivot_price", 0)) - _d("0.10") * atr:
            return _failure("NO_OPPORTUNITY", ("M30_STRUCTURE_BREAK_NOT_CONFIRMED",))
    else:
        if not _d(head.get("pivot_price", 0)) >= _d(zone.get("low", 0)) or not _d(br.get("close", 0)) > _d(b.get("pivot_price", 0)) + _d("0.10") * atr:
            return _failure("NO_OPPORTUNITY", ("M30_STRUCTURE_BREAK_NOT_CONFIRMED",))
    return _result(snapshot, config, direction, "0.72", ("H4_REGIME_CONFIRMED", "H4_ZONE_VALID", "M30_AO_DIVERGENCE", "M30_QM_CONFIRMED", "RETEST_LIMIT_ARMED"))


def evaluate_snapshot(snapshot: Any, config: StrategyConfig) -> EvaluationResult:
    """Evaluate one complete snapshot; disabled configs remain inspectable."""
    invalid = _validate(snapshot, config)
    if invalid:
        return invalid
    if config.strategy.startswith("TrendPullback"):
        return _trend(snapshot, config)
    if config.strategy.startswith("TrendFilteredBreakout"):
        return _wti(snapshot, config)
    if config.strategy.startswith("SupplyDemandAOQM"):
        return _eurusd(snapshot, config)
    return _failure("NO_OPPORTUNITY", ("UNSUPPORTED_STRATEGY",))


def evaluate(snapshot: Any, config: StrategyConfig) -> Opportunity | None:
    """Backend Strategy interface: return only a candidate or no trade."""
    return evaluate_snapshot(snapshot, config).opportunity
