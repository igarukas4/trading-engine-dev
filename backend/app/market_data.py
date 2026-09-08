"""Account-owned market data primitives for the read-only Markets slice.

Only closed M1 candles are canonical input.  Quotes are deliberately kept as
ephemeral telemetry and never participate in MarketState completeness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from .broker_accounts import AccountError, assert_account_scope

Timeframe = Literal["M1", "M5", "M15", "H1", "H4", "D1"]
TIMEFRAME_MINUTES: dict[Timeframe, int] = {"M1": 1, "M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}


@dataclass(frozen=True)
class Candle:
    account_id: str
    pair: str
    timeframe: Timeframe
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: int = 0
    real_volume: int = 0
    spread: Decimal = Decimal("0")
    source_revision: str = "canonical-r1"
    is_closed: bool = True


@dataclass(frozen=True)
class PairMapping:
    account_id: str
    canonical_code: str
    broker_symbol: str
    base_currency: str = ""
    quote_currency: str = ""


@dataclass(frozen=True)
class IndicatorValue:
    account_id: str
    pair: str
    timeframe: Timeframe
    candle_open_time: datetime
    value_name: str
    value_numeric: Decimal
    source_revision: str


@dataclass(frozen=True)
class QuoteTelemetry:
    account_id: str
    pair: str
    bid: Decimal
    ask: Decimal
    observed_at: datetime
    non_canonical: bool = True


@dataclass(frozen=True)
class MarketStateSnapshot:
    account_id: str
    pair: str
    trigger_timeframe: Timeframe
    trigger_time: datetime
    candles: tuple[Candle, ...]
    indicators: tuple[IndicatorValue, ...]
    completeness: str
    reasons: tuple[str, ...] = ()
    snapshot_id: str = ""


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class MarketDataStore:
    """In-memory account partitions used by the foundation and contract tests."""

    def __init__(self) -> None:
        self.candles: dict[str, list[Candle]] = {}
        self.pairs: dict[str, dict[str, PairMapping]] = {}
        self.indicators: dict[str, list[IndicatorValue]] = {}
        self.quotes: dict[str, list[QuoteTelemetry]] = {}
        self.resyncing: set[str] = set()
        self._snapshot_number = 0

    def _check(self, account_id: str, record_account_id: str) -> None:
        assert_account_scope(account_id, record_account_id)

    def ingest_candle(self, account_id: str, candle: Candle) -> Candle:
        self._check(account_id, candle.account_id)
        if candle.timeframe != "M1":
            raise AccountError("INCOMPLETE_MARKET_DATA", "only closed M1 is canonical input")
        if not candle.is_closed:
            raise AccountError("OPEN_CANDLE_INPUT", "open candles cannot enter canonical data")
        self.candles.setdefault(account_id, []).append(candle)
        return candle

    def register_pair(self, account_id: str, mapping: PairMapping) -> PairMapping:
        self._check(account_id, mapping.account_id)
        account_pairs = self.pairs.setdefault(account_id, {})
        existing = account_pairs.get(mapping.canonical_code)
        if existing and existing.broker_symbol != mapping.broker_symbol:
            raise AccountError("ACCOUNT_CONTEXT_MISMATCH", "canonical Pair has a conflicting mapping")
        account_pairs[mapping.canonical_code] = mapping
        return mapping

    def ingest_quote(self, account_id: str, quote: QuoteTelemetry) -> QuoteTelemetry:
        self._check(account_id, quote.account_id)
        self.quotes.setdefault(account_id, []).append(quote)
        return quote

    def aggregate(self, account_id: str, pair: str, timeframe: Timeframe) -> list[Candle]:
        source = [c for c in self.candles.get(account_id, []) if c.pair == pair and c.timeframe == "M1"]
        size = TIMEFRAME_MINUTES[timeframe]
        if timeframe == "M1":
            return source
        source.sort(key=lambda c: c.open_time)
        result: list[Candle] = []
        for index in range(0, len(source), size):
            group = source[index:index + size]
            if len(group) != size or any(group[n].open_time != group[0].open_time + timedelta(minutes=n) for n in range(len(group))):
                continue
            revisions = {c.source_revision for c in group}
            if len(revisions) != 1:
                continue
            first, last = group[0], group[-1]
            result.append(Candle(account_id, pair, timeframe, first.open_time, last.close_time,
                                 first.open, max(c.high for c in group), min(c.low for c in group),
                                 last.close, sum(c.tick_volume for c in group), sum(c.real_volume for c in group),
                                 max(c.spread for c in group), first.source_revision, True))
        return result

    def build_snapshot(self, account_id: str, pair: str, trigger_timeframe: Timeframe = "M15", lookback: int = 1) -> MarketStateSnapshot:
        frames = ["M5", "M15", "H1", "H4"]
        candles = tuple(c for frame in frames for c in self.aggregate(account_id, pair, frame)[-lookback:])
        reasons: list[str] = []
        if not candles:
            reasons.append("INSUFFICIENT_LOOKBACK")
        if any(not c.is_closed for c in candles):
            reasons.append("OPEN_CANDLE_INPUT")
        revisions = {c.source_revision for c in candles}
        if len(revisions) > 1:
            reasons.append("REVISION_MISMATCH")
        for frame in frames:
            selected = [c for c in candles if c.timeframe == frame]
            if selected and any(b.open_time - a.open_time != timedelta(minutes=TIMEFRAME_MINUTES[frame]) for a, b in zip(selected, selected[1:])):
                reasons.append("GAP_DETECTED")
        indicators = tuple(self.compute_indicators(account_id, pair, candles))
        self._snapshot_number += 1
        complete = not reasons and all(c.timeframe == trigger_timeframe or c.close_time <= max(x.close_time for x in candles) for c in candles)
        if not complete and not reasons:
            reasons.append("INCOMPLETE_MARKET_DATA")
        return MarketStateSnapshot(account_id, pair, trigger_timeframe, max(c.close_time for c in candles) if candles else datetime.now(timezone.utc), candles, indicators, "COMPLETE" if complete else "INCOMPLETE", tuple(dict.fromkeys(reasons)), f"market-state-{self._snapshot_number}")

    def compute_indicators(self, account_id: str, pair: str, candles: tuple[Candle, ...]) -> list[IndicatorValue]:
        """Compute deterministic EMA(1) values without borrowing another account's bars."""
        values: list[IndicatorValue] = []
        for candle in candles:
            self._check(account_id, candle.account_id)
            values.append(IndicatorValue(account_id, pair, candle.timeframe, candle.open_time, "EMA1", candle.close, candle.source_revision))
        self.indicators.setdefault(account_id, []).extend(values)
        return values

    def resync(self, account_id: str, stream: str) -> dict[str, Any]:
        self.resyncing.add(f"{account_id}:{stream}")
        return {"account_id": account_id, "stream": stream, "status": "RESYNCING"}


market_data = MarketDataStore()
