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

test("protective position stages exits only on confirmed fills and preserves rounding residual", () => {
  const output = run(`
from backend.app.execution import ExecutionSubstrate, ExecutionError

engine = ExecutionSubstrate()
entry = engine.pre_order(account_id="a", signal_id="s", idempotency_key="i", canonical_hash="h",
    risk_approved=True, execution_epoch=1,
    order_payload={"symbol": "EURUSD", "volume": "0.38", "side": "BUY", "stop_loss": "1.09000", "take_profit": "1.11000"})
engine.record_fill("a", entry.order.id, "deal-entry", "0.38", native_protection_confirmed=True)
position = engine.position("a", entry.order.id)
assert position.remaining_volume == "0.38"
try:
    engine.stage_exit("a", entry.order.id, "TP1")
except ExecutionError as error:
    assert error.code == "EXIT_FILL_NOT_CONFIRMED"
else:
    raise AssertionError("target crossing created an exit")
engine.record_exit_fill("a", entry.order.id, "deal-tp1", "TP1", "0.15")
assert engine.position("a", entry.order.id).stage == "TP1_CONFIRMED"
engine.record_exit_fill("a", entry.order.id, "deal-tp2", "TP2", "0.10")
position = engine.position("a", entry.order.id)
assert position.stage == "TP2_CONFIRMED"
assert position.remaining_volume == "0.13"
assert position.runner_volume == "0.13"
assert all(command.reduce_only for command in engine.position_commands)
try:
    engine.record_exit_fill("a", entry.order.id, "deal-too-much", "RUNNER", "0.14")
except ExecutionError as error:
    assert error.code == "REDUCTION_EXCEEDS_EXPOSURE"
else:
    raise AssertionError("reduction crossed zero")
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

test("trailing is monotonic, closed-candle gated, and capability safe", () => {
  const output = run(`
from backend.app.execution import ExecutionSubstrate, ExecutionError

engine = ExecutionSubstrate()
entry = engine.pre_order(account_id="a", signal_id="s", idempotency_key="i", canonical_hash="h",
    risk_approved=True, execution_epoch=1,
    order_payload={"volume": "1", "stop_loss": "90", "take_profit": "110"})
engine.record_fill("a", entry.order.id, "entry", "1", native_protection_confirmed=True)
engine.record_exit_fill("a", entry.order.id, "tp1", "TP1", "0.4")
engine.record_exit_fill("a", entry.order.id, "tp2", "TP2", "0.3")
try:
    engine.request_trailing("a", entry.order.id, "95", direction="LONG", closed_candle=False, atomic_capability=True)
except ExecutionError as error:
    assert error.code == "TRAIL_WAITING_FOR_CLOSED_CANDLE"
else:
    raise AssertionError("trailing used an open candle")
assert engine.request_trailing("a", entry.order.id, "95", direction="LONG", closed_candle=True, atomic_capability=False) is None
command = engine.request_trailing("a", entry.order.id, "96", direction="LONG", closed_candle=True, atomic_capability=True)
engine.confirm_trailing("a", entry.order.id, command.id, "96")
try:
    engine.request_trailing("a", entry.order.id, "95", direction="LONG", closed_candle=True, atomic_capability=True)
except ExecutionError as error:
    assert error.code == "TRAIL_NOT_TIGHTER"
else:
    raise AssertionError("trailing loosened protection")
engine.mark_position_command_unknown("a", command.id)
assert engine.account("a").exposure_gate == "QUARANTINED"
print("ok")
`);
  assert.match(output, /ok/);
});

test("partial entry fills accumulate in one account-scoped position", () => {
  const output = run(`
from backend.app.execution import ExecutionSubstrate

engine = ExecutionSubstrate()
entry = engine.pre_order(account_id="a", signal_id="s", idempotency_key="i", canonical_hash="h",
    risk_approved=True, execution_epoch=1, order_payload={"volume": "1", "stop_loss": "90", "take_profit": "110"})
engine.record_fill("a", entry.order.id, "deal-1", "0.4", native_protection_confirmed=True)
engine.record_fill("a", entry.order.id, "deal-2", "0.6", native_protection_confirmed=True)
position = engine.position("a", entry.order.id)
assert position.volume == "1"
assert position.remaining_volume == "1"
assert position.protection_status == "CONFIRMED"
print("ok")
`);
  assert.match(output, /ok/);
});

test("fill idempotency is scoped to the broker account", () => {
  const output = run(`
from backend.app.execution import ExecutionSubstrate

engine = ExecutionSubstrate()
orders = [
    engine.pre_order(account_id=account, signal_id="s", idempotency_key="i", canonical_hash="h",
        risk_approved=True, execution_epoch=1, order_payload={})
    for account in ("account-a", "account-b")
]
fills = [
    engine.record_fill(account, result.order.id, "shared-deal", "0.10",
        native_protection_confirmed=True)
    for account, result in zip(("account-a", "account-b"), orders)
]
assert fills[0].id != fills[1].id
assert fills[0].account_id == "account-a"
assert fills[1].account_id == "account-b"
print("ok")
`);
  assert.match(output, /ok/);
});

test("positions expose account-local operational facts and protected reduce commands", () => {
  const output = run(`
from backend.app.execution import ExecutionSubstrate, ExecutionError

engine = ExecutionSubstrate()
entry = engine.pre_order(account_id="a", signal_id="s", idempotency_key="i", canonical_hash="h",
    risk_approved=True, execution_epoch=1,
    order_payload={"symbol": "EURUSD", "volume": "1", "side": "BUY", "entry_price": "1.10"})
engine.record_fill("a", entry.order.id, "deal", "1", native_protection_confirmed=False)
view = engine.position_view("a", entry.order.id)
assert view["pair"] == "EURUSD" and view["direction"] == "LONG"
assert view["entry_price"] == "1.10" and view["current_pnl"] == "UNKNOWN"
assert view["protection_status"] == "UNCONFIRMED" and view["order_status"] == "FILLED"
assert view["data_status"] == "UNKNOWN"
try:
    engine.request_position_close("b", entry.order.id, "0.2", "wrong", "review", confirmed=True)
except ExecutionError as error:
    assert error.code == "WRONG_ACCOUNT"
else:
    raise AssertionError("position crossed account boundary")
command = engine.request_position_close("a", entry.order.id, "0.2", "EURUSD", "reduce risk", confirmed=True, idempotency_key="exit-1")
assert command.reduce_only and command.status == "RECEIVED"
assert command.requested_volume == "0.2"
print("ok")
`);
  assert.match(output, /ok/);
});
