import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

const run = (script) => execFileSync(process.execPath, ["tests/python.mjs", "-c", script], { encoding: "utf8" });

test("manual approval is separate, explicit, account-scoped, and idempotent", () => {
  const output = run(`
from backend.app.execution import ExecutionError, ExecutionSubstrate
engine = ExecutionSubstrate()
try:
    engine.approve_signal(account_id="a", signal_id="s", idempotency_key="a1", reason="", confirmed=True, signal_revision=1)
except ExecutionError as error:
    assert error.code == "OPERATOR_REASON_REQUIRED"
else: raise AssertionError("empty reason accepted")
try:
    engine.approve_signal(account_id="a", signal_id="s", idempotency_key="a2", reason="manual review", confirmed=False, signal_revision=1)
except ExecutionError as error:
    assert error.code == "OPERATOR_CONFIRMATION_REQUIRED"
else: raise AssertionError("missing confirmation accepted")
approved = engine.approve_signal(account_id="a", signal_id="s", idempotency_key="a3", reason="manual review", confirmed=True, signal_revision=1)
assert approved.status == "ACCEPTED"
assert engine.approve_signal(account_id="a", signal_id="s", idempotency_key="a3", reason="manual review", confirmed=True, signal_revision=1).id == approved.id
try:
    engine.execute_signal(account_id="b", signal_id="s", idempotency_key="e1", reason="go", confirmed=True, signal_revision=1, risk_approved=True, signal_fresh=True, fence_safe=True, account_state="RUNNING", live_lock=True, execution_epoch=1, order_payload={"stop_loss":"1", "take_profit":["2"]})
except ExecutionError as error:
    assert error.code == "SIGNAL_APPROVAL_REQUIRED"
else: raise AssertionError("approval crossed account boundary")
print("ok")
`);
  assert.match(output, /ok/);
});

test("execute requires every gate and broker-native protection", () => {
  const output = run(`
from backend.app.execution import ExecutionError, ExecutionSubstrate
engine = ExecutionSubstrate()
engine.approve_signal(account_id="a", signal_id="s", idempotency_key="a1", reason="reviewed", confirmed=True, signal_revision=1)
kwargs = dict(account_id="a", signal_id="s", idempotency_key="e1", reason="confirmed execution", confirmed=True, signal_revision=1, risk_approved=True, signal_fresh=True, fence_safe=True, account_state="RUNNING", live_lock=True, execution_epoch=1)
try: engine.execute_signal(**kwargs, order_payload={})
except ExecutionError as error: assert error.code == "NATIVE_PROTECTION_REQUIRED"
else: raise AssertionError("unprotected entry accepted")
result = engine.execute_signal(**kwargs, order_payload={"stop_loss":"1", "take_profit":["2"], "signal_revision":1})
assert result.order.status == "INTENT" and result.order.payload["stop_loss"] == "1"
print("ok")
`);
  assert.match(output, /ok/);
});
