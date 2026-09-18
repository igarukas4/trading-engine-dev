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
    valid_until=now + timedelta(seconds=20), signal_revision=2, signal_id="signal-a",
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
    assert reloaded.risk_assessments[created.order.risk_assessment_id]["signal_revision"] == 2
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
def assessment(account, signal_id):
    return RiskAssessment(account, 1, True, purpose="PRE_ORDER", assessed_at=now, valid_until=now + timedelta(seconds=10), signal_revision=1, signal_id=signal_id)

engine = ExecutionCoordinator()
try:
    engine.accept_execution(account_id="a", signal_id="s", idempotency_key="bad", canonical_hash="h", execution_epoch=1,
        risk_assessment=RiskAssessment("a", 1, False, purpose="PRE_ORDER", assessed_at=now, valid_until=now + timedelta(seconds=10), signal_revision=1, signal_id="s"), signal_revision=1,
        order_payload={"volume": "1"}, now=now)
except ExecutionError as error: assert error.code == "PRE_ORDER_RISK_REJECTED"
else: raise AssertionError("rejected assessment accepted")
try:
    engine.accept_execution(account_id="a", signal_id="s", idempotency_key="stale-revision", canonical_hash="stale", execution_epoch=1,
        risk_assessment=assessment("a", "s"), signal_revision=2, order_payload={"volume": "1"}, now=now)
except ExecutionError as error: assert error.code == "SIGNAL_REVISION_CHANGED"
else: raise AssertionError("assessment from another revision accepted")

def order(account, key):
    return engine.accept_execution(account_id=account, signal_id=key, idempotency_key=key, canonical_hash=key,
        execution_epoch=1, risk_assessment=assessment(account, key), signal_revision=1, order_payload={"symbol": "EURUSD", "volume": "1"}, now=now).order

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
    valid_until=now - timedelta(seconds=10), signal_revision=1, signal_id="signal-a",
)
try:
    engine.execute_signal(
        account_id="account-a", signal_id="signal-a", idempotency_key="execute-a",
        reason="execute", confirmed=True, signal_revision=1,
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
    valid_until=now + timedelta(seconds=20), signal_revision=1, signal_id="signal-initial",
)
try:
    engine.execute_signal(
        account_id="account-a", signal_id="signal-initial", idempotency_key="execute-initial",
        reason="execute", confirmed=True, signal_revision=1,
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
    valid_until=now + timedelta(seconds=20), signal_revision=1, signal_id="signal-b",
)
try:
    engine.execute_signal(
        account_id="account-a", signal_id="signal-b", idempotency_key="execute-stale-revision",
        reason="execute", confirmed=True, signal_revision=2,
        risk_assessment=fresh, signal_fresh=True, fence_safe=True, account_state="RUNNING",
        live_lock=True, execution_epoch=1,
        order_payload={"stop_loss": "1", "take_profit": ["2"], "signal_revision": 2},
    )
