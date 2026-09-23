import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";

const run = (script) => execFileSync(
  process.execPath,
  ["tests/python.mjs", "-c", script],
  { encoding: "utf8" },
);

test("authenticated lifecycle API enables, starts, stops, and disables one ready DEMO account", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone
import asyncio
import httpx
from fastapi.testclient import TestClient

from backend.app import main

account = main.accounts.register(
    provider="MT5", broker_server="Broker-Demo", external_account_id="72001",
    display_name="Issue 72", environment="DEMO",
)
main.accounts.bind_connector(account.id, "x" * 40)
account.connector_healthy = True
account.lease_owner = "connector-session"
account.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
account.reconciliation_complete = True
account.reconciliation_observed_at = datetime.now(timezone.utc)
main.lifecycle.record_risk_limits(account.id, version=1)
main.lifecycle.record_pair_mapping(account.id, "EURUSD", "EURUSD", valid=True)

client = TestClient(main.app)
headers = {"X-Authenticated-User": "operator-72"}
missing_auth = client.post(
    f"/api/v1/broker-accounts/{account.id}/lifecycle/enable",
    json={"idempotency_key": "enable-no-auth", "expected_version": 1, "reason": "DEMO drill"},
)
assert missing_auth.status_code == 401

async def forged_request():
    transport = httpx.ASGITransport(main.app, client=("198.51.100.24", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as attacker:
        return await attacker.post(
            f"/api/v1/broker-accounts/{account.id}/lifecycle/enable",
            headers=headers,
            json={"idempotency_key": "forged", "expected_version": 1, "reason": "DEMO drill"},
        )

assert asyncio.run(forged_request()).status_code == 401

def command(action, key, version):
    response = client.post(
        f"/api/v1/broker-accounts/{account.id}/lifecycle/{action}", headers=headers,
        json={"idempotency_key": key, "expected_version": version, "reason": "DEMO drill"},
    )
    assert response.status_code == 200, response.text
    return response.json()

enabled = command("enable", "enable-72", 1)
assert enabled["lifecycle_status"] == "ENABLED" and enabled["bot_state"] == "STOPPED"
assert enabled["account_version"] == 2
assert enabled["command_id"] and enabled["audit_id"]
started = command("start", "start-72", 2)
assert started["bot_state"] == "RUNNING" and started["readiness"]["allowed"] is True
assert main.execution.orders == {} and main.execution.dispatch_records == {}
snapshot = client.get(f"/api/v1/broker-accounts/{account.id}/snapshot").json()
for field in ["allowed", "can_enable", "lease_current", "generation_current", "no_unknown", "execution_gate"]:
    assert field in snapshot["readiness"]
assert snapshot["readiness"]["allowed"] is True
replayed_enable = command("enable", "enable-72", 1)
assert replayed_enable["replayed"] is True
assert replayed_enable["lifecycle_status"] == "ENABLED"
assert replayed_enable["bot_state"] == "STOPPED"
assert replayed_enable["account_version"] == 2
assert replayed_enable["execution_epoch"] == enabled["execution_epoch"]
epoch = started["execution_epoch"]
stopped = command("stop", "stop-72", 3)
assert stopped["bot_state"] == "STOPPED" and stopped["execution_epoch"] > epoch
disabled = command("disable", "disable-72", 4)
assert disabled["lifecycle_status"] == "DISABLED" and disabled["bot_state"] == "STOPPED"
assert disabled["execution_epoch"] > stopped["execution_epoch"]
assert main.execution.orders == {} and main.execution.dispatch_records == {}
reenabled = command("enable", "reenable-72", 5)
assert reenabled["lifecycle_status"] == "ENABLED" and reenabled["bot_state"] == "STOPPED"
restarted = command("start", "restart-72", 6)
assert restarted["lifecycle_status"] == "ENABLED" and restarted["bot_state"] == "RUNNING"
print("ok")
`);
  assert.match(output, /ok/);
});

test("lifecycle repository reloads commands, audit, account state, and account-local readiness", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from backend.app.broker_accounts import BrokerAccount
from backend.app.lifecycle import LifecycleCoordinator, ReadinessContext

def ready(account):
    return ReadinessContext(
        binding_identity=account.identity, connector_healthy=True,
        lease_current=True, reconciliation_complete=True, no_unknown=True,
          runtime_interlock="ELIGIBLE", runtime_reason_codes=(), recovery_ready=True,
          risk_limits_version=3, pair_mappings={"EURUSD": "EURUSD.a"}, facts_complete=True,
    )

with TemporaryDirectory() as directory:
    path = f"{directory}/lifecycle.json"
    first = LifecycleCoordinator(state_path=path)
    account = BrokerAccount("MT5", "Broker-Demo", "72002", "Durable", id="account-a")
    other = BrokerAccount("MT5", "Broker-Demo", "72003", "Other", id="account-b")
    first.record_risk_limits(account.id, version=3)
    first.record_pair_mapping(account.id, "EURUSD", "EURUSD.a", valid=True)
    result = first.command(
        account, "enable", idempotency_key="same", expected_version=1,
        reason="operator requested DEMO", actor="operator", readiness=ready(account),
        fence=lambda allow: account.execution_epoch,
    )
    assert result.status == "ACCEPTED"
    audit_count = len(first.audits(account.id))

    reloaded = LifecycleCoordinator(state_path=path)
    restored = BrokerAccount("MT5", "Broker-Demo", "72002", "Durable", id="account-a")
    reloaded.restore_account(restored)
    assert restored.lifecycle_status == "ENABLED" and restored.version == 2
    replay = reloaded.command(
        restored, "enable", idempotency_key="same", expected_version=1,
        reason="operator requested DEMO", actor="operator", readiness=ready(restored),
        fence=lambda allow: restored.execution_epoch,
    )
    assert replay.command_id == result.command_id and replay.replayed is True
    assert len(reloaded.audits(account.id)) == audit_count
    assert reloaded.readiness_facts(account.id).risk_limits_version == 3
    assert reloaded.readiness_facts(account.id).pair_mappings == {"EURUSD": "EURUSD.a"}
    assert reloaded.readiness_facts(other.id).risk_limits_version is None
    assert reloaded.readiness_facts(other.id).pair_mappings == {}
    try:
        reloaded.command(
            restored, "enable", idempotency_key="same", expected_version=2,
            reason="changed payload", actor="operator", readiness=ready(restored),
            fence=lambda allow: restored.execution_epoch,
        )
    except ValueError as error:
        assert str(error) == "IDEMPOTENCY_CONFLICT"
    else:
        raise AssertionError("changed payload reused an idempotency key")
print("ok")
`);
  assert.match(output, /ok/);
});

