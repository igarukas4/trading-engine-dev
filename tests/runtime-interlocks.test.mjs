import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";

const run = (script) => execFileSync(process.execPath, ["tests/python.mjs", "-c", script], { encoding: "utf8" });

test("protection repair is bounded and account-local", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionCoordinator
from backend.app.risk_calendar import RiskAssessment

now = datetime(2026, 1, 1, tzinfo=timezone.utc)
def assessment(account_id, signal_id):
    return RiskAssessment(
        account_id, 1, True, purpose="PRE_ORDER", assessed_at=now,
        valid_until=now + timedelta(seconds=30), signal_revision=1, signal_id=signal_id,
    )

def order(engine, account_id, signal_id):
    return engine.accept_execution(
        account_id=account_id, signal_id=signal_id, idempotency_key=signal_id,
        canonical_hash=signal_id, execution_epoch=engine.account(account_id).execution_epoch,
        risk_assessment=assessment(account_id, signal_id),
        signal_revision=1, order_payload={"symbol": "EURUSD", "volume": "1", "stop_loss": "90", "take_profit": ["110"]},
        now=now,
    )

class RepairingBroker:
    def modify_position_protection(self, position, stop_loss):
        return {"status": "CONFIRMED", "stop_loss": stop_loss}
    def position_protection(self, position):
        return {"status": "CONFIRMED", "stop_loss": "90"}

class FailingBroker:
    def __init__(self): self.attempts = 0
    def modify_position_protection(self, position, stop_loss):
        self.attempts += 1
        return {"status": "REJECTED"}
    def position_protection(self, position):
        return {"status": "UNCONFIRMED"}

engine = ExecutionCoordinator(max_protection_repair_attempts=2)
unsafe = order(engine, "account-a", "signal-a")
healthy = order(engine, "account-b", "signal-b")
engine.record_fill("account-a", unsafe.order.id, "deal-a", "1", native_protection_confirmed=False)
assert engine.runtime_interlock("account-a").status == "BLOCKED"
assert engine.runtime_interlock("account-b").status == "ELIGIBLE"
repaired = engine.repair_native_protection("account-a", unsafe.order.id, RepairingBroker(), now=now)
assert repaired.status == "RECOVERED"
assert engine.runtime_interlock("account-a").status == "ELIGIBLE"
engine.record_fill("account-b", healthy.order.id, "deal-b", "1", native_protection_confirmed=True)
failing = order(engine, "account-a", "signal-c")
engine.record_fill("account-a", failing.order.id, "deal-c", "1", native_protection_confirmed=False)
broker = FailingBroker()
quarantined = engine.repair_native_protection("account-a", failing.order.id, broker, now=now)
assert quarantined.status == "QUARANTINED"
assert broker.attempts == 2
assert engine.runtime_interlock("account-a").status == "QUARANTINED"
assert engine.critical_alerts("account-a")[0]["reason_code"] == "NATIVE_PROTECTION_UNVERIFIED"
assert engine.runtime_interlock("account-b").status == "ELIGIBLE"
print("ok")
`);
  assert.match(output, /ok/);
});

test("runtime health interlocks recover per account and quarantine stale operations", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionCoordinator, ExecutionError

now = datetime(2026, 1, 1, tzinfo=timezone.utc)
engine = ExecutionCoordinator()
blocked = engine.observe_runtime_health("account-a", connector_healthy=False)
assert blocked.status == "BLOCKED"
assert "CONNECTOR_UNAVAILABLE" in blocked.reasons
assert engine.runtime_interlock("account-b").status == "ELIGIBLE"
try:
    engine.accept_execution
except AttributeError:
    raise AssertionError("public coordinator seam disappeared")
recovered = engine.observe_runtime_health(
    "account-a", connector_healthy=True, connector_gap=False,
    broker_facts_fresh=True, reconciliation_healthy=True,
    risk_state_known=True, reservation_consistent=True, ordering_safe=True,
    now=now,
)
assert recovered.status == "ELIGIBLE"
quarantined = engine.observe_runtime_health(
    "account-a", backup_observed_at=now - timedelta(minutes=20),
    now=now, freshness_window=timedelta(minutes=5),
    escalation_deadline=timedelta(minutes=15),
)
assert quarantined.status == "QUARANTINED"
try:
    engine.recover_runtime_interlock("account-a", evidence={"broker_reconciled": True})
except ExecutionError as error:
    assert error.code == "QUARANTINE_RECOVERY_COMMAND_REQUIRED"
else:
    raise AssertionError("persistent quarantine cleared without custodian command")
assert engine.recover_runtime_interlock(
    "account-a", evidence={"broker_reconciled": True}, custodian_command=True,
).status == "ELIGIBLE"
assert engine.critical_alerts("account-a") == []
assert engine.runtime_interlock("account-b").status == "ELIGIBLE"
print("ok")
`);
  assert.match(output, /ok/);
});