except ExecutionError as error: assert error.code == "SIGNAL_REVISION_CHANGED"
else: raise AssertionError("assessment from another Signal revision created an order")
assert not engine.orders and not engine.reservations
accepted = engine.execute_signal(
    account_id="account-a", signal_id="signal-b", idempotency_key="execute-b",
    reason="execute", confirmed=True, signal_revision=1,
    risk_assessment=fresh, signal_fresh=True, fence_safe=True, account_state="RUNNING",
    live_lock=True, execution_epoch=1,
    order_payload={"stop_loss": "1", "take_profit": ["2"], "signal_revision": 1},
)
assert accepted.order.risk_assessment_id in engine.risk_assessments
print("ok")
`);
  assert.match(output, /ok/);
});

test("pre-order risk recomputes account-local exposure and uses the correct risk source", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import backend.app.main as main
from backend.app.execution import ExecutionSubstrate, RiskReservation
from backend.app.risk_calendar import RiskAssessment

now = datetime.now(timezone.utc)
captured = []
class CapturingRiskEngine:
    def assess(self, account_id, limits, **context):
        captured.append(context)
        return RiskAssessment(
            account_id, 1, True, purpose="PRE_ORDER", assessed_at=now,
            valid_until=now + timedelta(seconds=20), signal_id=context["signal_id"],
            signal_revision=context["signal_revision"],
        )

original_execution = main.execution
original_risk_engine = main.signals.risk_engine
try:
    engine = ExecutionSubstrate()
    engine.positions[("account-a", "position-a")] = SimpleNamespace(
        account_id="account-a", stage="ENTRY", data_status="CONFIRMED",
    )
    engine.positions[("account-b", "position-b")] = SimpleNamespace(
        account_id="account-b", stage="ENTRY", data_status="CONFIRMED",
    )
    engine.reservations["active-a"] = RiskReservation("active-a", "account-a", "s", "2.5")
    engine.reservations["released-a"] = RiskReservation("released-a", "account-a", "s", "9", "RELEASED")
    engine.reservations["active-b"] = RiskReservation("active-b", "account-b", "s", "7")
    main.execution = engine
    main.signals.risk_engine = CapturingRiskEngine()
    signal = SimpleNamespace(
        id="signal-a", revision=3, expires_at=now + timedelta(minutes=1),
        strategy_config_version_id="config-a", risk_context={
            "requested_risk": Decimal("1.25"), "open_positions": 99, "open_risk": Decimal("99"),
        },
    )
    account = SimpleNamespace(bot_state="RUNNING")
    main._pre_order_risk_assessment("account-a", signal, account, requested_risk=Decimal("0.75"))
    manual = captured[-1]
    assert manual["requested_risk"] == Decimal("0.75")
    assert manual["open_positions"] == 1 and manual["open_risk"] == Decimal("2.5")
    main._pre_order_risk_assessment("account-a", signal, account)
    semi_auto = captured[-1]
    assert semi_auto["requested_risk"] == Decimal("1.25")
finally:
    main.execution = original_execution
    main.signals.risk_engine = original_risk_engine
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
    valid_until=now + timedelta(seconds=20), signal_revision=1, signal_id="new-signal",
)
try:
    engine.schedule_automated_signal(
        account_id="account-a", signal_id="old-signal", idempotency_key="old-order",
        mode="SEMI_AUTO", signal_created_at=now - timedelta(seconds=1), signal_revision=1,
        signal_eligible=True, signal_approved=True, mode_changed_at=now,
        risk_assessment=fresh, signal_fresh=True, fence_safe=True, account_state="RUNNING",
        live_lock=True, execution_epoch=1, order_payload=payload,
    )
except ExecutionError as error: assert error.code == "SIGNAL_PRECEDES_MODE_CHANGE"
else: raise AssertionError("Signal from before the mode change was scheduled")

foreign = RiskAssessment(
    "account-b", 1, True, purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(seconds=20), signal_revision=1, signal_id="new-signal",
)
try:
    engine.schedule_automated_signal(
        account_id="account-a", signal_id="new-signal", idempotency_key="foreign-order",
        mode="SEMI_AUTO", signal_created_at=now, signal_revision=1,
        signal_eligible=True, signal_approved=True, mode_changed_at=now,
        risk_assessment=foreign, signal_fresh=True, fence_safe=True, account_state="RUNNING",
        live_lock=True, execution_epoch=1, order_payload=payload,
    )
except ExecutionError as error: assert error.code == "RISK_ASSESSMENT_ACCOUNT_MISMATCH"
else: raise AssertionError("foreign PRE_ORDER assessment created an order")
assert not engine.orders and not engine.reservations

accepted = engine.schedule_automated_signal(
    account_id="account-a", signal_id="new-signal", idempotency_key="fresh-order",
    mode="SEMI_AUTO", signal_created_at=now, signal_revision=1,
    signal_eligible=True, signal_approved=True, mode_changed_at=now,
    risk_assessment=fresh, signal_fresh=True, fence_safe=True, account_state="RUNNING",
    live_lock=True, execution_epoch=1, order_payload=payload,
)
assert accepted.order.risk_assessment_id in engine.risk_assessments
try:
    engine.schedule_automated_signal(
        account_id="account-a", signal_id="ready-signal", idempotency_key="not-ready",
        mode="FULL_AUTO", signal_created_at=now, signal_revision=1,
        signal_eligible=True, signal_approved=False, mode_changed_at=now,
        risk_assessment=fresh, signal_fresh=True, fence_safe=True, account_ready=False,
        account_state="RUNNING", live_lock=True, execution_epoch=1, order_payload=payload,
    )
except ExecutionError as error: assert error.code == "EXECUTION_GATE_UNSAFE"
else: raise AssertionError("FULL_AUTO bypassed account readiness")
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
assessment = RiskAssessment("a", 1, True, purpose="PRE_ORDER", assessed_at=now, valid_until=now + timedelta(seconds=10), signal_revision=1, signal_id="s")
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

test("Execution Coordination persists automatic UNKNOWN recovery, isolates accounts, and escalates deadlines", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from backend.app.execution import ExecutionCoordinator, ExecutionError
from backend.app.risk_calendar import RiskAssessment

class Broker:
    def __init__(self, state=None): self.sent = 0; self.state = state
    def order_check(self, order): return True
    def order_send(self, order): self.sent += 1; return "TIMEOUT"
    def journal(self, order): return None
    def broker_state(self, order): return self.state

at = datetime.now(timezone.utc)
def assessment(account, signal):
    return RiskAssessment(account, 1, True, purpose="PRE_ORDER", assessed_at=at,
        valid_until=at + timedelta(minutes=10), signal_revision=1, signal_id=signal)
def accept(engine, account, signal):
    return engine.schedule_automated_signal(
        account_id=account, signal_id=signal, idempotency_key=f"auto:{signal}",
        mode="FULL_AUTO", signal_created_at=at, signal_revision=1,
        signal_eligible=True, signal_approved=False, mode_changed_at=at,
        risk_assessment=assessment(account, signal), signal_fresh=True,
        fence_safe=True, account_state="RUNNING", live_lock=True,
        execution_epoch=engine.account(account).execution_epoch,
        order_payload={"volume": "1", "stop_loss": "1", "take_profit": ["2"]},
    ).order

ordered = ExecutionCoordinator(reconciliation_deadline=timedelta(seconds=30))
accept(ordered, "account-ordered", "first")
accept(ordered, "account-ordered", "second")
assert ordered.dispatch_next("account-ordered", Broker()).status == "UNKNOWN"
try:
    ordered.dispatch_next("account-ordered", Broker({"status": "SUBMITTED"}))
except ExecutionError as error: assert error.code == "RECONCILIATION_PENDING"
else: raise AssertionError("a later account dispatch crossed unresolved UNKNOWN")

with TemporaryDirectory() as directory:
    path = f"{directory}/execution.json"
    engine = ExecutionCoordinator(state_path=path, reconciliation_deadline=timedelta(seconds=30))
    unknown = accept(engine, "account-a", "signal-a")
    healthy = accept(engine, "account-b", "signal-b")
    ambiguous = Broker()
    assert engine.dispatch_next("account-a", ambiguous).status == "UNKNOWN"
    assert ambiguous.sent == 1 and engine.account("account-a").exposure_gate == "QUARANTINED"
    assert engine.account("account-b").exposure_gate == "OPEN"
    work = engine.recovery_records("account-a")[0]
    assert work["status"] == "PENDING" and work["deadline_at"]

    restarted = ExecutionCoordinator(state_path=path, reconciliation_deadline=timedelta(seconds=30))
    broker_truth = Broker({"status": "SUBMITTED", "external_id": "mt5-a"})
    results = restarted.reconcile_due("account-a", broker_truth, now=at + timedelta(seconds=1))
    assert results[0].status == "SUBMITTED" and broker_truth.sent == 0
    assert restarted.orders[unknown.id].status == "SUBMITTED"
    assert restarted.account("account-a").exposure_gate == "OPEN"
    assert restarted.recovery_records("account-a")[0]["status"] == "RECOVERED"
    assert restarted.account("account-b").exposure_gate == "FENCE_PENDING"
    assert restarted.recovery_snapshot("account-b")["status"] == "RECOVERING"

    stuck = accept(restarted, "account-a", "signal-stuck")
    restarted.dispatch_next("account-a", Broker())
    records = restarted.advance_recovery_deadlines(now=at + timedelta(seconds=31))
    assert records[0].subject_id == stuck.id
    escalated = [item for item in restarted.recovery_records("account-a") if item["subject_id"] == stuck.id][0]
    assert escalated["status"] == "ESCALATED" and escalated["critical"]
    assert restarted.account("account-a").exposure_gate == "QUARANTINED"
    assert restarted.account("account-b").exposure_gate == "FENCE_PENDING"
    try:
        accept(restarted, "account-a", "signal-after-deadline")
    except ExecutionError as error: assert error.code == "EXPOSURE_GATE_CLOSED"
    else: raise AssertionError("escalated account accepted new exposure")
print("ok")
`);
  assert.match(output, /ok/);
});

