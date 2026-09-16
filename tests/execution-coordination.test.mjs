import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";

const run = (script) => execFileSync(process.execPath, ["tests/python.mjs", "-c", script], { encoding: "utf8" });

test("Execution Coordination reloads durable intents and projects duplicate partial observations", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from backend.app.execution import ExecutionCoordinator
from backend.app.risk_calendar import RiskAssessment

class Broker:
    def order_check(self, order): return True
    def order_send(self, order): return {"status": "SUBMITTED", "external_id": "mt5-order-1"}

now = datetime(2026, 1, 1, tzinfo=timezone.utc)
assessment = RiskAssessment(
    broker_account_id="account-a", risk_limits_version=4, approved=True,
    purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(seconds=20), signal_revision=2,
)
with TemporaryDirectory() as directory:
    path = f"{directory}/execution.json"
    engine = ExecutionCoordinator(state_path=path)
    created = engine.accept_execution(
        account_id="account-a", signal_id="signal-a", idempotency_key="command-a",
        canonical_hash="request-a", execution_epoch=1, risk_assessment=assessment,
        signal_revision=2,
        order_payload={"symbol": "EURUSD", "side": "BUY", "volume": "1", "stop_loss": "90", "take_profit": "110"},
        now=now,
    )
    assert created.order.status == "INTENT"
    assert created.order.command_id in engine.commands
    assert engine.dispatch_next("account-a", Broker()).status == "SUBMITTED"

    reloaded = ExecutionCoordinator(state_path=path)
    assert reloaded.orders[created.order.id].external_id == "mt5-order-1"
    assert reloaded.orders[created.order.id].command_id in reloaded.commands
    assert created.order.risk_assessment_id in reloaded.risk_assessments
    assert reloaded.risk_assessments[created.order.risk_assessment_id]["assessed_at"] == now
    first = reloaded.reconcile_observation("account-a", {
        "orders": [{"order_id": created.order.id, "status": "PARTIALLY_FILLED"}],
        "fills": [{"deal_id": "deal-1", "order_id": created.order.id, "volume": "0.4", "position_id": "position-1", "entry": "IN"}],
        "positions": [{"position_id": "position-1", "order_id": created.order.id, "symbol": "EURUSD", "side": "BUY", "volume": "0.4", "sl": "90", "tp": "110"}],
    })
    second = reloaded.reconcile_observation("account-a", {
        "fills": [{"deal_id": "deal-1", "order_id": created.order.id, "volume": "0.4", "position_id": "position-1", "entry": "IN"}],
        "positions": [{"position_id": "position-1", "order_id": created.order.id, "symbol": "EURUSD", "side": "BUY", "volume": "0.4", "sl": "90", "tp": "110"}],
    })
    assert first.status == "PARTIALLY_FILLED"
    assert second.duplicate_fill_ids == ("deal-1",)
    assert len(reloaded.fills) == 1
    assert reloaded.position("account-a", "position-1").volume == "0.4"
    close = reloaded.request_position_close(
        "account-a", created.order.id, "0.1", "EURUSD", "test close",
        confirmed=True, idempotency_key="close-1",
    )
    reloaded_again = ExecutionCoordinator(state_path=path)
    assert reloaded_again.request_position_close(
        "account-a", created.order.id, "0.1", "EURUSD", "test close",
        confirmed=True, idempotency_key="close-1",
    ).id == close.id
    assert all(event.account_id == "account-a" for event in reloaded.audit_events)
print("ok")
`);
  assert.match(output, /ok/);
});

test("Execution Coordination rejects stale risk and keeps netting and hedging account-local", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionCoordinator, ExecutionError
from backend.app.risk_calendar import RiskAssessment

now = datetime(2026, 1, 1, tzinfo=timezone.utc)
def assessment(account):
    return RiskAssessment(account, 1, True, purpose="PRE_ORDER", assessed_at=now, valid_until=now + timedelta(seconds=10), signal_revision=1)

engine = ExecutionCoordinator()
try:
    engine.accept_execution(account_id="a", signal_id="s", idempotency_key="bad", canonical_hash="h", execution_epoch=1,
        risk_assessment=RiskAssessment("a", 1, False, purpose="PRE_ORDER", assessed_at=now, valid_until=now + timedelta(seconds=10), signal_revision=1), signal_revision=1,
        order_payload={"volume": "1"}, now=now)
except ExecutionError as error: assert error.code == "PRE_ORDER_RISK_REJECTED"
else: raise AssertionError("rejected assessment accepted")
try:
    engine.accept_execution(account_id="a", signal_id="s", idempotency_key="stale-revision", canonical_hash="stale", execution_epoch=1,
        risk_assessment=assessment("a"), signal_revision=2, order_payload={"volume": "1"}, now=now)
except ExecutionError as error: assert error.code == "SIGNAL_REVISION_CHANGED"
else: raise AssertionError("assessment from another revision accepted")

def order(account, key):
    return engine.accept_execution(account_id=account, signal_id=key, idempotency_key=key, canonical_hash=key,
        execution_epoch=1, risk_assessment=assessment(account), signal_revision=1, order_payload={"symbol": "EURUSD", "volume": "1"}, now=now).order

net_a = order("a", "net-a")
net_b = order("a", "net-b")
result = engine.reconcile_observation("a", {"fills": [
    {"deal_id": "net-deal-a", "order_id": net_a.id, "volume": "0.4", "position_id": "net-position", "entry": "IN"},
    {"deal_id": "net-deal-b", "order_id": net_b.id, "volume": "0.6", "position_id": "net-position", "entry": "IN"},
], "positions": [{"position_id": "net-position", "order_id": net_a.id, "symbol": "EURUSD", "side": "BUY", "volume": "1", "accounting_mode": "NETTING"}]})
assert result.status == "PARTIALLY_FILLED"
assert len([p for p in engine.positions.values() if p.account_id == "a"]) == 1

hedge_a = order("b", "hedge-a")
hedge_b = order("b", "hedge-b")
engine.reconcile_observation("b", {"positions": [
    {"position_id": "hedge-1", "order_id": hedge_a.id, "symbol": "EURUSD", "side": "BUY", "volume": "0.5", "accounting_mode": "HEDGING"},
    {"position_id": "hedge-2", "order_id": hedge_b.id, "symbol": "EURUSD", "side": "SELL", "volume": "0.5", "accounting_mode": "HEDGING"},
]})
assert len([p for p in engine.positions.values() if p.account_id == "b"]) == 2
try: engine.reconcile_observation("a", {"account_id": "b"})
except ExecutionError as error: assert error.code == "WRONG_ACCOUNT"
else: raise AssertionError("observation crossed account boundary")
print("ok")
`);
  assert.match(output, /ok/);
});

