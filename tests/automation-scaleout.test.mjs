import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";

const run = (script) => execFileSync(process.execPath, ["tests/python.mjs", "-c", script], { encoding: "utf8" });

test("automation mode is account-local and mode changes fence older Signals", () => {
  const output = run(`
from datetime import datetime, timezone, timedelta
from backend.app.broker_accounts import AccountRegistry
from backend.app.execution import ExecutionError, ExecutionSubstrate
from backend.app.risk_calendar import RiskAssessment

at = datetime(2026, 1, 1, tzinfo=timezone.utc)
now = datetime.now(timezone.utc)
registry = AccountRegistry()
accounts = [registry.register(provider="mt5", broker_server="demo", external_account_id=str(i), display_name=str(i), environment="DEMO") for i in range(3)]
for account in accounts:
    registry.set_execution_mode(account.id, "FULL_AUTO", now=at)
    account.bot_state = "RUNNING"
engine = ExecutionSubstrate()
payload = {"stop_loss": "1", "take_profit": ["2"], "signal_revision": 1}
def assessment(account_id, signal_id):
    return RiskAssessment(account_id, 1, True, purpose="PRE_ORDER", assessed_at=now, valid_until=now + timedelta(seconds=20), signal_revision=1, signal_id=signal_id)
old = at - timedelta(seconds=1)
try:
    engine.schedule_automated_signal(account_id=accounts[0].id, signal_id="old", idempotency_key="old", mode="FULL_AUTO", signal_created_at=old, signal_revision=1, signal_eligible=True, signal_approved=False, mode_changed_at=at, signal_fresh=True, fence_safe=True, account_state="RUNNING", live_lock=True, execution_epoch=1, order_payload=payload)
except ExecutionError as error: assert error.code == "SIGNAL_PRECEDES_MODE_CHANGE"
else: raise AssertionError("old Signal was auto-scheduled after mode change")
for number, account in enumerate(accounts):
    result = engine.schedule_automated_signal(account_id=account.id, signal_id=f"s-{number}", idempotency_key=f"i-{number}", mode="FULL_AUTO", signal_created_at=at, signal_revision=1, signal_eligible=True, signal_approved=False, mode_changed_at=at, risk_assessment=assessment(account.id, f"s-{number}"), signal_fresh=True, fence_safe=True, account_state="RUNNING", live_lock=True, execution_epoch=1, order_payload=payload)
    assert result.order.account_id == account.id and result.order.dispatch_sequence == 1
assert [len(engine.outbox(account.id)) for account in accounts] == [1, 1, 1]
print("ok")
`);
  assert.match(output, /ok/);
});

test("global emergency freezes a durable target set until every target converges", () => {
  const output = run(`
from backend.app.execution import ExecutionError, ExecutionSubstrate
engine = ExecutionSubstrate()
operation = engine.begin_global_emergency(["a", "b", "c"], kind="CLOSE_ALL")
assert operation.status == "INCOMPLETE"
assert operation.target_account_ids == ("a", "b", "c")
assert all(engine.account(account).exposure_gate == "FENCE_PENDING" for account in operation.target_account_ids)
engine.converge_global_target(operation.id, "a", resolved=True)
engine.converge_global_target(operation.id, "b", resolved=False, detail="connector offline")
assert operation.status == "INCOMPLETE" and operation.targets["b"].status == "UNRESOLVED"
engine.converge_global_target(operation.id, "c", resolved=True)
assert operation.status == "INCOMPLETE"
engine.converge_global_target(operation.id, "b", resolved=True)
assert operation.status == "COMPLETE"
try: engine.converge_global_target(operation.id, "other", resolved=True)
except ExecutionError as error: assert error.code == "GLOBAL_TARGET_NOT_FOUND"
else: raise AssertionError("unknown account entered global target set")
print("ok")
`);
  assert.match(output, /ok/);
});

test("automation and emergency migration preserve account-scoped durable records", () => {
  const migration = readFileSync("backend/migrations/008_automation_emergency.sql", "utf8");
  for (const phrase of ["execution_mode_revision", "mode_changed_at", "global_emergency_operations", "global_emergency_targets", "broker_account_id", "INCOMPLETE", "UNRESOLVED"]) {
    assert.match(migration, new RegExp(phrase));
  }
});
