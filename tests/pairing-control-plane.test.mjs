import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";

const run = (script) => execFileSync(process.execPath, ["tests/python.mjs", "-c", script], { encoding: "utf8" });

test("discovery pairing accepts one candidate and delivers a connector key once", () => {
  const output = run(`
from datetime import datetime, timezone, timedelta
from backend.app.broker_accounts import AccountRegistry
from backend.app.pairing import CandidateReport, PairingError, PairingRegistry

now = datetime(2026, 1, 1, tzinfo=timezone.utc)
accounts = AccountRegistry()
pairing = PairingRegistry(accounts)
started = pairing.start("dashboard-session", now=now)
assert len(started.device_code) >= 8
assert "device_code" not in pairing.view(started.session_id, now=now)

session = pairing.submit_candidate(
    started.device_code,
    connector_session_id="terminal-1",
    candidate=CandidateReport("MT5", "Broker-Demo", "12345", "DEMO", {"balance": "1000"}),
    now=now + timedelta(minutes=1),
)
assert session["status"] == "CANDIDATE_SUBMITTED"
assert session["candidate"]["external_account_id"] == "12345"
try:
    pairing.submit_candidate(started.device_code, "terminal-2", CandidateReport("MT5", "Broker-Demo", "12345", "DEMO"), now=now + timedelta(minutes=2))
except PairingError as error:
    assert error.code == "PAIRING_CODE_USED"
else:
    raise AssertionError("pairing code was reusable")

confirmed = pairing.confirm(started.session_id, "dashboard-session", display_name="Demo account", now=now + timedelta(minutes=2))
assert confirmed.account.environment == "DEMO"
assert len(accounts.accounts) == 1 and len(accounts.bindings) == 1
assert confirmed.connector_secret and confirmed.key_id
delivered = pairing.consume_key(started.session_id, "terminal-1")
assert delivered.connector_secret == confirmed.connector_secret
try:
    pairing.consume_key(started.session_id, "terminal-1")
except PairingError as error:
    assert error.code == "KEY_ALREADY_DELIVERED"
else:
    raise AssertionError("connector key was replayable")
print("ok")
`);
  assert.match(output, /ok/);
});

test("pairing expiration, cancellation, duplicate identity, and controls fail closed", () => {
  const output = run(`
from datetime import datetime, timezone, timedelta
from backend.app.broker_accounts import AccountError, AccountRegistry
from backend.app.pairing import CandidateReport, PairingError, PairingRegistry

now = datetime(2026, 1, 1, tzinfo=timezone.utc)
accounts = AccountRegistry()
pairing = PairingRegistry(accounts)
bounded = pairing.start("custodian", now=now)
for _ in range(4):
    try: pairing.submit_candidate("INVALIDCODE", "terminal", CandidateReport("MT5", "server", "bad", "DEMO"), now=now)
    except PairingError as error: assert error.code == "INVALID_DEVICE_CODE"
try: pairing.submit_candidate("INVALIDCODE", "terminal", CandidateReport("MT5", "server", "bad", "DEMO"), now=now)
except PairingError as error: assert error.code == "PAIRING_ATTEMPTS_EXCEEDED"
else: raise AssertionError("failed-code bound was not enforced")
expired = pairing.start("custodian", now=now)
try:
    pairing.submit_candidate(expired.device_code, "terminal", CandidateReport("MT5", "server", "1", "LIVE"), now=now + timedelta(minutes=10))
except PairingError as error:
    assert error.code == "PAIRING_EXPIRED"
else: raise AssertionError("expired code was accepted")

cancelled = pairing.start("custodian", now=now)
pairing.cancel(cancelled.session_id, "custodian", now=now)
try: pairing.confirm(cancelled.session_id, "custodian", display_name="cancelled", now=now)
except PairingError as error: assert error.code == "CANDIDATE_REQUIRED"
else: raise AssertionError("cancelled pairing was confirmed")

first = pairing.start("custodian", now=now)
candidate = CandidateReport("MT5", "server", "same", "LIVE")
pairing.submit_candidate(first.device_code, "terminal-1", candidate, now=now)
pairing.confirm(first.session_id, "custodian", display_name="first", now=now)
second = pairing.start("custodian", now=now)
pairing.submit_candidate(second.device_code, "terminal-2", candidate, now=now)
try: pairing.confirm(second.session_id, "custodian", display_name="second", now=now)
except PairingError as error: assert error.code == "DUPLICATE_IDENTITY"
else: raise AssertionError("duplicate full identity was accepted")

account = next(iter(accounts.accounts.values()))
accounts.set_execution_mode(account.id, "FULL_AUTO")
accounts.set_live_execution(account.id, True)
assert account.execution_mode == "FULL_AUTO" and account.live_execution_enabled
assert account.runtime_interlock == "BLOCKED"
print("ok")
`);
  assert.match(output, /ok/);
});

test("pairing and custodian controls are exposed through the public surfaces", () => {
  const backend = readFileSync("backend/app/main.py", "utf8");
  const migration = readFileSync("backend/migrations/009_discovery_pairing.sql", "utf8");
  const system = readFileSync("frontend/app/system/page.tsx", "utf8");
  for (const route of [
    "/api/v1/pairing-sessions",
    "/api/v1/pairing-sessions/{session_id}/confirm",
    "/api/v1/pairing-sessions/{session_id}/cancel",
    "/api/v1/connector/pairing-candidate",
    "/api/v1/broker-accounts/{account_id}/live-execution/enable",
    "/api/v1/broker-accounts/{account_id}/live-execution/disable",
  ]) assert.match(backend, new RegExp(`\\"${route.replaceAll("/", "\\/")}\\"`));
  assert.match(backend, /pairing_candidate/);
  assert.match(backend, /PAIRING_READ_ONLY/);
  assert.match(backend, /key_delivered|consume_key/);
  assert.doesNotMatch(migration, /device_code|connector_secret/);
  for (const term of ["Account Discovery Pairing", "FULL_AUTO", "Enable LIVE", "custodian controls"]) assert.match(system, new RegExp(term, "i"));
});
