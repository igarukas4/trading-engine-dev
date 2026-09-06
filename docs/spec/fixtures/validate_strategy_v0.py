#!/usr/bin/env python3
"""Validate the executable V0 strategy-template contract using only stdlib.

The JSON fixture is the structured authority for fixed V0 seeds.  This script
parses the normative tables and seed blocks rather than merely looking for a
few words, and checks the backend boundaries that make the templates safe.
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / "docs/spec/strategy-templates-v0.md"
BACKEND = ROOT / "docs/spec/backend-v0.md"
FIXTURE = Path(__file__).with_name("strategy-v0-cases.json")
SCALE_18 = Decimal("0.000000000000000001")


def fail(message: str) -> None:
    raise AssertionError(message)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def section(text: str, number: str, title: str) -> str:
    match = re.search(rf"^## {re.escape(number)}\. {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    require(match is not None, f"strategy-templates-v0.md: missing section {number}. {title}")
    return match.group(1)


def table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if not line.startswith("|") or re.fullmatch(r"[| :\-]+", line):
            continue
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def parse_decimal(value: str) -> Decimal:
    require(not isinstance(value, float), "fixture: binary floats are forbidden")
    return Decimal(str(value))


def canonical_parameter_hash(key: str, version: str, parameters: dict[str, Any]) -> str:
    # Fixture parameters intentionally use RFC 8785's JSON-safe integer/string
    # subset; sorted compact JSON is its canonical serialization for this data.
    payload = {"indicator_key": key, "implementation_version": version, "parameters": parameters}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def closed_bars(vector: dict[str, Any]) -> list[dict[str, Decimal]]:
    result = []
    for raw in vector["bars"]:
        require(raw["is_closed"] is True, f"indicator vector {raw['id']}: must be closed")
        bar = {name: parse_decimal(raw[name]) for name in ("open", "high", "low", "close")}
        require(bar["high"] >= max(bar["open"], bar["close"]), f"indicator vector {raw['id']}: invalid high")
        require(bar["low"] <= min(bar["open"], bar["close"]), f"indicator vector {raw['id']}: invalid low")
        result.append(bar)
    return result


def ema(bars: list[dict[str, Decimal]], period: int) -> Decimal:
    closes = [bar["close"] for bar in bars[1:]]
    require(len(closes) >= period, "EMA vector: insufficient observations")
    value = sum(closes[:period]) / Decimal(period)
    alpha = Decimal(2) / Decimal(period + 1)
    for close in closes[period:]:
        value = alpha * close + (Decimal(1) - alpha) * value
    return value


def true_ranges(bars: list[dict[str, Decimal]]) -> list[Decimal]:
    return [max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]), abs(cur["low"] - prev["close"])) for prev, cur in zip(bars, bars[1:])]


def atr(bars: list[dict[str, Decimal]], period: int) -> Decimal:
    values = true_ranges(bars)
    require(len(values) >= period, "ATR vector: insufficient observations")
    result = sum(values[:period]) / Decimal(period)
    for current in values[period:]:
        result = (Decimal(period - 1) * result + current) / Decimal(period)
    return result


def rsi(bars: list[dict[str, Decimal]], period: int) -> Decimal:
    closes = [bar["close"] for bar in bars]
    changes = [right - left for left, right in zip(closes, closes[1:])]
    require(len(changes) >= period, "RSI vector: insufficient observations")
    gains = [max(change, Decimal(0)) for change in changes]
    losses = [max(-change, Decimal(0)) for change in changes]
    average_gain = sum(gains[:period]) / Decimal(period)
    average_loss = sum(losses[:period]) / Decimal(period)
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = (Decimal(period - 1) * average_gain + gain) / Decimal(period)
        average_loss = (Decimal(period - 1) * average_loss + loss) / Decimal(period)
    if average_loss == 0:
        return Decimal(100) if average_gain > 0 else Decimal(50)
    return Decimal(100) - Decimal(100) / (Decimal(1) + average_gain / average_loss)


def adx(bars: list[dict[str, Decimal]], period: int) -> Decimal:
    observations: list[tuple[Decimal, Decimal, Decimal]] = []
    for previous, current in zip(bars, bars[1:]):
        up_move = current["high"] - previous["high"]
        down_move = previous["low"] - current["low"]
        plus_dm = up_move if up_move > down_move and up_move > 0 else Decimal(0)
        minus_dm = down_move if down_move > up_move and down_move > 0 else Decimal(0)
        tr = max(current["high"] - current["low"], abs(current["high"] - previous["close"]), abs(current["low"] - previous["close"]))
        observations.append((tr, plus_dm, minus_dm))
    require(len(observations) >= period * 2 - 1, "ADX vector: insufficient observations")
    smooth_tr = sum(item[0] for item in observations[:period])
    smooth_plus = sum(item[1] for item in observations[:period])
    smooth_minus = sum(item[2] for item in observations[:period])
    dxs: list[Decimal] = []
    for index in range(period - 1, len(observations)):
        if index >= period:
            tr, plus_dm, minus_dm = observations[index]
            smooth_tr = smooth_tr - smooth_tr / Decimal(period) + tr
            smooth_plus = smooth_plus - smooth_plus / Decimal(period) + plus_dm
            smooth_minus = smooth_minus - smooth_minus / Decimal(period) + minus_dm
        if smooth_tr == 0:
            dxs.append(Decimal(0))
            continue
        plus_di = Decimal(100) * smooth_plus / smooth_tr
        minus_di = Decimal(100) * smooth_minus / smooth_tr
        denominator = plus_di + minus_di
        dxs.append(Decimal(0) if denominator == 0 else Decimal(100) * abs(plus_di - minus_di) / denominator)
    result = sum(dxs[:period]) / Decimal(period)
    for dx in dxs[period:]:
        result = (Decimal(period - 1) * result + dx) / Decimal(period)
    return result


def verify_fixture_shape(contract: dict[str, Any]) -> None:
    templates = contract["templates"]
    expected = [
        ("xauusd-trend-pullback-v0", "XAUUSD", "TrendPullbackContinuationStrategy@0.1.0"),
        ("eurusd-trend-pullback-v0", "EURUSD", "TrendPullbackContinuationStrategy@0.1.0"),
        ("usdjpy-trend-pullback-v0", "USDJPY", "TrendPullbackContinuationStrategy@0.1.0"),
        ("wti-trend-breakout-v0", "WTI", "TrendFilteredBreakoutStrategy@0.1.0"),
    ]
    require([(item["key"], item["pair"], item["strategy"]) for item in templates] == expected, "fixture: exact V0 template-to-Pair/plugin mapping changed")
    require(contract["trigger_timeframe"] == "M15" and contract["required_timeframes"] == ["H4", "H1", "M15"], "fixture: V0 must require exactly H4/H1/M15 with M15 trigger")
    require(contract["default_activation_status"] == "DISABLED", "fixture: V0 templates must start disabled")
    require(contract["h4_trend"] == {"ema_fast_period": 34, "ema_slow_period": 150, "atr_period": 14, "adx_period": 14, "adx_min": "22", "ema_separation_atr": "0.10", "slope_bars": 5}, "fixture: H4 seed drift")
    require(contract["h1_pullback"] == {"ema_period": 34, "atr_period": 14, "rsi_period": 14, "pullback_window_bars": 2, "pullback_tolerance_atr": "0.50", "reclaim_level_rsi": "50", "min_body_fraction": "0.55"}, "fixture: H1 pullback seed drift")
    require(contract["trend_m15"] == {"channel_bars": 12, "atr_period": 14, "buffer_spread_multiple": "1.50", "min_body_fraction": "0.55", "max_extension_atr": "1.00"}, "fixture: common M15 seed drift")
    require(contract["exit_policy"] == {"tp1_r": "1.00", "tp1_fraction": "0.40", "tp2_r": "2.00", "tp2_fraction": "0.30", "runner_fraction_nominal": "0.30", "native_safety_tp_r": "4.00", "trailing": {"starts_after": "TP2_CONFIRMED", "timeframe": "M15", "atr_period": 14, "atr_multiple": "2.00", "update_on": "CLOSED_CANDLE"}}, "fixture: staged exit seed drift")
    require(sum(parse_decimal(contract["exit_policy"][name]) for name in ("tp1_fraction", "tp2_fraction", "runner_fraction_nominal")) == Decimal(1), "fixture: exit fractions must sum to one")
    wti = templates[-1]["wti"]
    require(wti["false_breakout_cooldown_m15_bars"] == 8 and wti["reopen_cooldown_minutes"] == 30 and wti["roll_guard_before_expiry_broker_trading_days"] == 3, "fixture: WTI cooldown/roll safety drift")
    boundary = contract["strategy_input_boundary"]
    require(boundary["market_state_account_scoped"] and boundary["strategy_is_pure_and_deterministic"] and boundary["forbidden_inputs"] == ["news", "llm", "mutable_account_state", "broker_calls"], "fixture: strategy input boundary drift")


def verify_spec(contract: dict[str, Any], spec: str) -> None:
    templates = table_rows(section(spec, "2", "V0 template set"))
    actual = [(row[0].strip("`"), row[1].strip("`"), row[2].strip("`"), row[3], row[4], row[5]) for row in templates[1:]]
    expected = [(item["key"], item["pair"], item["strategy"], contract["trigger_timeframe"], ", ".join(contract["required_timeframes"]), "disabled") for item in contract["templates"]]
    require(actual == expected, f"strategy-templates-v0.md: template table differs from fixture: {actual!r}")

    trend_section = section(spec, "4", "TrendPullbackContinuationStrategy")
    h4_block = re.search(r"The common seed for XAUUSD, EURUSD, and USDJPY is:\n\n```yaml\n(.*?)```", trend_section, re.S)
    require(h4_block is not None, "strategy-templates-v0.md: missing H4 seed block")
    h4_contract = contract["h4_trend"]
    expected_h4 = {
        "ema_fast": {"period": str(h4_contract["ema_fast_period"]), "price": "close"},
        "ema_slow": {"period": str(h4_contract["ema_slow_period"]), "price": "close"},
        "atr_h4": {"period": str(h4_contract["atr_period"])},
        "adx_h4": {"period": str(h4_contract["adx_period"])},
        "adx_min": h4_contract["adx_min"],
        "ema_separation_atr": h4_contract["ema_separation_atr"],
        "slope_bars": str(h4_contract["slope_bars"]),
    }
    for name, expected_value in expected_h4.items():
        if isinstance(expected_value, dict):
            block = re.search(rf"^{name}:\n((?:  [^\n]+\n?)+)", h4_block.group(1), re.M)
            require(block is not None, f"strategy-templates-v0.md: missing H4 {name}")
            parsed = dict(re.findall(r"^  ([a-z_]+):\s*(.+)$", block.group(1), re.M))
            require(parsed == expected_value, f"strategy-templates-v0.md: H4 {name} drift: {parsed!r}")
        else:
            require(re.search(rf"^{name}:\s*{re.escape(expected_value)}$", h4_block.group(1), re.M) is not None, f"strategy-templates-v0.md: H4 {name} drift")
    h1_block = re.search(r"Common seed:\n\n```yaml\n(.*?)```", trend_section, re.S)
    require(h1_block is not None, "strategy-templates-v0.md: missing H1 seed block")
    h1_contract = contract["h1_pullback"]
    expected_h1_blocks = {
        "ema_pullback": {"period": str(h1_contract["ema_period"]), "price": "close"},
        "atr_h1": {"period": str(h1_contract["atr_period"])},
        "rsi_h1": {"period": str(h1_contract["rsi_period"])},
    }
    for name, expected_value in expected_h1_blocks.items():
        block = re.search(rf"^{name}:\n((?:  [^\n]+\n?)+)", h1_block.group(1), re.M)
        require(block is not None, f"strategy-templates-v0.md: missing H1 {name}")
        parsed = dict(re.findall(r"^  ([a-z_]+):\s*(.+)$", block.group(1), re.M))
        require(parsed == expected_value, f"strategy-templates-v0.md: H1 {name} drift: {parsed!r}")
    for name, value in {"pullback_window_bars": h1_contract["pullback_window_bars"], "pullback_tolerance_atr": h1_contract["pullback_tolerance_atr"], "reclaim_level_rsi": h1_contract["reclaim_level_rsi"], "min_body_fraction": h1_contract["min_body_fraction"]}.items():
        value = str(value)
        require(re.search(rf"^{name}:\s*{re.escape(value)}$", h1_block.group(1), re.M) is not None, f"strategy-templates-v0.md: H1 {name} drift")

    trend_table = table_rows(section(spec, "4", "TrendPullbackContinuationStrategy"))
    parameter_rows = {row[0].strip("`"): row[1:] for row in trend_table if len(row) == 4 and row[0] != "Parameter"}
    trend_contract = contract["trend_m15"]
    trend_templates = contract["templates"][:3]
    expected_trend_m15 = {
        "channel_bars": [str(trend_contract["channel_bars"])] * 3,
        "atr_m15_period": [str(trend_contract["atr_period"])] * 3,
        "buffer_atr": [item["m15"]["buffer_atr"] for item in trend_templates],
        "buffer_spread_multiple": [trend_contract["buffer_spread_multiple"]] * 3,
        "min_body_fraction": [trend_contract["min_body_fraction"]] * 3,
        "max_extension_atr": [trend_contract["max_extension_atr"]] * 3,
        "signal_ttl_m15_bars": [str(item["m15"]["signal_ttl_m15_bars"]) for item in trend_templates],
    }
    require(parameter_rows == expected_trend_m15, f"strategy-templates-v0.md: trend M15 seed drift: {parameter_rows!r}")
    for item in trend_templates:
        require(
            f"- {item['pair']}: {item['m15']['ttl_minutes']} minutes" in trend_section,
            f"strategy-templates-v0.md: {item['pair']} Signal TTL drift",
        )

    wti_section = section(spec, "5", "TrendFilteredBreakoutStrategy for WTI")
    for key, value in contract["templates"][-1]["wti"].items():
        if key == "ttl_minutes":
            continue
        require(f"{key}: {value}" in wti_section, f"strategy-templates-v0.md: WTI seed drift for {key}")
    require(f"The Signal TTL is {contract['templates'][-1]['wti']['ttl_minutes']} minutes." in wti_section, "strategy-templates-v0.md: WTI TTL drift")
    for phrase in ("cannot be identified as WTI/US crude", "lacks native StopLoss and TakeProfit support", "unknown fixed-expiry/continuous-roll semantics", "PairEntryGateSnapshot", "blocked gate skips Strategy evaluation"):
        require(phrase in spec, f"strategy-templates-v0.md: missing WTI safeguard {phrase!r}")

    exit_section = section(spec, "6", "Entry protection and staged exit policy")
    for phrase in ("close 40% of initial filled volume", "close 30% of initial filled volume", "nominally 30%", "one native safety TakeProfit at `4.00R`", "starts only after TP2 Fill is confirmed"):
        require(phrase in exit_section, f"strategy-templates-v0.md: staged exit contract drift: {phrase!r}")
    lookback_rows = table_rows(section(spec, "8", "Minimum lookback"))
    actual_lookbacks = {row[0]: [int(value) for value in row[1:]] for row in lookback_rows[1:]}
    expected_lookbacks = {"XAUUSD trend-pullback": [450, 150, 60], "EURUSD trend-pullback": [450, 150, 60], "USDJPY trend-pullback": [450, 150, 60], "WTI trend-breakout": [450, 300, 500]}
    require(actual_lookbacks == expected_lookbacks, "strategy-templates-v0.md: exact minimum lookbacks drift")
    require("All persisted parameters and calculations use Decimal semantics." in spec and "Decimal precision 38 with `ROUND_HALF_EVEN`" in spec, "strategy-templates-v0.md: Decimal semantics drift")


def verify_backend(backend: str) -> None:
    # These are interface/data-boundary checks, not generic vocabulary checks.
    for fragment in (
        "def evaluate(\n        self,\n        market_state: MarketState,\n        config: StrategyConfig,\n    ) -> Opportunity | None",
        "Pure/deterministic for the supplied state and config.",
        "evaluation_key = strategy_config_version_id + pair_id + trigger_time",
        "News, LLM output, mutable account state, and broker calls are not available inside Strategy.",
        "MarketState` is the immutable account-scoped in-memory value",
        "Every reference must belong to the same BrokerAccount.",
        "activation_status`: `DISABLED | ACTIVE`",
        "minimum_score`, `signal_ttl`",
        "MT5-native SL/TP is mandatory on entry",
        "multiple Strategy target levels become durable reduce-only PositionCommand children",
        "Python `Decimal`; no binary float in domain calculations.",
        "indicator_definition_id`, `parameter_hash`",
    ):
        require(fragment in backend, f"backend-v0.md: required strategy interface contract drift: {fragment!r}")


def verify_vectors(vector: dict[str, Any]) -> None:
    require(vector["precision"] == 38 and vector["rounding"] == "ROUND_HALF_EVEN" and vector["persisted_scale"] == 18, "fixture: Decimal vector context drift")
    bars = closed_bars(vector)
    calculators = {"ema": ema, "atr": atr, "rsi": rsi, "adx": adx}
    with localcontext() as context:
        context.prec = vector["precision"]
        context.rounding = ROUND_HALF_EVEN
        for expected in vector["expected_terminal_values"]:
            key = expected["indicator_key"]
            require(key in calculators, f"fixture: unknown indicator {key}")
            require(expected["indicator_definition_id"] == f"indicator-definition-{key}-v1", f"fixture: {key} definition identity drift")
            require(canonical_parameter_hash(key, expected["implementation_version"], expected["parameters"]) == expected["parameter_hash"], f"fixture: {key} parameter hash is not canonical")
            actual = calculators[key](bars, int(expected["parameters"]["period"])).quantize(SCALE_18, rounding=ROUND_HALF_EVEN)
            require(actual == parse_decimal(expected["value"]), f"fixture: {key} vector expected {expected['value']}, got {actual}")


def main() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    require(data["schema_version"] == 1, "fixture: unsupported schema version")
    contract = data["template_contract"]
    verify_fixture_shape(contract)
    verify_spec(contract, SPEC.read_text(encoding="utf-8"))
    verify_backend(BACKEND.read_text(encoding="utf-8"))
    verify_vectors(data["indicator_vectors"])
    print("strategy-v0 template contract validation: PASS")


if __name__ == "__main__":
    main()
