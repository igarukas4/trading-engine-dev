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
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / "docs/spec/strategy-templates-v0.md"
BACKEND = ROOT / "docs/spec/backend-v0.md"
FIXTURE = Path(__file__).with_name("strategy-v0-cases.json")
QM_AO_FIXTURE = Path(__file__).with_name("strategy-eurusd-snd-ao-qm-cases.json")
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
        ("usdjpy-trend-pullback-v0", "USDJPY", "TrendPullbackContinuationStrategy@0.1.0"),
        ("eurusd-snd-ao-qm-bidirectional-v0", "EURUSD", "SupplyDemandAOQMStrategy@0.1.0"),
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
    expected = []
    for item in contract["templates"]:
        if item["pair"] == "EURUSD":
            expected.append((item["key"], item["pair"], item["strategy"], "M30", "H4, M30", "disabled"))
        else:
            expected.append((item["key"], item["pair"], item["strategy"], contract["trigger_timeframe"], ", ".join(contract["required_timeframes"]), "disabled"))
    require(actual == expected, f"strategy-templates-v0.md: template table differs from fixture: {actual!r}")

    trend_section = section(spec, "4", "TrendPullbackContinuationStrategy")
    h4_block = re.search(r"The common seed for XAUUSD and USDJPY \(the pairs still served by this plugin after the EURUSD override\) is:\n\n```yaml\n(.*?)```", trend_section, re.S)
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
    parameter_rows = {row[0].strip("`"): row[1:] for row in trend_table if len(row) == 3 and row[0] != "Parameter"}
    trend_contract = contract["trend_m15"]
    trend_templates = [item for item in contract["templates"] if item["strategy"] == "TrendPullbackContinuationStrategy@0.1.0"]
    expected_trend_m15 = {
        "channel_bars": [str(trend_contract["channel_bars"])] * 2,
        "atr_m15_period": [str(trend_contract["atr_period"])] * 2,
        "buffer_atr": [item["m15"]["buffer_atr"] for item in trend_templates],
        "buffer_spread_multiple": [trend_contract["buffer_spread_multiple"]] * 2,
        "min_body_fraction": [trend_contract["min_body_fraction"]] * 2,
        "max_extension_atr": [trend_contract["max_extension_atr"]] * 2,
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
    expected_lookbacks = {"XAUUSD trend-pullback": [450, 150, 60], "USDJPY trend-pullback": [450, 150, 60], "WTI trend-breakout": [450, 300, 500]}
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


SUCCESS_TREND = ["H4_TREND_CONFIRMED", "H1_PULLBACK_RECLAIMED", "M15_CONTINUATION_CONFIRMED"]
SUCCESS_WTI = ["H4_TREND_CONFIRMED", "H1_COMPRESSION_CONFIRMED", "M15_RANGE_BREAK_CONFIRMED"]
TIMEFRAME_DELTA = {"H4": timedelta(hours=4), "H1": timedelta(hours=1), "M15": timedelta(minutes=15)}


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def indicator_parameters(key: str) -> dict[str, Any]:
    periods = {"ema34": 34, "ema150": 150, "atr": 14, "adx": 14, "rsi": 14, "atr6": 6, "atr30": 30}
    period = periods[key]
    if key.startswith("ema"):
        return {"period": period, "price": "close"}
    return {"period": period}


def make_indicator(account: str, pair_id: str, candle: dict[str, Any], value_name: str, value: str) -> dict[str, Any]:
    key = "ema" if value_name.startswith("ema") else ("atr" if value_name.startswith("atr") else value_name)
    parameters = indicator_parameters(value_name)
    version = "v1"
    return {
        "id": f"indicator-{account}-{candle['id']}-{value_name}",
        "broker_account_id": account,
        "pair_id": pair_id,
        "timeframe": candle["timeframe"],
        "candle_id": candle["id"],
        "candle_open_time": candle["open_time"],
        "indicator_definition_id": f"indicator-definition-{key}-v1",
        "implementation_version": version,
        "parameters": parameters,
        "parameter_hash": canonical_parameter_hash(key, version, parameters),
        "value_name": value_name,
        "value_numeric": value,
    }


def build_graph(template: dict[str, Any], account: str, state: dict[str, Any], scenario_kind: str) -> dict[str, Any]:
    """Expand one declarative case into normalized, account-owned records."""
    pair = template["pair"]
    pair_id = f"pair-{account}-{pair.lower()}"
    trigger = parse_time("2026-01-09T00:00:00Z")
    candles: list[dict[str, Any]] = []
    by_timeframe: dict[str, list[str]] = {}
    for timeframe, cardinality in template["lookback"].items():
        delta = TIMEFRAME_DELTA[timeframe]
        ids: list[str] = []
        for index in range(cardinality):
            close_time = trigger - delta * (cardinality - 1 - index)
            candle = {
                "id": f"candle-{account}-{pair.lower()}-{timeframe.lower()}-{index:04d}",
                "broker_account_id": account,
                "pair_id": pair_id,
                "timeframe": timeframe,
                "open_time": iso(close_time - delta),
                "close_time": iso(close_time),
                "open": "100", "high": "101", "low": "99", "close": "100",
                "source_revision": "canonical-r1", "is_closed": True,
            }
            candles.append(candle)
            ids.append(candle["id"])
        by_timeframe[timeframe] = ids
    candle_by_id = {item["id"]: item for item in candles}
    indicators: list[dict[str, Any]] = []
    indicator_ids: list[str] = []
    required = {"H4": ("ema34", "ema150", "atr", "adx"), "H1": ("ema34", "atr", "rsi") if scenario_kind == "trend" else ("atr", "atr6", "atr30"), "M15": ("atr",)}
    # Every generated input candle has typed IndicatorValues. Terminal values are
    # overwritten below from the case's concrete OHLC/indicator state.
    for candle in candles:
        for value_name in required[candle["timeframe"]]:
            item = make_indicator(account, pair_id, candle, value_name, "0")
            indicators.append(item)
            indicator_ids.append(item["id"])
    config = {
        "id": f"config-version-{account}-{template['key']}-1",
        "broker_account_id": account,
        "pair_id": pair_id,
        "strategy": template["strategy"],
        "version": 1,
    }
    snapshot = {
        "id": f"snapshot-{account}-{pair.lower()}-{scenario_kind}",
        "broker_account_id": account,
        "pair_id": pair_id,
        "strategy_config_version_id": config["id"],
        "trigger_timeframe": "M15",
        "trigger_time": iso(trigger),
        "candle_ids": by_timeframe,
        "indicator_value_ids": indicator_ids,
        "completeness": "COMPLETE",
        "spread_at_evaluation": state["m15"].get("spread", "0.10"),
    }
    graph = {
        "broker_account": {"id": account, "environment": "DEMO"},
        "pair": {"id": pair_id, "broker_account_id": account, "canonical_code": pair},
        "strategy_config_version": config,
        "candles": candles,
        "indicator_values": indicators,
        "market_state_snapshot": snapshot,
        "pair_entry_gate_snapshot": {"id": f"gate-{account}-{pair.lower()}-{scenario_kind}", "broker_account_id": account, "pair_id": pair_id, "status": "OPEN"},
        "execution_state": {"id": f"execution-state-{account}-{pair.lower()}", "broker_account_id": account, "active_signal_ids": [], "order_ids": []},
    }
    if scenario_kind == "wti":
        # A forming H1 may be stored, but is deliberately not a Snapshot input
        # and therefore cannot influence the 12-bar range.
        graph["forming_h1_candle"] = {"id": f"candle-{account}-{pair.lower()}-h1-forming", "broker_account_id": account, "pair_id": pair_id, "timeframe": "H1", "open_time": iso(trigger), "close_time": iso(trigger + timedelta(hours=1)), "open": "100", "high": state["h1"]["forming_high"], "low": state["h1"]["forming_low"], "close": "100", "source_revision": "canonical-r1", "is_closed": False}
    synchronize_graph(graph, state, scenario_kind)
    return graph


def terminal(graph: dict[str, Any], timeframe: str) -> dict[str, Any]:
    item_id = graph["market_state_snapshot"]["candle_ids"][timeframe][-1]
    return next(item for item in graph["candles"] if item["id"] == item_id)


def sync_indicator(graph: dict[str, Any], candle_id: str, value_name: str, value: str) -> None:
    for indicator in graph["indicator_values"]:
        if indicator["candle_id"] == candle_id and indicator["value_name"] == value_name:
            indicator["value_numeric"] = value
            return
    fail(f"scenario graph: missing terminal {value_name} IndicatorValue")


def synchronize_graph(graph: dict[str, Any], state: dict[str, Any], scenario_kind: str) -> None:
    h4, h1, m15 = terminal(graph, "H4"), terminal(graph, "H1"), terminal(graph, "M15")
    h4.update({name: state["h4"][name] for name in ("close",)})
    h4["open"], h4["high"], h4["low"] = state["h4"]["ema150"], max(parse_decimal(state["h4"]["close"]), parse_decimal(state["h4"]["ema34"]), parse_decimal(state["h4"]["ema150"])).to_eng_string(), min(parse_decimal(state["h4"]["close"]), parse_decimal(state["h4"]["ema150"])).to_eng_string()
    for name in ("ema34", "ema150", "atr", "adx"):
        sync_indicator(graph, h4["id"], name, state["h4"][name])
    # ema150_previous is the value exactly slope_bars candles ago, not a second
    # terminal value on the current Candle.
    h4_prior = graph["market_state_snapshot"]["candle_ids"]["H4"][-6]
    sync_indicator(graph, h4_prior, "ema150", state["h4"]["ema150_previous"])
    h1.update({name: state["h1"][name] for name in ("open", "high", "low") if name in state["h1"]})
    h1["close"] = state["h1"].get("latest_close", state["h1"].get("range_high", "100"))
    if scenario_kind == "trend":
        for name in ("ema34", "atr", "rsi"):
            sync_indicator(graph, h1["id"], name, state["h1"][f"{name}_latest"] if name == "rsi" else state["h1"][name])
        h1_previous = graph["market_state_snapshot"]["candle_ids"]["H1"][-2]
        sync_indicator(graph, h1_previous, "rsi", state["h1"]["rsi_previous"])
        touch_id = graph["market_state_snapshot"]["candle_ids"]["H1"][-3]
        touch_candle = next(item for item in graph["candles"] if item["id"] == touch_id)
        previous_candle = next(item for item in graph["candles"] if item["id"] == h1_previous)
        touch = parse_decimal(state["h1"]["touch"])
        invalidation = parse_decimal(state["h1"]["invalidation_close"])
        if parse_decimal(state["h1"]["latest_close"]) > parse_decimal(state["h1"]["ema34"]):
            touch_candle.update({"open": max(Decimal("100"), touch).to_eng_string(), "close": max(Decimal("100"), touch).to_eng_string(), "low": touch.to_eng_string(), "high": state["h1"]["high"]})
            previous_candle.update({"open": max(Decimal("100"), invalidation).to_eng_string(), "close": invalidation.to_eng_string(), "low": min(invalidation, touch).to_eng_string(), "high": state["h1"]["high"]})
        else:
            touch_candle.update({"open": "100", "close": "100", "high": max(Decimal("100"), touch).to_eng_string(), "low": state["h1"]["low"]})
            previous_candle.update({"open": "100", "close": invalidation.to_eng_string(), "high": max(invalidation, touch).to_eng_string(), "low": state["h1"]["low"]})
    else:
        range_high, range_low = parse_decimal(state["h1"]["range_high"]), parse_decimal(state["h1"]["range_low"])
        h1.update({"open": ((range_high + range_low) / 2).to_eng_string(), "high": state["h1"]["range_high"], "low": state["h1"]["range_low"]})
        for name in ("atr", "atr6", "atr30"):
            sync_indicator(graph, h1["id"], name, state["h1"]["atr14"] if name == "atr" else state["h1"][name])
        range_ids = graph["market_state_snapshot"]["candle_ids"]["H1"][-13:-1]
        for candle_id in range_ids:
            candle = next(item for item in graph["candles"] if item["id"] == candle_id)
            candle["open"] = candle["close"] = ((range_high + range_low) / 2).to_eng_string()
            candle["high"], candle["low"] = state["h1"]["range_high"], state["h1"]["range_low"]
    m15.update({name: state["m15"][name] for name in ("open", "high", "low", "close") if name in state["m15"]})
    if "high" not in state["m15"]:
        m15["high"] = max(parse_decimal(m15["open"]), parse_decimal(m15["close"])).to_eng_string()
    if "low" not in state["m15"]:
        m15["low"] = min(parse_decimal(m15["open"]), parse_decimal(m15["close"])).to_eng_string()
    sync_indicator(graph, m15["id"], "atr", state["m15"]["atr"])
    channel_ids = graph["market_state_snapshot"]["candle_ids"]["M15"][-13:-1]
    for candle_id in channel_ids:
        candle = next(item for item in graph["candles"] if item["id"] == candle_id)
        channel_high = parse_decimal(state["m15"].get("channel_high", state["h1"].get("range_high", "110")))
        channel_low = parse_decimal(state["m15"].get("channel_low", state["h1"].get("range_low", "90")))
        candle["high"], candle["low"] = channel_high.to_eng_string(), channel_low.to_eng_string()
        if scenario_kind == "wti":
            candle["open"] = candle["close"] = ((channel_high + channel_low) / 2).to_eng_string()
    graph["market_state_snapshot"]["spread_at_evaluation"] = state["m15"].get("spread", "0.10")
    if scenario_kind == "wti":
        graph["forming_h1_candle"]["high"], graph["forming_h1_candle"]["low"] = state["h1"]["forming_high"], state["h1"]["forming_low"]


def indicator_value(graph: dict[str, Any], candle_id: str, name: str) -> Decimal:
    matches = [item for item in graph["indicator_values"] if item["candle_id"] == candle_id and item["value_name"] == name]
    require(len(matches) == 1, f"scenario graph: expected one {name} IndicatorValue for {candle_id}")
    return parse_decimal(matches[0]["value_numeric"])


def validate_graph(graph: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    """Validate FK scope, revisions, UTC chronology and exact lookback prefixes."""
    account = graph["broker_account"]["id"]
    pair = graph["pair"]
    config, snapshot, gate = graph["strategy_config_version"], graph["market_state_snapshot"], graph["pair_entry_gate_snapshot"]
    for entity_name, entity in (("Pair", pair), ("StrategyConfig", config), ("MarketStateSnapshot", snapshot), ("PairEntryGateSnapshot", gate), ("ExecutionState", graph["execution_state"])):
        if entity["broker_account_id"] != account:
            return {"outcome": "REJECT_GRAPH", "reason_codes": ["ACCOUNT_CONTEXT_MISMATCH"], "entity": entity_name}
    if any(entity["pair_id"] != pair["id"] for entity in (config, snapshot, gate)):
        return {"outcome": "REJECT_GRAPH", "reason_codes": ["ACCOUNT_CONTEXT_MISMATCH"], "entity": "PairForeignKey"}
    candle_index = {item["id"]: item for item in graph["candles"]}
    if len(candle_index) != len(graph["candles"]):
        return {"outcome": "REJECT_GRAPH", "reason_codes": ["DUPLICATE_CANDLE_ID"]}
    for candle in graph["candles"]:
        if parse_decimal(candle["high"]) < max(parse_decimal(candle["open"]), parse_decimal(candle["close"])) or parse_decimal(candle["low"]) > min(parse_decimal(candle["open"]), parse_decimal(candle["close"])):
            return {"outcome": "REJECT_GRAPH", "reason_codes": ["INVALID_CANDLE_OHLC"]}
    for timeframe, expected_count in template["lookback"].items():
        ids = snapshot["candle_ids"].get(timeframe, [])
        if len(ids) < expected_count:
            return {"outcome": "SKIP_EVALUATION", "reason_codes": ["INSUFFICIENT_LOOKBACK"]}
        if len(ids) != expected_count:
            return {"outcome": "REJECT_GRAPH", "reason_codes": ["LOOKBACK_PREFIX_NOT_EXACT"]}
        series = [candle_index.get(item) for item in ids]
        if any(item is None or item["broker_account_id"] != account or item["pair_id"] != pair["id"] for item in series):
            return {"outcome": "REJECT_GRAPH", "reason_codes": ["ACCOUNT_CONTEXT_MISMATCH"]}
        if any(item["is_closed"] is not True for item in series):
            return {"outcome": "SKIP_EVALUATION", "reason_codes": ["OPEN_CANDLE_INPUT"]}
        if any(item["source_revision"] != "canonical-r1" for item in series):
            return {"outcome": "SKIP_EVALUATION", "reason_codes": ["REVISION_MISMATCH"]}
        delta = TIMEFRAME_DELTA[timeframe]
        times = [parse_time(item["close_time"]) for item in series]
        if any(right - left != delta for left, right in zip(times, times[1:])):
            return {"outcome": "SKIP_EVALUATION", "reason_codes": ["GAP_DETECTED"]}
    trigger = parse_time(snapshot["trigger_time"])
    if snapshot["trigger_timeframe"] != "M15" or parse_time(candle_index[snapshot["candle_ids"]["M15"][-1]]["close_time"]) != trigger:
        return {"outcome": "REJECT_GRAPH", "reason_codes": ["TRIGGER_ALIGNMENT_FAILED"]}
    if any(parse_time(candle_index[snapshot["candle_ids"][frame][-1]]["close_time"]) > trigger for frame in ("H4", "H1")):
        return {"outcome": "REJECT_GRAPH", "reason_codes": ["CLOSED_CANDLE_ALIGNMENT_FAILED"]}
    indicator_index = {item["id"]: item for item in graph["indicator_values"]}
    if len(indicator_index) != len(graph["indicator_values"]):
        return {"outcome": "REJECT_GRAPH", "reason_codes": ["DUPLICATE_INDICATOR_ID"]}
    for indicator_id in snapshot["indicator_value_ids"]:
        item = indicator_index.get(indicator_id)
        if item is None or item["broker_account_id"] != account or item["pair_id"] != pair["id"] or item["candle_id"] not in candle_index:
            return {"outcome": "REJECT_GRAPH", "reason_codes": ["ACCOUNT_CONTEXT_MISMATCH"]}
        key = "ema" if item["value_name"].startswith("ema") else ("atr" if item["value_name"].startswith("atr") else item["value_name"])
        if item["parameter_hash"] != canonical_parameter_hash(key, item["implementation_version"], item["parameters"]):
            return {"outcome": "REJECT_GRAPH", "reason_codes": ["INDICATOR_PARAMETER_HASH_MISMATCH"]}
    if "forming_h1_candle" in graph:
        forming = graph["forming_h1_candle"]
        if forming["broker_account_id"] != account or forming["pair_id"] != pair["id"]:
            return {"outcome": "REJECT_GRAPH", "reason_codes": ["ACCOUNT_CONTEXT_MISMATCH"]}
        if forming["is_closed"] or forming["id"] in snapshot["candle_ids"]["H1"]:
            return {"outcome": "REJECT_GRAPH", "reason_codes": ["FORMING_CANDLE_INCLUDED"]}
    return {"outcome": "COMPLETE", "reason_codes": []}


def result(outcome: str, reason_codes: list[str], **values: Any) -> dict[str, Any]:
    return {"outcome": outcome, "reason_codes": reason_codes, **values}


def evaluate_trend(graph: dict[str, Any], template: dict[str, Any], direction: str, force_conflict: bool = False) -> dict[str, Any]:
    validity = validate_graph(graph, template)
    if validity["outcome"] != "COMPLETE":
        return validity
    if force_conflict:
        return result("NO_OPPORTUNITY", ["CONFLICTING_SETUPS"])
    sign = Decimal(1) if direction == "LONG" else Decimal(-1)
    h4, h1, m15 = terminal(graph, "H4"), terminal(graph, "H1"), terminal(graph, "M15")
    h4_id, h1_id, m15_id = h4["id"], h1["id"], m15["id"]
    close, fast, slow, h4_atr, adx_value = (parse_decimal(h4["close"]), indicator_value(graph, h4_id, "ema34"), indicator_value(graph, h4_id, "ema150"), indicator_value(graph, h4_id, "atr"), indicator_value(graph, h4_id, "adx"))
    slow_previous = indicator_value(graph, graph["market_state_snapshot"]["candle_ids"]["H4"][-6], "ema150")
    if not (sign * (close - slow) > 0):
        return result("NO_OPPORTUNITY", ["TREND_PRICE_FILTER_FAILED"])
    if not (sign * (fast - slow) > 0):
        return result("NO_OPPORTUNITY", ["EMA_ORDER_FAILED"])
    if not (sign * (fast - slow) >= parse_decimal(graph["_contract"]["h4_trend"]["ema_separation_atr"]) * h4_atr):
        return result("NO_OPPORTUNITY", ["EMA_SEPARATION_FAILED"])
    if not (sign * (slow - slow_previous) > 0):
        return result("NO_OPPORTUNITY", ["EMA_SLOPE_FAILED"])
    if adx_value < parse_decimal(graph["_contract"]["h4_trend"]["adx_min"]):
        return result("NO_OPPORTUNITY", ["ADX_FILTER_FAILED"])
    h1_ema, h1_atr = indicator_value(graph, h1_id, "ema34"), indicator_value(graph, h1_id, "atr")
    latest_rsi = indicator_value(graph, h1_id, "rsi")
    pullback_ids = graph["market_state_snapshot"]["candle_ids"]["H1"][-3:-1]
    pullbacks = [next(item for item in graph["candles"] if item["id"] == item_id) for item_id in pullback_ids]
    prior_h1_id = pullback_ids[-1]
    prior_h1 = pullbacks[-1]
    prior_rsi = indicator_value(graph, prior_h1_id, "rsi")
    tolerance = parse_decimal(graph["_contract"]["h1_pullback"]["pullback_tolerance_atr"]) * h1_atr
    touch = min(parse_decimal(item["low"]) for item in pullbacks) if direction == "LONG" else max(parse_decimal(item["high"]) for item in pullbacks)
    if not (sign * (touch - h1_ema) <= tolerance):
        return result("NO_OPPORTUNITY", ["PULLBACK_NOT_TOUCHED"])
    invalidation_close = min(parse_decimal(item["close"]) for item in pullbacks) if direction == "LONG" else max(parse_decimal(item["close"]) for item in pullbacks)
    if not (sign * (invalidation_close - h1_ema) > -tolerance):
        return result("NO_OPPORTUNITY", ["PULLBACK_INVALIDATED"])
    h1_open, h1_close, h1_high, h1_low = (parse_decimal(h1["open"]), parse_decimal(h1["close"]), parse_decimal(h1["high"]), parse_decimal(h1["low"]))
    body = Decimal(0) if h1_high == h1_low else abs(h1_close - h1_open) / (h1_high - h1_low)
    if not (sign * (h1_close - h1_ema) > 0 and sign * (h1_close - h1_open) > 0 and body >= parse_decimal(graph["_contract"]["h1_pullback"]["min_body_fraction"])):
        return result("NO_OPPORTUNITY", ["H1_BODY_FILTER_FAILED"])
    reclaim = parse_decimal(graph["_contract"]["h1_pullback"]["reclaim_level_rsi"])
    if not (sign * (latest_rsi - reclaim) >= 0 and sign * (prior_rsi - reclaim) < 0):
        return result("NO_OPPORTUNITY", ["RSI_RECLAIM_FAILED"])
    channel = [next(item for item in graph["candles"] if item["id"] == candle_id) for candle_id in graph["market_state_snapshot"]["candle_ids"]["M15"][-13:-1]]
    boundary = max(parse_decimal(item["high"]) for item in channel) if direction == "LONG" else min(parse_decimal(item["low"]) for item in channel)
    m15_atr = indicator_value(graph, m15_id, "atr")
    buffer_atr = parse_decimal(template["m15"]["buffer_atr"]) * m15_atr
    spread_buffer = parse_decimal(graph["_contract"]["trend_m15"]["buffer_spread_multiple"]) * parse_decimal(graph["market_state_snapshot"]["spread_at_evaluation"])
    trigger_buffer = max(buffer_atr, spread_buffer)
    m_open, m_close, m_high, m_low = (parse_decimal(m15["open"]), parse_decimal(m15["close"]), parse_decimal(m15["high"]), parse_decimal(m15["low"]))
    if not sign * (m_close - boundary) > trigger_buffer:
        return result("NO_OPPORTUNITY", ["M15_BREAKOUT_NOT_CLOSED"])
    body = Decimal(0) if m_high == m_low else abs(m_close - m_open) / (m_high - m_low)
    if not (sign * (m_close - m_open) > 0 and body >= parse_decimal(graph["_contract"]["trend_m15"]["min_body_fraction"])):
        return result("NO_OPPORTUNITY", ["M15_BODY_FILTER_FAILED"])
    if sign * (m_close - boundary) > parse_decimal(graph["_contract"]["trend_m15"]["max_extension_atr"]) * m15_atr:
        return result("NO_OPPORTUNITY", ["TRIGGER_OVEREXTENDED"])
    return result("OPPORTUNITY", SUCCESS_TREND, direction=direction, confidence="0.70", evaluation_key={"strategy_config_version_id": graph["strategy_config_version"]["id"], "pair_id": graph["pair"]["id"], "trigger_time": graph["market_state_snapshot"]["trigger_time"]})


def evaluate_wti(graph: dict[str, Any], template: dict[str, Any], direction: str, gate: str = "OPEN") -> dict[str, Any]:
    if gate != "OPEN":
        return result("SKIP_EVALUATION", [{"ROLL_GUARD": "WTI_ROLL_GUARD_ACTIVE", "REOPEN_COOLDOWN": "WTI_REOPEN_COOLDOWN_ACTIVE", "FALSE_BREAKOUT_COOLDOWN": "WTI_COOLDOWN_ACTIVE"}[gate]])
    validity = validate_graph(graph, template)
    if validity["outcome"] != "COMPLETE":
        return validity
    h4, h1, m15 = terminal(graph, "H4"), terminal(graph, "H1"), terminal(graph, "M15")
    h4_id, h1_id, m15_id = h4["id"], h1["id"], m15["id"]
    close, fast, slow = parse_decimal(h4["close"]), indicator_value(graph, h4_id, "ema34"), indicator_value(graph, h4_id, "ema150")
    sign = Decimal(1) if direction == "LONG" else Decimal(-1)
    if not (sign * (close - slow) > 0 and sign * (fast - slow) > 0 and sign * (fast - slow) >= Decimal("0.10") * indicator_value(graph, h4_id, "atr") and sign * (slow - indicator_value(graph, graph["market_state_snapshot"]["candle_ids"]["H4"][-6], "ema150")) > 0 and indicator_value(graph, h4_id, "adx") >= Decimal("22")):
        return result("NO_OPPORTUNITY", ["H4_TREND_FILTER_FAILED"])
    range_candles = [next(item for item in graph["candles"] if item["id"] == item_id) for item_id in graph["market_state_snapshot"]["candle_ids"]["H1"][-13:-1]]
    high, low = max(parse_decimal(item["high"]) for item in range_candles), min(parse_decimal(item["low"]) for item in range_candles)
    atr14 = indicator_value(graph, h1_id, "atr")
    if not (Decimal("1.00") * atr14 <= high - low <= Decimal("3.00") * atr14):
        return result("NO_OPPORTUNITY", ["WTI_RANGE_WIDTH_FAILED"])
    if indicator_value(graph, h1_id, "atr6") / indicator_value(graph, h1_id, "atr30") > Decimal("0.65"):
        return result("NO_OPPORTUNITY", ["WTI_COMPRESSION_FAILED"])
    m_open, m_close = parse_decimal(m15["open"]), parse_decimal(m15["close"])
    m_atr = indicator_value(graph, m15_id, "atr")
    boundary = high if direction == "LONG" else low
    if not sign * (m_close - boundary) > Decimal("0.10") * m_atr:
        return result("NO_OPPORTUNITY", ["WTI_BREAKOUT_NOT_CLOSED"])
    if not sign * (m_close - m_open) >= Decimal("0.50") * m_atr:
        return result("NO_OPPORTUNITY", ["WTI_BODY_FILTER_FAILED"])
    if sign * (m_close - boundary) > Decimal("1.25") * m_atr:
        return result("NO_OPPORTUNITY", ["TRIGGER_OVEREXTENDED"])
    return result("OPPORTUNITY", SUCCESS_WTI, direction=direction, confidence="0.68")


def apply_set(state: dict[str, Any], values: dict[str, str]) -> bool:
    conflict = False
    for path, value in values.items():
        if path == "force_conflict":
            conflict = value == "true"
            continue
        parent, key = path.split(".")
        state[parent][key] = value
    return conflict


def assert_expected(actual: dict[str, Any], expected: dict[str, Any], case_id: str) -> None:
    for key, value in expected.items():
        require(actual.get(key) == value, f"scenario {case_id}: expected {key}={value!r}, got {actual.get(key)!r}")


def run_trend_cases(data: dict[str, Any]) -> None:
    contract, scenarios = data["template_contract"], data["scenario_contract"]
    templates = {item["pair"]: item for item in contract["templates"] if item["strategy"] == "TrendPullbackContinuationStrategy@0.1.0"}
    cases = scenarios["trend_cases"]
    generated_ids: set[str] = set()
    for pair in cases["pairs"]:
        for direction in cases["directions"]:
            state = deepcopy(scenarios["trend_profiles"][direction.lower()])
            graph = build_graph(templates[pair], "broker-account-a", state, "trend")
            graph["_contract"] = contract
            assert_expected(evaluate_trend(graph, templates[pair], direction), cases["expected_success"], f"{pair}-{direction}")
            generated_ids.update(item["id"] for item in graph["candles"] + graph["indicator_values"])
            clone = build_graph(templates[pair], "broker-account-b", state, "trend")
            clone["_contract"] = contract
            require(not generated_ids.intersection(item["id"] for item in clone["candles"] + clone["indicator_values"]), f"{pair}-{direction}: account clone shares entity IDs")
            require(graph["pair"]["id"] != clone["pair"]["id"] and graph["market_state_snapshot"]["id"] != clone["market_state_snapshot"]["id"] and graph["pair_entry_gate_snapshot"]["id"] != clone["pair_entry_gate_snapshot"]["id"] and graph["execution_state"]["id"] != clone["execution_state"]["id"], f"{pair}-{direction}: account clone shares scoped state")
            assert_expected(evaluate_trend(clone, templates[pair], direction), cases["expected_success"], f"{pair}-{direction}-clone")
    for mutation in cases["mutations"]:
        state = deepcopy(scenarios["trend_profiles"]["long"])
        conflict = apply_set(state, mutation["set"])
        graph = build_graph(templates[cases["canonical_pair"]], "broker-account-a", state, "trend")
        graph["_contract"] = contract
        assert_expected(evaluate_trend(graph, templates[cases["canonical_pair"]], "LONG", conflict), mutation["expected"], mutation["id"])
    for gate_case in cases["execution_gates"]:
        if "execution_time" in gate_case:
            actual = result("EXECUTABLE", []) if parse_time(gate_case["execution_time"]) < parse_time(gate_case["expires_at"]) else result("SIGNAL_BLOCKED", ["SIGNAL_EXPIRED"])
        else:
            # A new structured evaluation key is necessary but cannot bypass an
            # existing Signal/Order ownership of the armed H1 setup.
            actual = result("SIGNAL_BLOCKED", ["SETUP_ALREADY_OWNED"]) if gate_case["active_owner"] else result("SIGNAL_CREATED", [])
        assert_expected(actual, gate_case["expected"], gate_case["id"])
    require({item["id"] for item in cases["execution_gates"]} == {"ttl-one-quantum-before", "ttl-equality", "new-key-no-active-owner", "new-key-active-owner"}, "trend execution gate coverage drift")
    # Config version and replay are evaluated from real structured keys.
    state = deepcopy(scenarios["trend_profiles"]["long"])
    first = build_graph(templates["XAUUSD"], "broker-account-a", state, "trend"); first["_contract"] = contract
    second = deepcopy(first); second["strategy_config_version"]["id"] = "config-version-broker-account-a-xauusd-v2"; second["market_state_snapshot"]["strategy_config_version_id"] = second["strategy_config_version"]["id"]
    first_result, second_result = evaluate_trend(first, templates["XAUUSD"], "LONG"), evaluate_trend(second, templates["XAUUSD"], "LONG")
    require(first_result["evaluation_key"] != second_result["evaluation_key"], "config-version isolation: evaluation keys collide")
    require(evaluate_trend(first, templates["XAUUSD"], "LONG")["evaluation_key"] == first_result["evaluation_key"], "replay deduplication: same structured key changed")


def run_completeness_cases(data: dict[str, Any]) -> None:
    contract, scenarios = data["template_contract"], data["scenario_contract"]
    template = next(item for item in contract["templates"] if item["pair"] == "XAUUSD")
    for mutation in scenarios["completeness_mutations"]:
        graph = build_graph(template, "broker-account-a", deepcopy(scenarios["trend_profiles"]["long"]), "trend")
        graph["_contract"] = contract
        snapshot = graph["market_state_snapshot"]
        if mutation["kind"] == "open_candle":
            next(item for item in graph["candles"] if item["id"] == snapshot["candle_ids"]["M15"][-1])["is_closed"] = False
        elif mutation["kind"] == "short_lookback":
            snapshot["candle_ids"][mutation["timeframe"]].pop(0)
        elif mutation["kind"] == "gap":
            candle = next(item for item in graph["candles"] if item["id"] == snapshot["candle_ids"]["H1"][20])
            candle["close_time"] = iso(parse_time(candle["close_time"]) + timedelta(minutes=1))
        elif mutation["kind"] == "revision_mismatch":
            next(item for item in graph["candles"] if item["id"] == snapshot["candle_ids"]["H4"][-1])["source_revision"] = "canonical-r2"
        elif mutation["kind"] == "foreign_account":
            snapshot["broker_account_id"] = "broker-account-b"
        assert_expected(validate_graph(graph, template), mutation["expected"], mutation["id"])


def run_wti_cases(data: dict[str, Any]) -> None:
    contract, scenarios = data["template_contract"], data["scenario_contract"]
    template, cases = contract["templates"][-1], scenarios["wti_cases"]
    for activation in cases["activation_cases"]:
        actual = result("ACTIVATION_ALLOWED", []) if activation["mapping"] == "WTI_US_CRUDE" and activation["roll_semantics"] == "KNOWN" else result("ACTIVATION_BLOCKED", ["WTI_MAPPING_AMBIGUOUS"] if activation["mapping"] != "WTI_US_CRUDE" else ["WTI_ROLL_SEMANTICS_UNKNOWN"])
        assert_expected(actual, activation["expected"], activation["id"])
    for direction in cases["directions"]:
        profile = direction.lower()
        base = build_graph(template, "broker-account-a", deepcopy(cases["profiles"][profile]), "wti"); base["_contract"] = contract
        assert_expected(evaluate_wti(base, template, direction), cases["expected_success"][profile], f"wti-{profile}-valid")
        for mutation in cases["mutations"][profile]:
            state = deepcopy(cases["profiles"][profile])
            if "set" in mutation:
                apply_set(state, mutation["set"])
            graph = build_graph(template, "broker-account-a", state, "wti"); graph["_contract"] = contract
            if mutation.get("data_mutation") == "gap":
                candle = next(item for item in graph["candles"] if item["id"] == graph["market_state_snapshot"]["candle_ids"]["H1"][20])
                candle["close_time"] = iso(parse_time(candle["close_time"]) + timedelta(minutes=1))
            if "new_range_final_candle_id" in mutation:
                require(mutation["new_range_final_candle_id"] != graph["market_state_snapshot"]["candle_ids"]["H1"][-2], f"{mutation['id']}: new range reuses cooldown range final candle")
            assert_expected(evaluate_wti(graph, template, direction, mutation.get("gate", "OPEN")), mutation["expected"], mutation["id"])
    expires = cases["expiry_gate"]
    actual = result("SIGNAL_BLOCKED", ["SIGNAL_EXPIRED"]) if parse_time(expires["execution_time"]) >= parse_time(expires["expires_at"]) else result("EXECUTABLE", [])
    assert_expected(actual, expires["expected"], "wti-expiry")


def rounded_down(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def execute_exit_case(case: dict[str, Any]) -> dict[str, Any]:
    """Small deterministic projection of the entry/protection/exit contract."""
    volume, step = parse_decimal(case.get("volume", "1.00")), parse_decimal(case.get("step", "0.01"))
    state: dict[str, Any] = {"order_status": "FILLED", "exposure_gate": "OPEN", "stage": "ENTRY", "position": "OPEN", "reason_codes": [], "reductions": [], "reduce_only_commands": 0, "native_protection": "ACTIVE"}
    submitted_unconfirmed = False
    for action in case["actions"]:
        if action == "FRESH_QUOTE":
            state["entry_quote"] = "FRESH"
        elif action == "PRE_ORDER_GATE":
            state["evaluation_gate_id"], state["pre_order_gate_id"] = "gate-evaluation", "gate-pre-order"
            state["distinct_pre_order_gate"] = state["evaluation_gate_id"] != state["pre_order_gate_id"]
        elif action == "SUBMIT_WITH_NATIVE_PROTECTION":
            require(state.get("entry_quote") == "FRESH" and state.get("pre_order_gate_id"), f"{case['id']}: entry did not use fresh quote and pre-order gate")
            state.update({"order_status": "SUBMITTED", "native_sl": True, "native_tp_r": "4.00"})
            submitted_unconfirmed = True
        elif action == "PROTECTION_CONFIRMED":
            require(submitted_unconfirmed, f"{case['id']}: protection cannot confirm before submission")
            submitted_unconfirmed = False
        elif action == "TARGET_1_CROSSED":
            state["reason_codes"].append("TARGET_CROSSING_UNCONFIRMED")
        elif action in ("TP1_FILL", "TP2_FILL"):
            fraction = Decimal("0.40") if action == "TP1_FILL" else Decimal("0.30")
            reduction = rounded_down(volume * fraction, step)
            if reduction < step:
                state.update({"stage": "BLOCKED", "reason_codes": ["UNSPLITTABLE_POSITION_SIZE"]})
                break
            state["reductions"].append(reduction.to_eng_string())
            state["stage"] = "TP1_CONFIRMED" if action == "TP1_FILL" else "TP2_CONFIRMED"
            if action == "TP2_FILL":
                state["runner_trailing"] = True
        elif action == "SPLIT_CHECK":
            if any(rounded_down(volume * fraction, step) < step for fraction in (Decimal("0.40"), Decimal("0.30"))):
                state.update({"stage": "BLOCKED", "reason_codes": ["UNSPLITTABLE_POSITION_SIZE"]})
        elif action == "TRAIL_NO_NEW_CANDLE":
            state["reason_codes"].append("TRAIL_WAITING_FOR_CLOSED_CANDLE")
        elif action == "TRAIL_LOOSER":
            state["reason_codes"].append("TRAIL_NOT_TIGHTER")
        elif action == "TRAIL_FREEZE_VIOLATION":
            state["reason_codes"].append("TRAIL_STOPS_FREEZE_VIOLATION")
        elif action == "TRAIL_UNKNOWN":
            state["reconciliation"] = "REQUIRED"; state["reason_codes"].append("TRAIL_MODIFICATION_UNKNOWN")
        elif action == "TRAIL_UNSUPPORTED":
            state["degradation"] = "FIXED_NATIVE_PROTECTION"; state["reason_codes"].append("TRAILING_DEGRADED")
        elif action == "OPPOSITE_H4":
            state.update({"position": "CLOSING", "reduce_only_commands": 1}); state["reason_codes"].append("OPPOSITE_H4_EXIT")
        elif action == "NEUTRAL_H4":
            pass
        elif action == "SIGNAL_EXPIRED":
            state["reason_codes"].append("SIGNAL_EXPIRED_POSITION_UNCHANGED")
        elif action == "DISCONNECT":
            state["reason_codes"].append("CONNECTOR_DISCONNECTED_PROTECTION_REMAINS")
        else:
            fail(f"{case['id']}: unknown execution action {action}")
    if submitted_unconfirmed:
        state["exposure_gate"] = "QUARANTINED"
        state["reason_codes"].append("PROTECTION_UNCONFIRMED")
    if state["reductions"]:
        reductions = [parse_decimal(item) for item in state["reductions"]]
        require(sum(reductions) <= volume, f"{case['id']}: requested reductions exceed filled volume")
        state["runner_volume"] = (volume - sum(reductions)).to_eng_string()
    return state


def run_execution_exit_cases(data: dict[str, Any]) -> None:
    case_ids = set()
    for case in data["scenario_contract"]["execution_exit_cases"]:
        require(case["id"] not in case_ids, f"execution fixture: duplicate case {case['id']}")
        case_ids.add(case["id"])
        assert_expected(execute_exit_case(case), case["expected"], case["id"])
    required = {
        "entry-fresh-pre-order-gate", "protection-unconfirmed-quarantine", "tp-cross-without-fill", "tp1-fill", "tp2-fill", "rounding-residual", "unsplittable", "trailing-no-new-close", "trailing-never-loosens", "trailing-freeze", "trailing-unknown", "trailing-unsupported", "opposite-h4-exit", "neutral-h4-no-exit", "disconnect-native-protection",
    }
    require(case_ids == required, f"execution fixture: coverage drift: {case_ids ^ required}")


def verify_scenario_contract(data: dict[str, Any]) -> None:
    scenarios = data["scenario_contract"]
    require(scenarios["entity_revision"] == "canonical-r1", "scenario fixture: entity revision drift")
    require(scenarios["account_ids"] == ["broker-account-a", "broker-account-b"], "scenario fixture: account clone contract drift")
    require(scenarios["trend_cases"]["pairs"] == ["XAUUSD", "USDJPY"], "scenario fixture: trend Pair matrix drift")
    require(scenarios["trend_cases"]["directions"] == ["LONG", "SHORT"], "scenario fixture: mirror direction matrix drift")
    wti_cases = scenarios["wti_cases"]
    require(wti_cases["directions"] == ["LONG", "SHORT"], "scenario fixture: WTI mirror direction matrix drift")
    require(set(wti_cases["profiles"]) == {"long", "short"} and set(wti_cases["expected_success"]) == {"long", "short"} and set(wti_cases["mutations"]) == {"long", "short"}, "scenario fixture: WTI directional case matrix drift")
    for direction in wti_cases["directions"]:
        profile = direction.lower()
        require(wti_cases["expected_success"][profile].get("direction") == direction, f"scenario fixture: WTI {direction} expected output direction drift")
        for mutation in wti_cases["mutations"][profile]:
            if mutation["expected"].get("outcome") == "OPPORTUNITY":
                require(mutation["expected"].get("direction") == direction, f"scenario fixture: {mutation['id']} output direction drift")
    require(any(item["id"] == "short-directional-body-negative" for item in wti_cases["mutations"]["short"]), "scenario fixture: missing WTI SHORT directional-body negative mutation")
    required_completeness = {"open-candle", "lookback-h4-short", "lookback-h1-short", "lookback-m15-short", "gap", "revision-mismatch", "foreign-account"}
    require({item["id"] for item in scenarios["completeness_mutations"]} == required_completeness, "scenario fixture: completeness mutation matrix drift")
    # Cases are data-bearing executable inputs. Every expected result includes
    # a concrete outcome/state and a complete ordered reason-code list.
    wti_mutations = [item for direction in wti_cases["directions"] for item in wti_cases["mutations"][direction.lower()]]
    for collection in (scenarios["trend_cases"]["mutations"], scenarios["completeness_mutations"], wti_mutations, scenarios["execution_exit_cases"]):
        for case in collection:
            expected = case.get("expected", {})
            require("reason_codes" in expected and ("outcome" in expected or "order_status" in expected or "stage" in expected or "position" in expected), f"scenario fixture: {case['id']} lacks executable expected result")


def negative_mutation_probe(data: dict[str, Any]) -> None:
    """Ensure mutation checks are independent and can actually detect drift."""
    contract = data["template_contract"]
    template = next(item for item in contract["templates"] if item["pair"] == "XAUUSD")
    graph = build_graph(template, "broker-account-a", deepcopy(data["scenario_contract"]["trend_profiles"]["long"]), "trend")
    graph["_contract"] = contract
    graph["market_state_snapshot"]["candle_ids"]["H4"].pop()
    actual = validate_graph(graph, template)
    require(actual == {"outcome": "SKIP_EVALUATION", "reason_codes": ["INSUFFICIENT_LOOKBACK"]}, "negative mutation probe: independent short-lookback rejection failed")
    wti_template = next(item for item in contract["templates"] if item["pair"] == "WTI")
    wti_cases = data["scenario_contract"]["wti_cases"]
    directional_body = next(item for item in wti_cases["mutations"]["short"] if item["id"] == "short-directional-body-negative")
    wti_state = deepcopy(wti_cases["profiles"]["short"])
    apply_set(wti_state, directional_body["set"])
    wti_graph = build_graph(wti_template, "broker-account-a", wti_state, "wti")
    wti_graph["_contract"] = contract
    require(evaluate_wti(wti_graph, wti_template, "SHORT") == directional_body["expected"], "negative mutation probe: WTI SHORT directional body drift was not detected")


def verify_qm_ao_contract() -> tuple[int, int, int]:
    """Validate the independent H4/M30 EURUSD SND/AO/QM fixture contract."""
    q = json.loads(QM_AO_FIXTURE.read_text(encoding="utf-8"))
    require(q["fixture_schema_version"] == "1.0.0", "QM/AO: unsupported schema version")
    require((q["plugin"], q["template"], q["pair"], q["trigger_timeframe"], q["entry_order_type"]) == ("SupplyDemandAOQMStrategy@0.1.0", "eurusd-snd-ao-qm-bidirectional-v0", "EURUSD", "M30", "LIMIT"), "QM/AO: strategy identity drift")
    require(q["required_timeframes"] == {"H4": 200, "M30": 60} and q["confidence"] == "0.72", "QM/AO: timeframe/confidence drift")
    reasons = ["H4_REGIME_CONFIRMED", "H4_ZONE_VALID", "M30_AO_DIVERGENCE", "M30_QM_CONFIRMED", "RETEST_LIMIT_ARMED"]
    require(q["reason_codes"] == reasons, "QM/AO: reason-code contract drift")
    p = q["parameters"]
    require(p["ao"] == {"fast_period": 5, "slow_period": 34, "price_source": "MEDIAN_HIGH_LOW"}, "QM/AO: AO parameters drift")
    require(p["signal_ttl_minutes"] == 30 and p["pending_order_expiry_minutes"] == 120 and p["minimum_net_reward_risk"] == "2.00", "QM/AO: expiry/R:R parameters drift")
    cases = {case["id"]: case for case in q["valid_cases"]}
    require(set(cases) == {"eurusd-snd-ao-qm-short-001", "eurusd-snd-ao-qm-long-001"}, "QM/AO: valid-case matrix drift")
    for case in cases.values():
        require(case["available_closed_bars"] == q["required_timeframes"], f"QM/AO {case['id']}: lookback drift")
        h4, m30, expected = case["h4"], case["m30"], case["expected"]
        require(h4["all_closed"] and m30["all_closed"] and not h4["gap_detected"] and not m30["gap_detected"], f"QM/AO {case['id']}: passing state must be closed/gap-free")
        require(parse_time(h4["latest_closed_time"]) <= parse_time(case["trigger_close_time"]) == parse_time(m30["latest_closed_time"]), f"QM/AO {case['id']}: candle alignment drift")
        require(h4["source_revision"] == m30["source_revision"] == h4["opposing_zone"]["source_revision"], f"QM/AO {case['id']}: source revision drift")
        zone, opposing = h4["zone"], h4["opposing_zone"]
        require(zone["status"] == opposing["status"] == "VALID" and zone["visit_count"] == 0, f"QM/AO {case['id']}: zone validity/freshness drift")
        a, b, head, br = m30["a"], m30["b"], m30["c"], m30["break"]
        require(parse_time(a["confirmed_at"]) < parse_time(b["confirmed_at"]) < parse_time(head["confirmed_at"]) < parse_time(br["close_time"]) == parse_time(case["trigger_close_time"]), f"QM/AO {case['id']}: A-B-C chronology drift")
        signal = expected["signal"]
        require(expected["result"] == "OPPORTUNITY" and expected["confidence"] == q["confidence"] and expected["reason_codes"] == reasons, f"QM/AO {case['id']}: opportunity output drift")
        require(expected["evaluation_key"] == {"strategy_config_version_id": case["strategy_config_version_id"], "pair_id": case["pair_id"], "trigger_time": case["trigger_close_time"]}, f"QM/AO {case['id']}: evaluation key drift")
        entry, stop, target, cost = (parse_decimal(signal[key]) for key in ("entry_price", "stop_loss", "take_profit", "estimated_round_trip_cost"))
        atr = parse_decimal(m30["atr14"])
        require(signal["native_protection_required"] is True and parse_time(signal["signal_expires_at"]) - parse_time(case["trigger_close_time"]) == timedelta(minutes=30) and parse_time(signal["pending_order_expires_at"]) - parse_time(case["trigger_close_time"]) == timedelta(minutes=120), f"QM/AO {case['id']}: protection/expiry drift")
        if expected["direction"] == "SHORT":
            require((h4["regime"], zone["kind"], opposing["kind"], signal["entry_order_type"]) == ("BEARISH_PULLBACK", "SUPPLY", "DEMAND", "SELL_LIMIT"), f"QM/AO {case['id']}: SHORT context drift")
            require(entry == min(parse_decimal(a["open"]), parse_decimal(a["close"])) and parse_decimal(head["pivot_price"]) > parse_decimal(a["pivot_price"]) and parse_decimal(m30["ao"]["second_pivot_value"]) < parse_decimal(m30["ao"]["first_pivot_value"]), f"QM/AO {case['id']}: SHORT entry/divergence drift")
            require(parse_decimal(br["close"]) < parse_decimal(b["pivot_price"]) - atr * Decimal("0.10") and stop > parse_decimal(head["pivot_price"]) and target == parse_decimal(opposing["high"]) - atr * Decimal("0.10"), f"QM/AO {case['id']}: SHORT QM/SL/TP drift")
            planned_loss, planned_reward = stop - entry + cost, entry - target - cost
        else:
            require(expected["direction"] == "LONG" and (h4["regime"], zone["kind"], opposing["kind"], signal["entry_order_type"]) == ("BULLISH_PULLBACK", "DEMAND", "SUPPLY", "BUY_LIMIT"), f"QM/AO {case['id']}: LONG context drift")
            require(entry == min(parse_decimal(a["open"]), parse_decimal(a["close"])) and parse_decimal(head["pivot_price"]) < parse_decimal(a["pivot_price"]) and parse_decimal(m30["ao"]["second_pivot_value"]) > parse_decimal(m30["ao"]["first_pivot_value"]), f"QM/AO {case['id']}: LONG entry/divergence drift")
            require(parse_decimal(br["close"]) > parse_decimal(b["pivot_price"]) + atr * Decimal("0.10") and stop < parse_decimal(head["pivot_price"]) and target == parse_decimal(opposing["low"]) - atr * Decimal("0.10"), f"QM/AO {case['id']}: LONG QM/SL/TP drift")
            planned_loss, planned_reward = entry - stop + cost, target - entry - cost
        require(planned_loss > 0 and planned_reward / planned_loss >= Decimal(signal["minimum_net_reward_risk"]), f"QM/AO {case['id']}: net R:R drift")

    required_mutations = {item["id"] for item in q["mutations"]}
    require(required_mutations == set(q["coverage_contract"]["required_mutation_ids"]), "QM/AO: mutation coverage drift")
    expected_reasons = {"qm-h4-transition-blocks-entry": "H4_REGIME_INELIGIBLE", "qm-zone-invalid-blocks-entry": "H4_ZONE_INVALID", "qm-zone-visited-blocks-entry": "H4_ZONE_NOT_FRESH", "qm-ao-price-difference-below": "M30_AO_PRICE_DIFFERENCE_TOO_SMALL", "qm-ao-difference-below": "M30_AO_DIFFERENCE_TOO_SMALL", "qm-head-outside-zone": "QM_HEAD_OUTSIDE_H4_ZONE", "qm-wick-below": "QM_HEAD_WICK_TOO_SMALL", "qm-break-at-boundary": "QM_STRUCTURE_BREAK_NOT_CONFIRMED", "qm-break-beyond-buffer": None, "qm-target-too-close": "REWARD_RISK_BELOW_MINIMUM", "qm-h4-open-candle": "OPEN_CANDLE_INPUT", "qm-m30-open-candle": "OPEN_CANDLE_INPUT", "qm-h4-future-candle": "FUTURE_CANDLE_INPUT", "qm-m30-gap-detected": "GAP_DETECTED", "qm-source-revision-mismatch": "REVISION_MISMATCH", "qm-m30-short-lookback": "INSUFFICIENT_LOOKBACK", "qm-h4-short-lookback": "INSUFFICIENT_LOOKBACK", "qm-context-invalidates-pending": "PENDING_CONTEXT_INVALIDATED", "qm-event-blackout-cancels-pending": "EVENT_BLACKOUT_ACTIVE", "qm-signal-expired": "SIGNAL_EXPIRED", "qm-pending-expired": "PENDING_ORDER_EXPIRED"}
    require(required_mutations == set(expected_reasons), "QM/AO: required mutation set drift")
    def set_path(root: dict[str, Any], path: str, value: Any) -> None:
        node: Any = root
        parts = path.split(".")
        for key in parts[:-1]:
            require(key in node, f"QM/AO: unknown mutation path {path}")
            node = node[key]
        require(parts[-1] in node, f"QM/AO: unknown mutation path {path}")
        node[parts[-1]] = deepcopy(value)
    for mutation in q["mutations"]:
        require(mutation["base"] in cases, f"QM/AO {mutation['id']}: unknown base")
        changed = deepcopy(cases[mutation["base"]])
        for path, value in mutation.get("set", {}).items(): set_path(changed, path, value)
        reason = expected_reasons[mutation["id"]]
        require(mutation["expected"].get("audit_reason") == reason, f"QM/AO {mutation['id']}: expected reason drift")
        if mutation["id"] == "qm-break-at-boundary": require(parse_decimal(changed["m30"]["break"]["close"]) == parse_decimal(changed["m30"]["b"]["pivot_price"]) - parse_decimal(changed["m30"]["atr14"]) * Decimal("0.10"), "QM/AO: break boundary must be strict")
        if mutation["id"] == "qm-break-beyond-buffer": require(parse_decimal(changed["m30"]["break"]["close"]) < parse_decimal(changed["m30"]["b"]["pivot_price"]) - parse_decimal(changed["m30"]["atr14"]) * Decimal("0.10"), "QM/AO: break pass must exceed strict buffer")
        if mutation["id"] == "qm-target-too-close":
            s = changed["expected"]["signal"]; ratio = (parse_decimal(s["entry_price"]) - parse_decimal(s["take_profit"]) - parse_decimal(s["estimated_round_trip_cost"])) / (parse_decimal(s["stop_loss"]) - parse_decimal(s["entry_price"]) + parse_decimal(s["estimated_round_trip_cost"]))
            require(ratio < Decimal(s["minimum_net_reward_risk"]), "QM/AO: too-close target must fail net R:R")
        if mutation["id"] == "qm-h4-future-candle": require(parse_time(changed["h4"]["latest_closed_time"]) > parse_time(changed["trigger_close_time"]), "QM/AO: future-candle mutation inert")
        if mutation["id"] == "qm-source-revision-mismatch": require(changed["h4"]["source_revision"] != changed["m30"]["source_revision"], "QM/AO: revision mutation inert")
    lifecycle = {item["id"]: item for item in q["execution_lifecycle_cases"]}
    require(set(lifecycle) == set(q["coverage_contract"]["required_execution_ids"]), "QM/AO: lifecycle coverage drift")
    require(lifecycle["qm-pending-created-with-native-protection"]["input"] == {"order_type": "SELL_LIMIT", "native_stop_loss_present": True, "native_take_profit_present": True, "risk_reservation_status": "RESERVED"}, "QM/AO: pending protection create contract drift")
    require(lifecycle["qm-pending-fill-consumes-reservation"]["expected"] == {"risk_reservation_status": "CONSUMED", "position_protection_status": "CONFIRMED"}, "QM/AO: pending fill contract drift")
    require(lifecycle["qm-pending-cancel-releases-reservation"]["expected"] == {"risk_reservation_status": "RELEASED"} and lifecycle["qm-pending-expiry-releases-reservation"]["expected"] == {"risk_reservation_status": "RELEASED"}, "QM/AO: release contract drift")
    require(lifecycle["qm-fill-races-cancel-reconciles"]["expected"] == {"reconciliation_action": "RECORD_FILL_AND_CONSUME_RESERVATION", "retry_policy": "NO_BLIND_RETRY"}, "QM/AO: race reconciliation contract drift")
    require(lifecycle["qm-disconnect-keeps-broker-protection"]["expected"] == {"position_action": "RETAIN_BROKER_NATIVE_PROTECTION", "new_entry_action": "BLOCK"}, "QM/AO: disconnect contract drift")
    return len(cases), len(required_mutations), len(lifecycle)


def main() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    require(data["schema_version"] == 1, "fixture: unsupported schema version")
    contract = data["template_contract"]
    verify_fixture_shape(contract)
    verify_spec(contract, SPEC.read_text(encoding="utf-8"))
    verify_backend(BACKEND.read_text(encoding="utf-8"))
    verify_vectors(data["indicator_vectors"])
    verify_scenario_contract(data)
    run_trend_cases(data)
    run_completeness_cases(data)
    run_wti_cases(data)
    run_execution_exit_cases(data)
    negative_mutation_probe(data)
    qm_cases, qm_mutations, qm_lifecycle = verify_qm_ao_contract()
    print(f"strategy-v0 template and expanded scenario contract validation: PASS; EURUSD QM/AO {qm_cases} valid + {qm_mutations} mutations + {qm_lifecycle} lifecycle cases")


if __name__ == "__main__":
    main()