test("Execution Coordination requires a fresh PRE_ORDER assessment for manual execution", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionCoordinator, ExecutionError
from backend.app.risk_calendar import RiskAssessment

now = datetime.now(timezone.utc)
engine = ExecutionCoordinator()
engine.approve_signal(
    account_id="account-a", signal_id="signal-a", idempotency_key="approve-a",
    reason="reviewed", confirmed=True, signal_revision=1,
)
expired = RiskAssessment(
    "account-a", 1, True, purpose="PRE_ORDER", assessed_at=now - timedelta(seconds=20),
    valid_until=now - timedelta(seconds=10), signal_revision=1,
)
try:
    engine.execute_signal(
        account_id="account-a", signal_id="signal-a", idempotency_key="execute-a",
        reason="execute", confirmed=True, signal_revision=1, risk_approved=True,
        risk_assessment=expired, signal_fresh=True, fence_safe=True, account_state="RUNNING",
        live_lock=True, execution_epoch=1,
        order_payload={"stop_loss": "1", "take_profit": ["2"], "signal_revision": 1},
    )
except ExecutionError as error: assert error.code == "RISK_ASSESSMENT_EXPIRED"
else: raise AssertionError("expired PRE_ORDER assessment created an order")
assert not engine.orders and not engine.reservations

engine.approve_signal(
    account_id="account-a", signal_id="signal-initial", idempotency_key="approve-initial",
    reason="reviewed", confirmed=True, signal_revision=1,
)
initial = RiskAssessment(
    "account-a", 1, True, purpose="INITIAL", assessed_at=now,
    valid_until=now + timedelta(seconds=20), signal_revision=1,
)
try:
    engine.execute_signal(
        account_id="account-a", signal_id="signal-initial", idempotency_key="execute-initial",
        reason="execute", confirmed=True, signal_revision=1, risk_approved=True,
        risk_assessment=initial, signal_fresh=True, fence_safe=True, account_state="RUNNING",
        live_lock=True, execution_epoch=1,
        order_payload={"stop_loss": "1", "take_profit": ["2"], "signal_revision": 1},
    )
except ExecutionError as error: assert error.code == "PRE_ORDER_ASSESSMENT_REQUIRED"
else: raise AssertionError("INITIAL assessment created an order")
assert not engine.orders and not engine.reservations

engine.approve_signal(
    account_id="account-a", signal_id="signal-b", idempotency_key="approve-b",
    reason="reviewed", confirmed=True, signal_revision=1,
)
fresh = RiskAssessment(
    "account-a", 1, True, purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(seconds=20), signal_revision=1,
)
try:
    engine.execute_signal(
        account_id="account-a", signal_id="signal-b", idempotency_key="execute-stale-revision",
        reason="execute", confirmed=True, signal_revision=2, risk_approved=True,
        risk_assessment=fresh, signal_fresh=True, fence_safe=True, account_state="RUNNING",
        live_lock=True, execution_epoch=1,
        order_payload={"stop_loss": "1", "take_profit": ["2"], "signal_revision": 2},
    )
except ExecutionError as error: assert error.code == "SIGNAL_REVISION_CHANGED"
else: raise AssertionError("assessment from another Signal revision created an order")
assert not engine.orders and not engine.reservations
accepted = engine.execute_signal(
    account_id="account-a", signal_id="signal-b", idempotency_key="execute-b",
    reason="execute", confirmed=True, signal_revision=1, risk_approved=True,
    risk_assessment=fresh, signal_fresh=True, fence_safe=True, account_state="RUNNING",
    live_lock=True, execution_epoch=1,
    order_payload={"stop_loss": "1", "take_profit": ["2"], "signal_revision": 1},
)
assert accepted.order.risk_assessment_id in engine.risk_assessments
print("ok")
`);
  assert.match(output, /ok/);
});

test("Execution Coordination fences semi-auto Signals from old modes and foreign risk", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionCoordinator, ExecutionError
from backend.app.risk_calendar import RiskAssessment

now = datetime.now(timezone.utc)
engine = ExecutionCoordinator()
payload = {"stop_loss": "1", "take_profit": ["2"], "signal_revision": 1}
fresh = RiskAssessment(
    "account-a", 1, True, purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(seconds=20), signal_revision=1,
)
try:
    engine.schedule_automated_signal(
        account_id="account-a", signal_id="old-signal", idempotency_key="old-order",
        mode="SEMI_AUTO", signal_created_at=now - timedelta(seconds=1), signal_revision=1,
        signal_eligible=True, signal_approved=True, mode_changed_at=now, risk_approved=True,
        risk_assessment=fresh, signal_fresh=True, fence_safe=True, account_state="RUNNING",
        live_lock=True, execution_epoch=1, order_payload=payload,
    )
except ExecutionError as error: assert error.code == "SIGNAL_PRECEDES_MODE_CHANGE"
else: raise AssertionError("Signal from before the mode change was scheduled")

foreign = RiskAssessment(
    "account-b", 1, True, purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(seconds=20), signal_revision=1,
)
try:
    engine.schedule_automated_signal(
        account_id="account-a", signal_id="new-signal", idempotency_key="foreign-order",
        mode="SEMI_AUTO", signal_created_at=now, signal_revision=1,
        signal_eligible=True, signal_approved=True, mode_changed_at=now, risk_approved=True,
        risk_assessment=foreign, signal_fresh=True, fence_safe=True, account_state="RUNNING",
        live_lock=True, execution_epoch=1, order_payload=payload,
    )
except ExecutionError as error: assert error.code == "RISK_ASSESSMENT_ACCOUNT_MISMATCH"
else: raise AssertionError("foreign PRE_ORDER assessment created an order")
assert not engine.orders and not engine.reservations

accepted = engine.schedule_automated_signal(
    account_id="account-a", signal_id="new-signal", idempotency_key="fresh-order",
    mode="SEMI_AUTO", signal_created_at=now, signal_revision=1,
    signal_eligible=True, signal_approved=True, mode_changed_at=now, risk_approved=True,
    risk_assessment=fresh, signal_fresh=True, fence_safe=True, account_state="RUNNING",
    live_lock=True, execution_epoch=1, order_payload=payload,
)
assert accepted.order.risk_assessment_id in engine.risk_assessments
print("ok")
`);
  assert.match(output, /ok/);
});

