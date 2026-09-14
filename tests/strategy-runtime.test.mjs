import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

const run = (script) => execFileSync(process.execPath, ["tests/python.mjs", "-c", script], { encoding: "utf8" });

test("canonical V0 strategy evaluation is deterministic, disabled, and auditable", () => {
  const output = run(`
from decimal import Decimal
from backend.app.strategies import canonical_configs, evaluate_snapshot

configs = canonical_configs("account-a")
assert [(c.template_key, c.pair, c.activation_status) for c in configs] == [
    ("xauusd-trend-pullback-v0", "XAUUSD", "DISABLED"),
    ("usdjpy-trend-pullback-v0", "USDJPY", "DISABLED"),
    ("eurusd-snd-ao-qm-bidirectional-v0", "EURUSD", "DISABLED"),
    ("wti-trend-breakout-v0", "WTI", "DISABLED"),
]
config = configs[0]
snapshot = {
    "account_id": "account-a", "pair": "XAUUSD", "snapshot_id": "snap-1",
    "trigger_time": "2026-01-05T00:00:00Z", "completeness": "COMPLETE",
    "h4": {"close": "110", "ema34": "105", "ema150": "100", "ema150_previous": "99", "atr": "10", "adx": "22"},
    "h1": {"touch": "104", "invalidation_close": "96", "ema34": "100", "atr": "10", "latest_close": "106", "latest_open": "100", "high": "108", "low": "100", "rsi_previous": "49.99", "rsi_latest": "50.00"},
    "m15": {"channel_high": "100", "channel_low": "90", "atr": "10", "spread": "0.10", "open": "100", "high": "106", "low": "99", "close": "106"},
}
first = evaluate_snapshot(snapshot, config)
second = evaluate_snapshot(snapshot, config)
assert first == second
assert first.opportunity and first.opportunity.direction == "LONG"
assert first.opportunity.evaluation_key == {"strategy_config_version_id": config.id, "pair_id": "XAUUSD", "trigger_time": snapshot["trigger_time"]}
assert first.reason_codes == ("H4_TREND_CONFIRMED", "H1_PULLBACK_RECLAIMED", "M15_CONTINUATION_CONFIRMED")
assert evaluate_snapshot({**snapshot, "force_conflict": True}, config).opportunity is None
assert evaluate_snapshot({**snapshot, "force_conflict": True}, config).reason_codes == ("CONFLICTING_SETUPS",)
print("ok")
`);
  assert.match(output, /ok/);
});

test("WTI and EURUSD use their canonical overrides and complete data fails closed", () => {
  const output = run(`
from backend.app.strategies import canonical_configs, evaluate_snapshot

configs = {config.pair: config for config in canonical_configs("account-a")}
wti = {
    "account_id": "account-a", "pair": "WTI", "trigger_time": "2026-01-05T00:00:00Z", "completeness": "COMPLETE",
    "h4": {"close": "110", "ema34": "105", "ema150": "100", "ema150_previous": "99", "atr": "10", "adx": "22"},
    "h1": {"range_low": "90", "range_high": "110", "atr14": "10", "atr6": "6.5", "atr30": "10"},
    "m15": {"open": "110", "close": "116", "atr": "10"},
}
result = evaluate_snapshot(wti, configs["WTI"])
assert result.opportunity and result.opportunity.direction == "LONG" and str(result.opportunity.confidence) == "0.68"
eur = {
    "account_id": "account-a", "pair": "EURUSD", "trigger_time": "2026-01-05T00:00:00Z", "completeness": "COMPLETE",
    "h4": {"regime": "BEARISH_PULLBACK", "zone": {"kind": "SUPPLY", "status": "VALID", "visit_count": 0, "high": "1.11200"}},
    "m30": {"atr14": "0.001", "ao": {"first_pivot_value": "0.0012", "second_pivot_value": "0.0008"}, "a": {"pivot_price": "1.110"}, "b": {"pivot_price": "1.1075"}, "c": {"pivot_price": "1.111"}, "break": {"close": "1.1073"}},
}
assert evaluate_snapshot(eur, configs["EURUSD"]).opportunity.direction == "SHORT"
incomplete = {**wti, "completeness": "INCOMPLETE", "reasons": ["REVISION_MISMATCH"]}
assert evaluate_snapshot(incomplete, configs["WTI"]).reason_codes == ("REVISION_MISMATCH",)
print("ok")
`);
  assert.match(output, /ok/);
});
