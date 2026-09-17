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
    engine.execute_signal(account_id="b", signal_id="s", idempotency_key="e1", reason="go", confirmed=True, signal_revision=1, signal_fresh=True, fence_safe=True, account_state="RUNNING", live_lock=True, execution_epoch=1, order_payload={"stop_loss":"1", "take_profit":["2"]})
except ExecutionError as error:
    assert error.code == "SIGNAL_APPROVAL_REQUIRED"
else: raise AssertionError("approval crossed account boundary")
print("ok")
`);
  assert.match(output, /ok/);
});

test("ExecutionSubstrate requires a fresh account-local PRE_ORDER assessment", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionError, ExecutionSubstrate
from backend.app.risk_calendar import RiskAssessment

now = datetime.now(timezone.utc)
payload = {"stop_loss": "1", "take_profit": ["2"], "signal_revision": 1}
engine = ExecutionSubstrate()
engine.approve_signal(account_id="account-a", signal_id="manual", idempotency_key="approve-manual", reason="reviewed", confirmed=True, signal_revision=1)
try:
    engine.execute_signal(
        account_id="account-a", signal_id="manual", idempotency_key="missing-risk",
        reason="execute", confirmed=True, signal_revision=1,
        signal_fresh=True, fence_safe=True, account_state="RUNNING", live_lock=True,
        execution_epoch=1, order_payload=payload,
    )
except ExecutionError as error: assert error.code == "PRE_ORDER_ASSESSMENT_REQUIRED"
else: raise AssertionError("manual execution accepted no PRE_ORDER assessment")

initial = RiskAssessment("account-a", 1, True, purpose="INITIAL", assessed_at=now, valid_until=now + timedelta(seconds=20), signal_revision=1, signal_id="manual")
try:
    engine.execute_signal(
        account_id="account-a", signal_id="manual", idempotency_key="initial-risk",
        reason="execute", confirmed=True, signal_revision=1,
        risk_assessment=initial, signal_fresh=True, fence_safe=True, account_state="RUNNING",
        live_lock=True, execution_epoch=1, order_payload=payload,
    )
except ExecutionError as error: assert error.code == "PRE_ORDER_ASSESSMENT_REQUIRED"
else: raise AssertionError("INITIAL assessment created a manual order")

foreign = RiskAssessment("account-b", 1, True, purpose="PRE_ORDER", assessed_at=now, valid_until=now + timedelta(seconds=20), signal_revision=1, signal_id="semi")
try:
    engine.schedule_automated_signal(
        account_id="account-a", signal_id="semi", idempotency_key="foreign-risk",
        mode="SEMI_AUTO", signal_created_at=now, signal_revision=1, signal_eligible=True,
        signal_approved=True, mode_changed_at=now,
        risk_assessment=foreign, signal_fresh=True, fence_safe=True, account_state="RUNNING",
        live_lock=True, execution_epoch=1, order_payload=payload,
    )
except ExecutionError as error: assert error.code == "RISK_ASSESSMENT_ACCOUNT_MISMATCH"
else: raise AssertionError("foreign assessment created a semi-auto order")
assert not engine.orders and not engine.reservations
print("ok")
`);
  assert.match(output, /ok/);
});

test("execute requires every gate and broker-native protection", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone
from backend.app.execution import ExecutionError, ExecutionSubstrate
from backend.app.risk_calendar import RiskAssessment
engine = ExecutionSubstrate()
engine.approve_signal(account_id="a", signal_id="s", idempotency_key="a1", reason="reviewed", confirmed=True, signal_revision=1)
now = datetime.now(timezone.utc)
assessment = RiskAssessment("a", 1, True, purpose="PRE_ORDER", assessed_at=now, valid_until=now + timedelta(seconds=20), signal_revision=1, signal_id="s")
kwargs = dict(account_id="a", signal_id="s", idempotency_key="e1", reason="confirmed execution", confirmed=True, signal_revision=1, risk_assessment=assessment, signal_fresh=True, fence_safe=True, account_state="RUNNING", live_lock=True, execution_epoch=1)
try: engine.execute_signal(**kwargs, order_payload={})
except ExecutionError as error: assert error.code == "NATIVE_PROTECTION_REQUIRED"
else: raise AssertionError("unprotected entry accepted")
result = engine.execute_signal(**kwargs, order_payload={"stop_loss":"1", "take_profit":["2"], "signal_revision":1})
assert result.order.status == "INTENT" and result.order.payload["stop_loss"] == "1"
try:
    engine.execute_signal(
        **kwargs, risk_amount="1",
        order_payload={"stop_loss":"1", "take_profit":["2"], "signal_revision":1},
    )
except ExecutionError as error: assert error.code == "IDEMPOTENCY_KEY_REUSED"
else: raise AssertionError("changed risk amount reused the prior order")
print("ok")
`);
  assert.match(output, /ok/);
});