test("connector reconnect requests and applies account-scoped reconciliation work", () => {
  const output = run(`
import asyncio
from datetime import datetime, timedelta, timezone

from starlette.websockets import WebSocketDisconnect

from backend.app.main import accounts, connector_stream, dashboard_hub, execution
from backend.app.risk_calendar import RiskAssessment

class Broker:
    def order_check(self, order): return True
    def order_send(self, order): return "TIMEOUT"

now = datetime.now(timezone.utc)
account = accounts.register(provider="mt5", broker_server="demo", external_account_id="reconcile-1", display_name="reconcile", environment="DEMO")
secret = "x" * 32
key_id = accounts.bind_connector(account.id, secret)
assessment = RiskAssessment(account.id, 1, True, purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(minutes=1), signal_revision=1, signal_id="signal")
created = execution.accept_execution(account_id=account.id, signal_id="signal", idempotency_key="reconcile", canonical_hash="reconcile", execution_epoch=1,
    risk_assessment=assessment, signal_revision=1, order_payload={"volume": "1"}, now=now)
assert execution.dispatch_next(account.id, Broker()).status == "UNKNOWN"

class Socket:
    def __init__(self, messages): self.messages = iter(messages); self.sent = []
    async def accept(self): pass
    async def receive_json(self):
        try: return next(self.messages)
        except StopIteration: raise WebSocketDisconnect()
    async def send_json(self, message): self.sent.append(message)
    async def close(self, **kwargs): self.sent.append({"type": "closed", **kwargs})

socket = Socket([
    {"type": "hello", "account_id": account.id, "provider": "mt5", "broker_server": "demo", "external_account_id": "reconcile-1", "key_id": key_id, "secret": secret, "generation": 0, "session_id": "session"},
    {"type": "reconciliation_observation", "account_id": account.id, "generation": 0, "observation": {"orders": [{"order_id": created.order.id, "status": "SUBMITTED", "external_id": "mt5-1"}]}},
    {"type": "reconciliation_observation", "account_id": account.id, "generation": 0, "observation": {"account_id": "other"}},
    {"type": "reconciliation_observation", "account_id": account.id, "generation": 0, "observation": "invalid"},
])
asyncio.run(connector_stream(socket))
assert socket.sent and socket.sent[0]["type"] == "snapshot", socket.sent
assert socket.sent[1]["type"] == "reconciliation.required" and socket.sent[1]["account_id"] == account.id
assert socket.sent[2]["type"] == "reconciliation_observed"
assert socket.sent[3]["type"] == "error" and socket.sent[3]["payload"]["code"] == "WRONG_ACCOUNT"
assert socket.sent[4]["type"] == "error" and socket.sent[4]["payload"]["code"] == "INVALID_RECONCILIATION_OBSERVATION"

assert execution.orders[created.order.id].status == "SUBMITTED"
assert execution.account(account.id).exposure_gate == "OPEN"
assert any(event["type"] == "execution.reconciliation.observed" for event in dashboard_hub.connect({"account_cursors": {account.id: 0}})["events"])
print("ok")
`);
  assert.match(output, /ok/);
});