test("execution coordination has a forward-only PostgreSQL restart checkpoint", () => {
  const migration = readFileSync("backend/migrations/010_execution_coordination_checkpoint.sql", "utf8");
  const backend = readFileSync("backend/app/main.py", "utf8");
  assert.match(migration, /execution_state_snapshots/);
  assert.match(migration, /JSONB/);
  assert.match(migration, /PARTIALLY_FILLED/);
  assert.match(migration, /010_execution_coordination_checkpoint/);
  assert.match(backend, /ExecutionCoordinator\(database_url=/);
});

test("Execution Coordination quarantines an ambiguous dispatch and recovers without a resend", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from backend.app.execution import ExecutionCoordinator
from backend.app.risk_calendar import RiskAssessment

class Broker:
    def __init__(self): self.sent = 0
    def order_check(self, order): return True
    def order_send(self, order): self.sent += 1; return "TIMEOUT"
    def journal(self, order): return None
    def broker_state(self, order): return {"status": "FILLED", "external_id": "mt5-order-reconciled"}

now = datetime(2026, 1, 1, tzinfo=timezone.utc)
assessment = RiskAssessment("a", 1, True, purpose="PRE_ORDER", assessed_at=now, valid_until=now + timedelta(seconds=10), signal_revision=1)
with TemporaryDirectory() as directory:
    path = f"{directory}/execution.json"
    engine = ExecutionCoordinator(state_path=path)
    created = engine.accept_execution(account_id="a", signal_id="s", idempotency_key="k", canonical_hash="h", execution_epoch=1,
        risk_assessment=assessment, signal_revision=1, order_payload={"volume": "1"}, now=now)
    broker = Broker()
    assert engine.dispatch_next("a", broker).status == "UNKNOWN"
    assert engine.account("a").exposure_gate == "QUARANTINED"
    recovered = ExecutionCoordinator(state_path=path)
    assert recovered.recover("a", created.order.id, broker).status == "FILLED"
    assert broker.sent == 1
    checked_again = ExecutionCoordinator(state_path=path)
    assert checked_again.orders[created.order.id].status == "FILLED"
print("ok")
`);
  assert.match(output, /ok/);
});
