import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";

const run = (script) => execFileSync("python3", ["-c", script], { encoding: "utf8" });

test("pre-order is atomic, account-scoped, idempotent, and ordered", () => {
  const output = run(`
from backend.app.execution import ExecutionSubstrate, ExecutionError

engine = ExecutionSubstrate()
created = engine.pre_order(
    account_id="account-a", signal_id="signal-a", idempotency_key="idem-1",
    canonical_hash="hash-1", risk_approved=True, execution_epoch=1,
    order_payload={"symbol": "EURUSD", "volume": "0.10"},
)
assert created.order.account_id == "account-a"
assert created.order.dispatch_sequence == 1
assert created.reservation.status == "ACTIVE"
assert len(engine.outbox("account-a")) == 1
assert engine.pre_order(
    account_id="account-a", signal_id="signal-a", idempotency_key="idem-1",
    canonical_hash="hash-1", risk_approved=True, execution_epoch=1,
    order_payload={"symbol": "EURUSD", "volume": "0.10"},
).order.id == created.order.id
try:
    engine.pre_order(account_id="account-a", signal_id="signal-b", idempotency_key="idem-1",
        canonical_hash="different-hash", risk_approved=True, execution_epoch=1, order_payload={})
except ExecutionError as error:
    assert error.code == "IDEMPOTENCY_KEY_REUSED"
else:
    raise AssertionError("idempotency hash was silently changed")
print("ok")
`);
  assert.match(output, /ok/);
});

test("order_check failure releases reservation and timeout becomes journaled UNKNOWN", () => {
  const output = run(`
from backend.app.execution import ExecutionSubstrate

class Connector:
    def __init__(self, check=True, send="TIMEOUT"):
        self.check = check; self.send = send; self.sent = 0
    def order_check(self, order): return self.check
    def order_send(self, order): self.sent += 1; return self.send
    def journal(self, order): return None
    def broker_state(self, order): return {"status": "FILLED", "external_id": "mt5-1"}

engine = ExecutionSubstrate()
failed = engine.pre_order(account_id="a", signal_id="s1", idempotency_key="i1", canonical_hash="h1",
    risk_approved=True, execution_epoch=1, order_payload={})
engine.dispatch_next("a", Connector(check=False))
assert failed.reservation.status == "RELEASED"
unknown = engine.pre_order(account_id="a", signal_id="s2", idempotency_key="i2", canonical_hash="h2",
    risk_approved=True, execution_epoch=1, order_payload={})
connector = Connector()
result = engine.dispatch_next("a", connector)
assert result.status == "UNKNOWN" and connector.sent == 1
assert engine.recover("a", unknown.order.id, connector).status == "FILLED"
assert connector.sent == 1
print("ok")
`);
  assert.match(output, /ok/);
});

test("fill without native protection quarantines exposure and emergency fence blocks entries", () => {
  const output = run(`
from backend.app.execution import ExecutionSubstrate, ExecutionError

engine = ExecutionSubstrate()
entry = engine.pre_order(account_id="a", signal_id="s", idempotency_key="i", canonical_hash="h",
    risk_approved=True, execution_epoch=1, order_payload={})
engine.record_fill("a", entry.order.id, "deal-1", "0.10", native_protection_confirmed=False)
assert engine.account("a").exposure_gate == "QUARANTINED"
assert engine.position("a", entry.order.id).protection_status == "UNCONFIRMED"
fence = engine.install_fence("a", "EMERGENCY_STOP")
assert fence.status == "FENCE_PENDING"
try:
    engine.pre_order(account_id="a", signal_id="s2", idempotency_key="i2", canonical_hash="h2",
        risk_approved=True, execution_epoch=1, order_payload={})
except ExecutionError as error:
    assert error.code == "EXPOSURE_GATE_CLOSED"
else:
    raise AssertionError("entry crossed emergency fence")
print("ok")
`);
  assert.match(output, /ok/);
});

test("execution migration contains durable account-local safety records", () => {
  const migration = readFileSync("backend/migrations/006_execution_safety.sql", "utf8");
  for (const phrase of ["risk_reservations", "order_intents", "outbox_events", "connector_journal", "idempotency_key", "dispatch_sequence", "execution_epoch", "UNIQUE (broker_account_id, dispatch_sequence)"]) {
    assert.match(migration, new RegExp(phrase.replace(/[()]/g, "\\$&")));
  }
});