test("reconciliation gate rejects sparse or unmatched recovery evidence", () => {
  const output = run(`
from backend.app.main import _reconciliation_gate_complete

recovery = [{"subject_id": "command-1", "kind": "COMMAND"}]
base = {
    "complete": True,
    "orders": [], "fills": [], "positions": [],
    "commands": [{"command_id": "command-1"}],
    "history_orders": [], "deals": [],
    "from_server_time": "2026-09-17T23:55:00Z",
}
assert not _reconciliation_gate_complete(
    {**base, "recovery_matches": [{"subject_id": "command-1", "status": "UNRESOLVED"}]},
    recovery,
    "2026-09-17T23:55:00Z",
)
assert _reconciliation_gate_complete(
    {**base, "recovery_matches": [{"subject_id": "command-1", "kind": "COMMAND", "status": "MATCHED"}]},
    recovery,
    "2026-09-17T23:55:00Z",
)
assert not _reconciliation_gate_complete(
    {**base, "recovery_matches": [{"subject_id": "command-1", "status": "MATCHED"}], "from_server_time": None},
    recovery,
    "2026-09-17T23:55:00Z",
)
assert not _reconciliation_gate_complete(
    {**base, "recovery_matches": [{"subject_id": "command-1", "kind": "UNKNOWN", "status": "MATCHED"}]},
    recovery,
    "2026-09-17T23:55:00Z",
)
print("ok")
  `);
  assert.match(output, /ok/);
});

