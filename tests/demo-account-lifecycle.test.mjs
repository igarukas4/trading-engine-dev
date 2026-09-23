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
main.lifecycle.record_risk_limits(account.id, version=1)
main.lifecycle.record_pair_mapping(account.id, "EURUSD", "EURUSD", valid=True)

client = TestClient(main.app)
headers = {"X-Dashboard-Session": "operator-72"}
missing_auth = client.post(
    f"/api/v1/broker-accounts/{account.id}/lifecycle/enable",
    json={"idempotency_key": "enable-no-auth", "expected_version": 1, "reason": "DEMO drill"},
)
assert missing_auth.status_code == 401

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
epoch = started["execution_epoch"]
stopped = command("stop", "stop-72", 3)
assert stopped["bot_state"] == "STOPPED" and stopped["execution_epoch"] > epoch
disabled = command("disable", "disable-72", 4)
assert disabled["lifecycle_status"] == "DISABLED" and disabled["bot_state"] == "STOPPED"
assert disabled["execution_epoch"] > stopped["execution_epoch"]
assert main.execution.orders == {} and main.execution.dispatch_records == {}
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

test("issue 72 migration, connector pins, and Windows Python instructions are present", () => {
  const migration = readFileSync("backend/migrations/014_demo_account_lifecycle.sql", "utf8");
  for (const term of [
    "lifecycle_commands", "lifecycle_readiness", "idempotency_key", "request_hash",
    "expected_version", "observed_version", "execution_epoch", "audit_id",
  ]) assert.match(migration, new RegExp(term));
  const requirements = readFileSync("connector/requirements.txt", "utf8");
  assert.equal(requirements, "MetaTrader5==5.0.6180\\nwebsockets==17.1\\n");
  const readme = readFileSync("connector/README.md", "utf8");
  for (const term of ["Python 3.14", "py -3.14 -m venv", "connector/requirements.txt", "pip check"]) {
    assert.match(readme, new RegExp(term.replaceAll(".", "\\.")));
  }
});
