import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

const run = (script) => execFileSync(process.execPath, ["tests/python.mjs", "-c", script], { encoding: "utf8" });

test("Signal enrichment is immutable, account-scoped, expiring, and explainable", () => {
  const output = run(`
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from backend.app.signals import SignalStore
from backend.app.risk_calendar import RiskEngine, RiskLimits

at = datetime(2026, 1, 5, tzinfo=timezone.utc)
store = SignalStore()
signal = store.create(
    account_id="account-a",
    opportunity={
        "id": "opp-1", "account_id": "account-a", "pair": "XAUUSD",
        "strategy_config_version_id": "config-a-v1", "direction": "LONG",
        "confidence": "0.70", "evaluation_key": {"trigger_time": at.isoformat()},
    },
    market_snapshot_id="snapshot-a-1", policy_version=2,
    created_at=at, ttl=timedelta(minutes=30),
    entry_zone={"price": "2000"}, stop_loss="1990", take_profit=("2020",),
)
assert signal.account_id == signal.opportunity["account_id"] == "account-a"
assert signal.expires_at == at + timedelta(minutes=30)
assert signal.status == "BLOCKED_RISK"
assert "RISK_LIMITS_MISSING" in signal.reason_codes
assert store.get(signal.id).as_dict()["risk_assessment"]["purpose"] == "INITIAL"
assert store.create_revision(signal.id, policy_version=3, created_at=at + timedelta(minutes=1)).supersedes_signal_id == signal.id
assert store.get(signal.id).status == "INVALIDATED"
print("ok")
`);
  assert.match(output, /ok/);
});

test("pre-order risk codes are deterministic and stale approval cannot be reused", () => {
  const output = run(`
from datetime import datetime, timezone
from decimal import Decimal
from backend.app.risk_calendar import RiskEngine, RiskLimits

limits = RiskLimits(broker_account_id="account-a", baseline_minimum_samples=20)
engine = RiskEngine()
assessment = engine.assess(
    "account-a", limits, purpose="PRE_ORDER", now=datetime(2026, 1, 5, tzinfo=timezone.utc),
    signal_expires_at=datetime(2026, 1, 5, 0, 30, tzinfo=timezone.utc),
    baseline_samples=20, spread_multiple=Decimal("2.1"), volatility_multiple=Decimal("2.6"),
    policy_healthy=False, calendar_blackout=True, account_state="STOPPED",
    signal_revision=2, approved_revision=1,
)
assert assessment.approved is False
assert assessment.reason_codes == (
    "SIGNAL_REVISION_CHANGED", "POLICY_HEALTH_UNSAFE",
    "CALENDAR_BLACKOUT_ACTIVE", "SPREAD_LIMIT_EXCEEDED", "VOLATILITY_LIMIT_EXCEEDED",
    "ACCOUNT_STATE_UNSAFE",
)
print("ok")
`);
  assert.match(output, /ok/);
});

test("SignalStore rejects cross-account references and freezes its evidence", () => {
  const output = run(`
from datetime import datetime, timezone
from backend.app.signals import SignalStore
from backend.app.risk_calendar import RiskLimits

at = datetime(2026, 1, 5, tzinfo=timezone.utc)
store = SignalStore()
opportunity = {
    "id": "opp-immutable", "account_id": "account-a", "pair": "EURUSD",
    "strategy_config_version_id": "config-account-a-v1",
    "market_snapshot_id": "snapshot-account-a",
}
signal = store.create(
    account_id="account-a", opportunity=opportunity,
    market_snapshot_id="snapshot-account-a", policy_version=1, created_at=at,
    limits=RiskLimits(broker_account_id="account-a"),
)
opportunity["pair"] = "XAUUSD"
assert signal.opportunity["pair"] == "EURUSD"
try:
    store.create(account_id="account-a", opportunity={**opportunity, "account_id": "account-b"},
        market_snapshot_id="snapshot-account-a", policy_version=1, created_at=at)
except ValueError as error:
    assert str(error) == "ACCOUNT_CONTEXT_MISMATCH"
else:
    raise AssertionError("cross-account opportunity accepted")
try:
    store.create(account_id="account-a", opportunity={**opportunity, "strategy_config_version_id": "config-account-b-v1"},
        market_snapshot_id="snapshot-account-a", policy_version=1, created_at=at)
except ValueError as error:
    assert str(error) == "CONFIG_CONTEXT_MISMATCH"
else:
    raise AssertionError("cross-account config accepted")
print("ok")
`);
  assert.match(output, /ok/);
});

test("Signal revisions reassess preserved risk context and invalidate approval", () => {
  const output = run(`
from datetime import datetime, timezone
from backend.app.signals import SignalStore
from backend.app.risk_calendar import RiskLimits

at = datetime(2026, 1, 5, tzinfo=timezone.utc)
limits = RiskLimits(broker_account_id="account-a")
store = SignalStore()
signal = store.create(account_id="account-a", opportunity={
    "id": "opp-revision", "account_id": "account-a", "strategy_config_version_id": "config-account-a-v1",
    "market_snapshot_id": "snapshot-account-a",
}, market_snapshot_id="snapshot-account-a", policy_version=1, created_at=at,
    limits=limits, risk_kwargs={"baseline_samples": 20})
revised = store.create_revision(signal.id, policy_version=2, created_at=at, limits=limits)
assert store.get(signal.id).status == "INVALIDATED"
assert revised.revision == 2
assert revised.risk_assessment.purpose == "INITIAL"
assert revised.risk_assessment.signal_revision == 2
assert revised.risk_assessment.risk_limits_version == 1
print("ok")
`);
  assert.match(output, /ok/);
});