test("ambiguous close-all and position commands stay UNKNOWN until broker observations resolve them", () => {
  const output = run(`
from backend.app.execution import ExecutionCoordinator

class Broker:
    def __init__(self): self.close_calls = 0; self.command_status = None
    def close_all(self, account_id): self.close_calls += 1; return "TIMEOUT"
    def command_state(self, command): return self.command_status

engine = ExecutionCoordinator()
broker = Broker()
close = engine.close_all(account_id="a", idempotency_key="close", reason="operator", confirmed=True, connector=broker)
assert close.status == "UNKNOWN"
work = engine.recovery_records("a")[0]
assert work["subject_id"] == close.id and work["kind"] == "COMMAND"
broker.command_status = {"status": "EXECUTED"}
assert engine.reconcile_due("a", broker)[0].status == "EXECUTED"
assert broker.close_calls == 1
assert close.status == "EXECUTED" and engine.recovery_records("a")[0]["status"] == "RECOVERED"
engine.reconcile_observation("a", {"commands": [{"command_id": close.id, "status": "UNKNOWN"}]})
assert close.status == "EXECUTED" and len(engine.recovery_records("a")) == 1
print("ok")
`);
  assert.match(output, /ok/);
});

test("ambiguous pending-order cancellation is recovered without a second cancel", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionCoordinator
from backend.app.risk_calendar import RiskAssessment

class Broker:
    def __init__(self): self.send_calls = 0; self.cancel_calls = 0; self.command_status = None
    def order_check(self, order): return True
    def order_send(self, order): self.send_calls += 1; return "SUBMITTED"
    def cancel_order(self, order): self.cancel_calls += 1; return "TIMEOUT"
    def command_state(self, command): return self.command_status

now = datetime.now(timezone.utc)
engine = ExecutionCoordinator()
assessment = RiskAssessment("a", 1, True, purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(minutes=1), signal_revision=1, signal_id="signal")
entry = engine.accept_execution(account_id="a", signal_id="signal", idempotency_key="entry",
    canonical_hash="entry", execution_epoch=1, risk_assessment=assessment,
    signal_revision=1, order_payload={"volume": "1"}, now=now)
broker = Broker()
assert engine.dispatch_next("a", broker).status == "SUBMITTED"
cancel = engine.cancel_order(account_id="a", order_id=entry.order.id, idempotency_key="cancel",
    reason="risk change", confirmed=True, connector=broker)
assert cancel.status == "UNKNOWN" and broker.cancel_calls == 1
broker.command_status = {"status": "EXECUTED"}
assert engine.reconcile_due("a", broker)[0].status == "EXECUTED"
assert broker.cancel_calls == 1
assert entry.order.status == "CANCELLED" and entry.reservation.status == "RELEASED"
print("ok")
`);
  assert.match(output, /ok/);
});

test("automatic reconciliation resolves ambiguous position commands without resending", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionCoordinator
from backend.app.risk_calendar import RiskAssessment

class Broker:
    def __init__(self): self.modify_calls = 0; self.command_status = None
    def modify_position(self, command):
        self.modify_calls += 1
        return "TIMEOUT"
    def command_state(self, command):
        return self.command_status

now = datetime.now(timezone.utc)
engine = ExecutionCoordinator(reconciliation_deadline=timedelta(minutes=1))
assessment = RiskAssessment("a", 1, True, purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(minutes=1), signal_revision=1, signal_id="signal")
entry = engine.accept_execution(account_id="a", signal_id="signal", idempotency_key="entry",
    canonical_hash="entry", execution_epoch=1, risk_assessment=assessment,
    signal_revision=1, order_payload={"volume": "1", "symbol": "EURUSD", "stop_loss": "95"}, now=now)
engine.record_fill("a", entry.order.id, "deal", "1", native_protection_confirmed=True)
engine.record_exit_fill("a", entry.order.id, "tp1", "TP1", "0.1")
engine.record_exit_fill("a", entry.order.id, "tp2", "TP2", "0.1")
command = engine.request_trailing("a", entry.order.id, "96", direction="LONG",
    closed_candle=True, atomic_capability=True)
broker = Broker()
assert engine.dispatch_position_command("a", command.id, broker).status == "UNKNOWN"
assert broker.modify_calls == 1
assert engine.recovery_records("a")[0]["subject_id"] == command.id
broker.command_status = {"status": "CONFIRMED", "stop": "96"}
result = engine.reconcile_due("a", broker, now=now + timedelta(seconds=1))
assert result[0].status == "CONFIRMED"
assert broker.modify_calls == 1
assert command.status == "CONFIRMED"
assert engine.recovery_records("a")[0]["status"] == "RECOVERED"
assert engine.account("a").exposure_gate == "OPEN"
print("ok")
`);
  assert.match(output, /ok/);
});