test("calendar interlocks preserve known scope and fail closed when scope is unknown", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionCoordinator, ExecutionError
from backend.app.risk_calendar import RiskAssessment

now = datetime(2026, 1, 1, tzinfo=timezone.utc)
def assessment(account_id, signal_id):
    return RiskAssessment(account_id, 1, True, purpose="PRE_ORDER", assessed_at=now,
        valid_until=now + timedelta(minutes=5), signal_revision=1, signal_id=signal_id)
def accept(engine, account_id, signal_id, pair, at=now):
    return engine.accept_execution(
        account_id=account_id, signal_id=signal_id, idempotency_key=signal_id,
        canonical_hash=signal_id, execution_epoch=engine.account(account_id).execution_epoch,
        risk_assessment=assessment(account_id, signal_id), signal_revision=1,
        order_payload={"symbol": pair, "volume": "1", "stop_loss": "90", "take_profit": ["110"]}, now=at)

engine = ExecutionCoordinator()
engine.set_calendar_interlock("account-a", pair="EURUSD", currencies=("EUR", "USD"), scope_known=True)
try:
    accept(engine, "account-a", "blocked", "EURUSD")
except ExecutionError as error:
    assert error.code == "CALENDAR_BLACKOUT_ACTIVE"
else:
    raise AssertionError("affected Pair crossed calendar blackout")
assert accept(engine, "account-a", "unaffected", "GBPJPY").order.account_id == "account-a"
assert accept(engine, "account-b", "healthy", "EURUSD").order.account_id == "account-b"
override = engine.add_manual_economic_event_override(
    "account-a", pair="EURUSD", currencies=("EUR",),
    blackout_start=now, blackout_end=now + timedelta(minutes=5), reason="operator event")
extended = engine.add_manual_economic_event_override(
    "account-a", override_id=override["id"], pair="EURUSD", currencies=("EUR",),
    blackout_start=now + timedelta(minutes=1), blackout_end=now + timedelta(minutes=10), reason="operator event")
assert extended["blackout_start"] == now.isoformat()
assert extended["blackout_end"] == (now + timedelta(minutes=10)).isoformat()
try:
    engine.add_manual_economic_event_override(
        "account-b", override_id=override["id"], pair="EURUSD", currencies=("EUR",),
        blackout_start=now, blackout_end=now + timedelta(minutes=5), reason="wrong account")
except ExecutionError as error:
    assert error.code == "WRONG_ACCOUNT"
else:
    raise AssertionError("calendar override crossed account scope")
engine.set_calendar_interlock("account-a", scope_known=False)
assert engine.runtime_interlock("account-a").status == "BLOCKED"
try:
    accept(engine, "account-a", "unknown-scope", "GBPJPY")
except ExecutionError as error:
    assert error.code == "CALENDAR_BLACKOUT_ACTIVE"
else:
    raise AssertionError("unknown calendar scope allowed entry")
engine.set_calendar_interlock(
    "account-a", blackout=True, scope_known=False,
    blackout_start=now, blackout_end=now + timedelta(minutes=1), now=now,
)
assert accept(
    engine, "account-a", "expired-scope", "GBPJPY", now + timedelta(minutes=2),
).order.account_id == "account-a"
assert engine.runtime_interlock("account-a").status == "ELIGIBLE"
assert engine.runtime_interlock("account-b").status == "ELIGIBLE"
engine.set_calendar_interlock(
    "account-a", scope_known=False,
    blackout_start=datetime(2026, 1, 1, 0, 5),
    blackout_end=datetime(2026, 1, 1, 0, 10),
    now=now,
)
assert accept(
    engine, "account-a", "before-future-blackout", "GBPJPY", now,
).order.account_id == "account-a"
print("ok")
`);
  assert.match(output, /ok/);
});

test("runtime interlock persistence keeps safety state account-local", () => {
  const migration = readFileSync("backend/migrations/011_runtime_interlocks.sql", "utf8");
  for (const term of [
    "runtime_interlocks", "runtime_interlock_alerts", "protection_repairs",
    "calendar_blackouts", "manual_economic_event_overrides",
    "requires_custodian_command", "broker_account_id",
  ]) assert.match(migration, new RegExp(term));

  const output = run(`
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from backend.app.execution import ExecutionCoordinator

with TemporaryDirectory() as directory:
    path = f"{directory}/execution.json"
    engine = ExecutionCoordinator(state_path=path)
    engine.observe_runtime_health("account-a", connector_gap=True)
    reloaded = ExecutionCoordinator(state_path=path)
    decision = reloaded.runtime_interlock("account-a")
    assert decision.status == "BLOCKED"
    assert decision.reasons == ("CONNECTOR_GAP",)
    assert reloaded.runtime_interlock("account-b").status == "ELIGIBLE"
print("ok")
`);
  assert.match(output, /ok/);
});

test("reconciliation treats a changed native StopLoss as unconfirmed", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionCoordinator
from backend.app.risk_calendar import RiskAssessment

now = datetime(2026, 1, 1, tzinfo=timezone.utc)
assessment = RiskAssessment("account-a", 1, True, purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(seconds=30), signal_revision=1, signal_id="signal")
engine = ExecutionCoordinator(max_protection_repair_attempts=1)
entry = engine.accept_execution(account_id="account-a", signal_id="signal", idempotency_key="signal",
    canonical_hash="signal", execution_epoch=1, risk_assessment=assessment, signal_revision=1,
    order_payload={"symbol": "EURUSD", "volume": "1", "stop_loss": "90", "take_profit": ["110"]}, now=now)
engine.record_fill("account-a", entry.order.id, "deal", "1", native_protection_confirmed=True)
engine.reconcile_observation("account-a", {"positions": [{
    "position_id": entry.order.id, "order_id": entry.order.id, "symbol": "EURUSD",
    "side": "BUY", "volume": "1", "sl": "90", "tp": "110",
}]})
engine.reconcile_observation("account-a", {"positions": [{
    "position_id": entry.order.id, "order_id": entry.order.id, "symbol": "EURUSD",
    "side": "BUY", "volume": "1", "sl": "89", "tp": "110",
}]})
assert engine.position("account-a", entry.order.id).protection_status == "UNCONFIRMED"
assert engine.runtime_interlock("account-a").status == "BLOCKED"
class Broker:
    def modify_position_protection(self, position, stop_loss):
        assert stop_loss == "90"
        return {"status": "CONFIRMED", "stop_loss": "90"}
    def position_protection(self, position):
        return {"status": "CONFIRMED", "stop_loss": "90"}
assert engine.repair_native_protection("account-a", entry.order.id, Broker(), now=now).status == "RECOVERED"
assert engine.position("account-a", entry.order.id).protection_status == "CONFIRMED"
print("ok")
`);
  assert.match(output, /ok/);
});