test("deployed reconciliation acknowledgement follows lifecycle and fails closed by account", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.broker_accounts import AccountRegistry
from backend.app.execution import ExecutionCoordinator
from backend.app.lifecycle import LifecycleCoordinator
from backend.app.main import connector_reconciliation_acknowledgement

accounts = AccountRegistry()
execution = ExecutionCoordinator()
lifecycle = LifecycleCoordinator()
account = accounts.register(provider="MT5", broker_server="Demo", external_account_id="1", display_name="A", environment="DEMO")
other = accounts.register(provider="MT5", broker_server="Demo", external_account_id="2", display_name="B", environment="DEMO")
accounts.bind_connector(account.id, "a" * 40)
account.connector_healthy = True
account.lease_owner = "session"
account.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
account.reconciliation_complete = True
account.reconciliation_observed_at = datetime.now(timezone.utc)
lifecycle.record_risk_limits(account.id, version=1)
lifecycle.record_pair_mapping(account.id, "EURUSD", "EURUSD", valid=True)

observation = {"complete": True, "orders": [], "fills": [], "positions": [], "history_orders": [], "deals": []}
before = connector_reconciliation_acknowledgement(account, observation, [], None, execution, lifecycle, accounts)
assert before["backend_execution_gate"] is False
context = lifecycle.readiness_context(account, accounts.bindings.get(account.id), execution)
lifecycle.command(account, "enable", idempotency_key="e", expected_version=1, reason="demo", actor="op", readiness=context, fence=lambda allow: execution.set_lifecycle_gate(account.id, allow))
context = lifecycle.readiness_context(account, accounts.bindings.get(account.id), execution)
lifecycle.command(account, "start", idempotency_key="s", expected_version=2, reason="demo", actor="op", readiness=context, fence=lambda allow: execution.set_lifecycle_gate(account.id, allow))
running = connector_reconciliation_acknowledgement(account, observation, [], None, execution, lifecycle, accounts)
assert running == {
    "status": "RECONCILED", "recovery": [], "reconciliation_complete": True,
    "no_unknown_commands": True, "backend_execution_gate": True,
}
assert connector_reconciliation_acknowledgement(other, observation, [], None, execution, lifecycle, accounts)["backend_execution_gate"] is False
context = lifecycle.readiness_context(account, accounts.bindings.get(account.id), execution)
lifecycle.command(account, "stop", idempotency_key="x", expected_version=3, reason="demo", actor="op", readiness=context, fence=lambda allow: execution.set_lifecycle_gate(account.id, allow))
assert connector_reconciliation_acknowledgement(account, observation, [], None, execution, lifecycle, accounts)["backend_execution_gate"] is False
print("ok")
`);
  assert.match(output, /ok/);
});

test("stop fences and releases a queued entry while preserving reduce-only dispatch", () => {
  const output = run(`
from datetime import datetime, timedelta, timezone

from backend.app.broker_accounts import BrokerAccount
from backend.app.execution import ExecutionCoordinator
from backend.app.lifecycle import LifecycleCoordinator
from backend.app.risk_calendar import RiskAssessment

now = datetime.now(timezone.utc)
account = BrokerAccount("MT5", "Demo", "72004", "Fence", id="account-fence")
account.lifecycle_status = "ENABLED"
account.bot_state = "RUNNING"
account.version = 2
execution = ExecutionCoordinator()
assessment = RiskAssessment(
    account.id, 1, True, purpose="PRE_ORDER", assessed_at=now,
    valid_until=now + timedelta(seconds=30), signal_revision=1, signal_id="signal-fence",
)
created = execution.accept_execution(
    account_id=account.id, signal_id="signal-fence", idempotency_key="entry-fence",
    canonical_hash="entry-fence-hash", execution_epoch=1, risk_assessment=assessment,
    signal_revision=1, order_payload={"symbol": "EURUSD", "volume": "1"}, now=now,
)
record = execution.prepare_connector_dispatch(
    account.id, created.order.id,
    identity={"provider": "MT5", "broker_server": "Demo", "external_account_id": "72004"},
    generation=0,
)
assert created.reservation.status == "ACTIVE"
assert created.order.status == "INTENT"
position_source = execution.accept_execution(
    account_id=account.id, signal_id="signal-position", idempotency_key="position-source",
    canonical_hash="position-source-hash", execution_epoch=1,
    risk_assessment=RiskAssessment(
        account.id, 1, True, purpose="PRE_ORDER", assessed_at=now,
        valid_until=now + timedelta(seconds=30), signal_revision=1, signal_id="signal-position",
    ),
    signal_revision=1, order_payload={"symbol": "EURUSD", "volume": "1"}, now=now,
)
execution.record_fill(
    account.id, position_source.order.id, "deal-fence", "1",
    native_protection_confirmed=True, external_position_id="position-fence",
)
stopped = LifecycleCoordinator(execution=execution).command(
    account, "stop", idempotency_key="stop-fence", expected_version=2,
    reason="stop before dispatch", actor="operator",
    readiness=None, fence=lambda allowed: (
        execution.set_lifecycle_gate(account.id, allowed),
        execution.fence_entry_dispatches(account.id, account.execution_epoch) if not allowed else (),
    ),
)
assert stopped.status == "ACCEPTED"
assert stopped.interlock_state == execution.runtime_interlock(account.id).status
assert stopped.interlock_state == "BLOCKED"
assert record.state == "FENCED"
assert record.result_payload == {"state": "REJECTED", "code": "LIFECYCLE_FENCE"}
assert created.order.status == "REJECTED"
assert created.reservation.status == "RELEASED"
assert all(
    event.status == "ABORTED"
    for event in execution.events.values()
    if event.order_id == created.order.id
)
assert execution.pending_connector_dispatches(account.id) == ()
close = execution.request_position_close(
    account.id, position_source.order.id, "0.5", "EURUSD", "operator exit",
    confirmed=True, idempotency_key="close-after-stop",
)
exit_record = execution.prepare_connector_position_dispatch(
    account.id, close.id,
    identity={"provider": "MT5", "broker_server": "Demo", "external_account_id": "72004"},
    generation=0,
)
assert exit_record.command_type == "position.close"
assert exit_record.state == "QUEUED"
print("ok")
`);
  assert.match(output, /ok/);
});

test("issue 72 migration, connector pins, and Windows Python instructions are present", () => {
  const migration = readFileSync("backend/migrations/014_demo_account_lifecycle.sql", "utf8");
  for (const term of [
    "lifecycle_commands", "lifecycle_readiness", "idempotency_key", "request_hash",
    "expected_version", "observed_version", "execution_epoch", "audit_id",
  ]) assert.match(migration, new RegExp(term));
  const requirements = readFileSync("connector/requirements.txt", "utf8");
  assert.equal(requirements, "MetaTrader5==5.0.6180\nwebsockets==17.1\n");
  const readme = readFileSync("connector/README.md", "utf8");
  for (const term of ["Python 3.14", "py -3.14 -m venv", "connector/requirements.txt", "pip check"]) {
    assert.match(readme, new RegExp(term.replaceAll(".", "\\.")));
  }
});