test("broker UNKNOWN observations create recoverable account-local work", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionCoordinator
from backend.app.risk_calendar import RiskAssessment

class Broker:
    def journal(self, order): return None
    def broker_state(self, order): return {"status": "SUBMITTED", "external_id": "broker-1"}

now = datetime.now(timezone.utc)
engine = ExecutionCoordinator()
assessment = RiskAssessment("a", 1, True, purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(minutes=1), signal_revision=1, signal_id="signal")
entry = engine.accept_execution(account_id="a", signal_id="signal", idempotency_key="entry",
    canonical_hash="entry", execution_epoch=1, risk_assessment=assessment,
    signal_revision=1, order_payload={"volume": "1"}, now=now)
observation = {"orders": [{"order_id": entry.order.id, "status": "UNKNOWN"}]}
engine.reconcile_observation("a", observation)
records = engine.recovery_records("a")
assert len(records) == 1 and records[0]["subject_id"] == entry.order.id
assert records[0]["status"] == "PENDING"
assert engine.account("a").exposure_gate == "QUARANTINED"
assert engine.reconcile_due("a", Broker(), now=now + timedelta(seconds=1))[0].status == "SUBMITTED"
assert engine.recovery_records("a")[0]["status"] == "RECOVERED"
assert engine.account("a").exposure_gate == "OPEN"
print("ok")
`);
  assert.match(output, /ok/);
});

test("unknown recovery preserves an independent account fence", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionCoordinator
from backend.app.risk_calendar import RiskAssessment

class Broker:
    def order_check(self, order): return True
    def order_send(self, order): return "TIMEOUT"
    def broker_state(self, order): return {"status": "SUBMITTED"}
    def journal(self, order): return None

now = datetime.now(timezone.utc)
engine = ExecutionCoordinator()
assessment = RiskAssessment("a", 1, True, purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(minutes=1), signal_revision=1, signal_id="signal")
entry = engine.accept_execution(account_id="a", signal_id="signal", idempotency_key="entry",
    canonical_hash="entry", execution_epoch=1, risk_assessment=assessment,
    signal_revision=1, order_payload={"volume": "1"}, now=now)
engine.install_fence("a", "EMERGENCY_STOP")
assert engine.dispatch_next("a", Broker()).status == "UNKNOWN"
assert engine.reconcile_due("a", Broker(), now=now + timedelta(seconds=1))[0].status == "SUBMITTED"
assert engine.account("a").exposure_gate == "FENCE_PENDING"
print("ok")
`);
  assert.match(output, /ok/);
});

