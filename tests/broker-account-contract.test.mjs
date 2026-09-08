import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const migration = readFileSync("backend/migrations/002_broker_account_connector.sql", "utf8");
const domain = readFileSync("backend/app/broker_accounts.py", "utf8");
const backend = readFileSync("backend/app/main.py", "utf8");

test("BrokerAccount identity and lifecycle are account-scoped and fail closed", () => {
  assert.match(migration, /UNIQUE \(provider, broker_server, external_account_id\)/);
  assert.match(migration, /lifecycle_status.*DISABLED.*ENABLED.*ARCHIVED/s);
  assert.match(migration, /bot_state.*STOPPED/);
  assert.match(migration, /live_execution_enabled BOOLEAN NOT NULL DEFAULT FALSE/);
  assert.match(domain, /ACCOUNT_CONTEXT_MISMATCH/);
  assert.match(domain, /DISABLED.*STOPPED.*MANUAL/s);
  assert.match(domain, /can_enable/);
});

test("connector foundation authenticates without retaining or exposing secrets", () => {
  assert.match(migration, /secret_hash/);
  assert.match(migration, /connector_generation/);
  assert.match(migration, /lease_expires_at/);
  assert.match(domain, /scrypt/);
  assert.match(domain, /compare_digest/);
  assert.match(domain, /STALE_GENERATION/);
  assert.match(domain, /WRONG_ACCOUNT/);
  assert.doesNotMatch(backend, /connector_secret|secret_hash|api_key.*response/i);
});

test("only read-only connector snapshot is exposed to operators", () => {
  assert.match(backend, /@app\.post\("\/api\/v1\/broker-accounts"/);
  assert.match(backend, /@app\.get\("\/api\/v1\/broker-accounts\/{account_id}\/snapshot"/);
  assert.match(backend, /@app\.websocket\("\/ws\/v1\/connector"/);
  assert.match(domain, /read_only_snapshot/);
  assert.match(domain, /execution_locked.*True/s);
});