test("uncertain risk, reservation state, and ordering health hold only one account", () => {
  const output = run(`
from datetime import datetime, timezone

from backend.app.execution import ExecutionCoordinator, ExecutionError

engine = ExecutionCoordinator()
for kwargs, reason in [
    ({"risk_state_known": False}, "RISK_STATE_UNCERTAIN"),
    ({"reservation_consistent": False}, "RESERVATION_INCONSISTENT"),
    ({"ordering_safe": False}, "ORDERING_OVERLOAD"),
]:
    decision = engine.observe_runtime_health("account-a", **kwargs)
    assert decision.status == "BLOCKED" and reason in decision.reasons
    assert engine.runtime_interlock("account-b").status == "ELIGIBLE"
    engine.observe_runtime_health(
        "account-a", risk_state_known=True, reservation_consistent=True,
        ordering_safe=True, now=datetime.now(timezone.utc),
    )
    assert engine.runtime_interlock("account-a").status == "ELIGIBLE"

assert engine.runtime_interlock("account-a").status == "ELIGIBLE"
print("ok")
`);
  assert.match(output, /ok/);
});

test("dashboard exposes account-local interlock and protection evidence", () => {
  const backend = readFileSync("backend/app/main.py", "utf8");
  const system = readFileSync("frontend/app/system/page.tsx", "utf8");
  for (const term of [
    "runtime-interlock", "runtime_interlock", "critical_alerts", "protection",
    "event.blackout.changed", "custodian_command",
  ]) assert.match(backend, new RegExp(term.replace(/[.-]/g, "\\$&")));
  for (const term of ["Runtime interlock", "Interlock reasons", "Protection", "Critical alerts", "execution.interlock"]) {
    assert.match(system, new RegExp(term));
  }
});