test("recovery does not replace an independent fence installed after an earlier recovery", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionCoordinator
from backend.app.risk_calendar import RiskAssessment

class Broker:
    def order_check(self, order): return True
    def order_send(self, order): return "TIMEOUT"
    def journal(self, order): return None
    def broker_state(self, order): return {"status": "SUBMITTED"}

now = datetime.now(timezone.utc)
engine = ExecutionCoordinator()
def assessment(signal_id):
    return RiskAssessment("a", 1, True, purpose="PRE_ORDER", assessed_at=now,
        valid_until=now + timedelta(minutes=1), signal_revision=1, signal_id=signal_id)
first = engine.accept_execution(account_id="a", signal_id="first", idempotency_key="first",
    canonical_hash="first", execution_epoch=1, risk_assessment=assessment("first"),
    signal_revision=1, order_payload={"volume": "1"}, now=now)
assert engine.dispatch_next("a", Broker()).status == "UNKNOWN"
assert engine.reconcile_due("a", Broker(), now=now + timedelta(seconds=1))[0].status == "SUBMITTED"
second = engine.accept_execution(account_id="a", signal_id="second", idempotency_key="second",
    canonical_hash="second", execution_epoch=engine.account("a").execution_epoch,
    risk_assessment=assessment("second"),
    signal_revision=1, order_payload={"volume": "1"}, now=now)
engine.install_fence("a", "EMERGENCY_STOP")
assert engine.dispatch_next("a", Broker()).status == "UNKNOWN"
assert engine.reconcile_due("a", Broker(), now=now + timedelta(seconds=2))[0].status == "SUBMITTED"
assert engine.account("a").exposure_gate == "FENCE_PENDING"
print("ok")
`);
  assert.match(output, /ok/);
});

test("protection confirmation cannot reopen an account with unresolved UNKNOWN work", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionCoordinator
from backend.app.risk_calendar import RiskAssessment

now = datetime.now(timezone.utc)
engine = ExecutionCoordinator()
assessment = RiskAssessment("a", 1, True, purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(minutes=1), signal_revision=1, signal_id="signal")
entry = engine.accept_execution(account_id="a", signal_id="signal", idempotency_key="entry",
    canonical_hash="entry", execution_epoch=1, risk_assessment=assessment,
    signal_revision=1, order_payload={"volume": "1", "stop_loss": "95"}, now=now)
engine.record_fill("a", entry.order.id, "deal", "1", native_protection_confirmed=True)
engine.record_exit_fill("a", entry.order.id, "tp1", "TP1", "0.1")
engine.record_exit_fill("a", entry.order.id, "tp2", "TP2", "0.1")
command = engine.request_trailing("a", entry.order.id, "96", direction="LONG",
    closed_candle=True, atomic_capability=True)
engine.mark_position_command_unknown("a", command.id)
engine.confirm_protection("a", entry.order.id)
assert engine.account("a").exposure_gate == "QUARANTINED"
print("ok")
`);
  assert.match(output, /ok/);
});

test("durable dispatch checkpoints the journal before broker invocation", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionCoordinator
from backend.app.risk_calendar import RiskAssessment

class Store:
    def __init__(self): self.state = None; self.saves = []
    def load(self): return self.state
    def save(self, state): self.state = state; self.saves.append(state)

class Broker:
    def __init__(self, store, order_id): self.store = store; self.order_id = order_id
    def order_check(self, order):
        assert self.store.state["orders"][self.order_id]["status"] == "DISPATCHING"
        assert self.store.state["journal"][self.order_id]["state"] == "DISPATCHING"
        return True
    def order_send(self, order): return {"status": "SUBMITTED", "external_id": "broker-1"}

now = datetime.now(timezone.utc)
store = Store()
engine = ExecutionCoordinator(state_store=store)
assessment = RiskAssessment("a", 1, True, purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(minutes=1), signal_revision=1, signal_id="signal")
entry = engine.accept_execution(account_id="a", signal_id="signal", idempotency_key="entry",
    canonical_hash="entry", execution_epoch=1, risk_assessment=assessment,
    signal_revision=1, order_payload={"volume": "1"}, now=now)
assert engine.dispatch_next("a", Broker(store, entry.order.id)).status == "SUBMITTED"
assert len(store.saves) >= 2
print("ok")
`);
  assert.match(output, /ok/);
});

test("reconciliation ignores stale order states after a terminal broker result", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.execution import ExecutionCoordinator
from backend.app.risk_calendar import RiskAssessment

now = datetime.now(timezone.utc)
engine = ExecutionCoordinator()
assessment = RiskAssessment("a", 1, True, purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(minutes=1), signal_revision=1, signal_id="signal")
entry = engine.accept_execution(account_id="a", signal_id="signal", idempotency_key="entry",
    canonical_hash="entry", execution_epoch=1, risk_assessment=assessment,
    signal_revision=1, order_payload={"volume": "1"}, now=now)
engine.reconcile_observation("a", {
    "orders": [{"order_id": entry.order.id, "status": "FILLED"}],
    "fills": [{"deal_id": "deal", "order_id": entry.order.id, "volume": "1", "entry": "IN"}],
})
engine.reconcile_observation("a", {
    "orders": [{"order_id": entry.order.id, "status": "SUBMITTED"}],
    "fills": [{"deal_id": "deal", "order_id": entry.order.id, "volume": "1", "entry": "IN"}],
})
assert entry.order.status == "FILLED"
assert entry.reservation.status == "CONSUMED"
print("ok")
`);
  assert.match(output, /ok/);
});

