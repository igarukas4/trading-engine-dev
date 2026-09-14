import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const shell = readFileSync("frontend/app/components/operator-shell.tsx", "utf8");
const dashboard = readFileSync("frontend/app/page.tsx", "utf8");
const stream = readFileSync("frontend/app/lib/dashboard-stream.ts", "utf8");

test("release shell keeps route account context explicit, safe, and reachable on mobile", () => {
  assert.match(shell, /usePathname/);
  assert.match(shell, /router\.push/);
  assert.match(shell, /Ringkasan Semua Akun.*read-only/i);
  assert.match(shell, /const account_ids = selected \? \[selected\] : accounts\.map/);
  assert.match(shell, /aria-label="Navigasi lainnya"/);
  assert.match(shell, /href=\{`\/system\$\{query\}`\}/);
  assert.match(shell, /mobile-account-context/);
});

test("critical alerts preserve their BrokerAccount context when opened", () => {
  assert.match(dashboard, /criticalAlerts/);
  assert.match(dashboard, /href={`\/system\?account=\$\{encodeURIComponent\(alert\.broker_account_id\)\}`}/);
});

test("dashboard stream exposes stream-local resync state without mixing account cursors", () => {
  assert.match(stream, /resyncingAccounts/);
  assert.match(stream, /snapshot\.required/);
  assert.match(stream, /isResyncing/);
  assert.match(stream, /else this\.systemResyncing = true/);
});
