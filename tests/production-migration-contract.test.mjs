import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const backend = readFileSync("backend/app/main.py", "utf8");
const accounts = readFileSync("backend/app/broker_accounts.py", "utf8");
const migrator = readFileSync("backend/app/migrate.py", "utf8");
const sharedHost = readFileSync("deploy/compose.shared-host-caddy.yml", "utf8");
const dedicated = readFileSync("deploy/compose.production.yml", "utf8");

test("production applies ordered migrations before starting the backend", () => {
  for (const compose of [sharedHost, dedicated]) {
    assert.match(compose, /migrate:/);
    assert.match(compose, /condition: service_completed_successfully/);
    assert.match(compose, /command: \["python", "-m", "app\.migrate"\]/);
    assert.match(compose, /DATABASE_PASSWORD_FILE: \/run\/secrets\/postgres_password/);
  }
  assert.match(migrator, /glob\("\*\.sql"\)/);
  assert.match(migrator, /cursor\.execute/);
  assert.doesNotMatch(migrator, /from \.main import/);
  assert.match(backend, /DATABASE_PASSWORD_FILE/);
  assert.match(backend, /AccountRegistry\(settings\.database_url\)/);
  assert.match(accounts, /SELECT id, provider, broker_server, external_account_id/);
  assert.match(accounts, /INSERT INTO broker_accounts/);
  assert.match(accounts, /UPDATE broker_accounts/);
});