test("FULL_AUTO uses account readiness instead of bypassing lifecycle gates", () => {
  const output = run(`
from datetime import timedelta
from types import SimpleNamespace

import backend.app.main as main
from backend.app.broker_accounts import BrokerAccount
from backend.app.execution import ExecutionCoordinator, ExecutionError
from backend.app.risk_calendar import RiskAssessment

account = BrokerAccount("mt5", "demo", "full-auto", "full-auto")
account.execution_mode = "FULL_AUTO"
account.bot_state = "RUNNING"
account.connector_bound = True
account.connector_healthy = True
account.reconciliation_complete = True
account.risk_limits_active = True
account.mappings_valid = True
signal = SimpleNamespace(
    id="signal", account_id=account.id, revision=1, created_at=account.mode_changed_at,
    expires_at=account.mode_changed_at + timedelta(minutes=1),
    strategy_config_version_id="config", stop_loss="95", take_profit=("105",),
    opportunity={"account_id": account.id, "pair": "EURUSD", "direction": "LONG"},
    risk_context={"requested_risk": "0.1"},
    as_dict=lambda: {"status": "ELIGIBLE"},
)
assessment = RiskAssessment(account.id, 1, True, purpose="PRE_ORDER",
    assessed_at=account.mode_changed_at,
    valid_until=account.mode_changed_at + timedelta(minutes=1),
    signal_revision=1, signal_id=signal.id)
original_execution = main.execution
original_assessment = main._pre_order_risk_assessment
main.execution = ExecutionCoordinator()
main._pre_order_risk_assessment = lambda *args, **kwargs: assessment
try:
    signal.account_id = "other-account"
    try:
        main._schedule_full_auto_signal(signal, account)
    except ExecutionError as error: assert error.code == "WRONG_ACCOUNT"
    else: raise AssertionError("FULL_AUTO accepted a foreign Signal")
    signal.account_id = account.id
    try:
        main._schedule_full_auto_signal(signal, account)
    except ExecutionError as error: assert error.code == "EXECUTION_GATE_UNSAFE"
    else: raise AssertionError("FULL_AUTO scheduled for a disabled account")
    account.lifecycle_status = "ENABLED"
    first = main._schedule_full_auto_signal(signal, account)
    second = main._schedule_full_auto_signal(signal, account)
    assert first["order_id"] == second["order_id"]
finally:
    main.execution = original_execution
    main._pre_order_risk_assessment = original_assessment
print("ok")
`);
  assert.match(output, /ok/);
});

test("restart converts a persisted DISPATCHING order into UNKNOWN recovery", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from backend.app.execution import ExecutionCoordinator
from backend.app.risk_calendar import RiskAssessment

class CrashBroker:
    def order_check(self, order): raise KeyboardInterrupt()
    def order_send(self, order): raise AssertionError("order_send must not run")

class TruthBroker:
    def order_check(self, order): raise AssertionError("recovery must not check")
    def order_send(self, order): raise AssertionError("recovery must not send")
    def journal(self, order): return None
    def broker_state(self, order): return {"status": "SUBMITTED", "external_id": "broker-1"}

now = datetime.now(timezone.utc)
assessment = RiskAssessment("a", 1, True, purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(minutes=1), signal_revision=1, signal_id="signal")
with TemporaryDirectory() as directory:
    path = f"{directory}/execution.json"
    engine = ExecutionCoordinator(state_path=path)
    entry = engine.accept_execution(account_id="a", signal_id="signal", idempotency_key="entry",
        canonical_hash="entry", execution_epoch=1, risk_assessment=assessment,
        signal_revision=1, order_payload={"volume": "1"}, now=now)
    try: engine.dispatch_next("a", CrashBroker())
    except KeyboardInterrupt: pass
    restarted = ExecutionCoordinator(state_path=path)
    assert restarted.orders[entry.order.id].status == "UNKNOWN"
    assert restarted.recovery_records("a")[0]["status"] == "PENDING"
    assert restarted.reconcile_due("a", TruthBroker(), now=now + timedelta(seconds=1))[0].status == "SUBMITTED"
    assert restarted.recovery_records("a")[0]["status"] == "RECOVERED"
print("ok")
`);
  assert.match(output, /ok/);
});
