import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

const run = (script) =>
  execFileSync(process.execPath, ["tests/python.mjs", "-c", script], {
    encoding: "utf8",
  });

test("a restart requires complete account-local reconciliation before entry resumes", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from backend.app.execution import ExecutionCoordinator, ExecutionError
from backend.app.risk_calendar import RiskAssessment

class Broker:
    def order_check(self, order): return True
    def order_send(self, order): return {"status": "SUBMITTED", "external_id": "broker-order-a"}

at = datetime(2026, 1, 1, tzinfo=timezone.utc)
assessment = RiskAssessment(
    "account-a", 1, True, purpose="PRE_ORDER", assessed_at=at,
    valid_until=at + timedelta(minutes=1), signal_revision=1, signal_id="signal-a",
)
with TemporaryDirectory() as directory:
    path = f"{directory}/execution.json"
    first = ExecutionCoordinator(state_path=path)
    accepted = first.accept_execution(
        account_id="account-a", signal_id="signal-a", idempotency_key="entry-a",
        canonical_hash="hash-a", execution_epoch=1, risk_assessment=assessment,
        signal_revision=1, order_payload={"symbol": "EURUSD", "volume": "1"}, now=at,
    )
    assert first.dispatch_next("account-a", Broker()).status == "SUBMITTED"

    restarted = ExecutionCoordinator(state_path=path)
    assert restarted.recovery_snapshot("account-a")["status"] == "RECOVERING"
    assert restarted.recovery_snapshot("account-a")["required"] is True
    try:
        restarted.accept_execution(
            account_id="account-a", signal_id="signal-b", idempotency_key="entry-b",
            canonical_hash="hash-b", execution_epoch=1, risk_assessment=assessment,
            signal_revision=1, order_payload={"symbol": "EURUSD", "volume": "1"}, now=at,
        )
    except ExecutionError as error:
        assert error.code == "EXPOSURE_GATE_CLOSED"
    else:
        raise AssertionError("restart accepted entry before reconciliation")

    try:
        restarted.reconcile_observation("account-a", {"complete": True, "orders": []})
    except ExecutionError as error:
        assert error.code == "INCOMPLETE_RECONCILIATION"
    else:
        raise AssertionError("incomplete broker snapshot was accepted")

    restarted.reconcile_observation("account-a", {
        "complete": True,
        "orders": [{"order_id": accepted.order.id, "status": "SUBMITTED"}],
        "fills": [],
        "positions": [],
    })
    assert restarted.recovery_snapshot("account-a")["status"] == "READY"
    assert restarted.account("account-a").exposure_gate == "OPEN"
print("ok")
`);
  assert.match(output, /ok/);
});

test("migration startup refuses a database newer than the image", () => {
  const output = run(`
from backend.app.migrate import SchemaCompatibilityError, validate_schema_compatibility

try:
    validate_schema_compatibility(
        {"001_foundation", "012_operational_convergence"},
        {"001_foundation", "011_runtime_interlocks"},
    )
except SchemaCompatibilityError as error:
    assert error.code == "DATABASE_SCHEMA_NEWER_THAN_IMAGE"
else:
    raise AssertionError("newer database schema was accepted")

validate_schema_compatibility(
    {"001_foundation", "010_execution_coordination_checkpoint"},
    {"001_foundation", "010_execution_coordination_checkpoint", "011_runtime_interlocks"},
)
print("ok")
`);
  assert.match(output, /ok/);
});

test("three mixed accounts dispatch and fail independently through recovery", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from backend.app.execution import ExecutionCoordinator
from backend.app.risk_calendar import RiskAssessment

class Broker:
    def __init__(self, account_id, result):
        self.account_id = account_id
        self.result = result
    def order_check(self, order): return order.account_id == self.account_id
    def order_send(self, order): return self.result

at = datetime(2026, 1, 1, tzinfo=timezone.utc)
accounts = [
    {"id": "demo-a", "environment": "DEMO"},
    {"id": "live-b", "environment": "LIVE"},
    {"id": "demo-c", "environment": "DEMO"},
]

def assessment(account_id, signal_id):
    return RiskAssessment(
        account_id, 1, True, purpose="PRE_ORDER", assessed_at=at,
        valid_until=at + timedelta(minutes=1), signal_revision=1, signal_id=signal_id,
    )

with TemporaryDirectory() as directory:
    engine = ExecutionCoordinator(state_path=f"{directory}/execution.json")
    orders = []
    for account in accounts:
        orders.append(engine.accept_execution(
            account_id=account["id"], signal_id=f"signal-{account['id']}",
            idempotency_key=f"entry-{account['id']}",
            canonical_hash=account["id"], execution_epoch=1,
            risk_assessment=assessment(account["id"], f"signal-{account['id']}"),
            signal_revision=1, order_payload={"symbol": "EURUSD", "volume": "1"}, now=at,
        ).order)
    results = [
        engine.dispatch_next(accounts[0]["id"], Broker(accounts[0]["id"], {"status": "SUBMITTED", "external_id": "a-1"})),
        engine.dispatch_next(accounts[1]["id"], Broker(accounts[1]["id"], "TIMEOUT")),
        engine.dispatch_next(accounts[2]["id"], Broker(accounts[2]["id"], {"status": "SUBMITTED", "external_id": "c-1"})),
    ]
    assert [result.status for result in results] == ["SUBMITTED", "UNKNOWN", "SUBMITTED"]
    assert engine.account(accounts[1]["id"]).exposure_gate == "QUARANTINED"
    assert engine.account(accounts[0]["id"]).exposure_gate == "OPEN"
    assert engine.account(accounts[2]["id"]).exposure_gate == "OPEN"

    operation = engine.begin_global_emergency([account["id"] for account in accounts], kind="STOP_ONLY")
    engine.converge_global_target(operation.id, accounts[0]["id"], resolved=True)
    engine.converge_global_target(operation.id, accounts[2]["id"], resolved=True)
    engine.converge_global_target(operation.id, accounts[1]["id"], resolved=False, detail="offline")
    assert operation.status == "INCOMPLETE"
    restarted = ExecutionCoordinator(state_path=f"{directory}/execution.json")
    restored = restarted.global_emergencies[operation.id]
    assert restored.target_account_ids == tuple(account["id"] for account in accounts)
    assert restored.targets[accounts[1]["id"]].status == "UNRESOLVED"
print("ok")
`);
  assert.match(output, /ok/);
});
