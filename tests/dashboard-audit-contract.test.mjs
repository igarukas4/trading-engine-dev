import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";

const backend = readFileSync("backend/app/main.py", "utf8");
const dashboard = readFileSync("frontend/app/page.tsx", "utf8");
const layout = readFileSync("frontend/app/layout.tsx", "utf8");
const shell = readFileSync("frontend/app/components/operator-shell.tsx", "utf8");
const opportunities = readFileSync("frontend/app/opportunities/page.tsx", "utf8");

test("backend exposes account-scoped dashboard snapshots and audit hub", () => {
  for (const route of [
    '/api/v1/broker-accounts',
    '/api/v1/dashboard-summary-snapshot',
    '/api/v1/broker-accounts/{account_id}/dashboard-snapshot',
    '/api/v1/broker-accounts/{account_id}/audit-events',
    '/api/v1/commands/{command_id}',
  ]) assert.match(backend, new RegExp(`\\"${route.replaceAll("/", "\\/")}\\"`));
  assert.match(backend, /@app\.websocket\("\/ws\/v1\/dashboard"/);
  assert.match(backend, /snapshot\.required|snapshot_required/);
  assert.match(backend, /stream_watermark/);
});

test("operator shell exposes six areas and keeps account context explicit", () => {
  for (const label of ["Dashboard", "Markets", "Opportunities", "Positions", "Strategies", "System"]) {
    assert.match(shell, new RegExp(label));
  }
  assert.match(dashboard, /Ringkasan Semua Akun/);
  assert.match(dashboard, /critical|Kritis/i);
  assert.match(dashboard, /watchlist|Watchlist/i);
});

test("dashboard stream model isolates cursors and deduplicates event IDs", () => {
  const output = execFileSync("python3", ["-c", `
from backend.app.dashboard import DashboardHub

hub = DashboardHub(replay_limit=4)
hub.publish("system", None, "system.alert.raised", {"incident_id": "i-1"})
hub.publish("account", "a", "position.updated", {"position_id": "p-1"})
hub.publish("account", "b", "position.updated", {"position_id": "p-2"})
hello = hub.connect({"account_cursors": {"a": 0, "b": 0}, "system_cursor": 0})
assert {event["broker_account_id"] for event in hello["events"]} == {None, "a", "b"}
assert hello["events"][0]["stream"] == "system"
assert hub.apply_event(hello["events"][0]) is True
assert hub.apply_event(hello["events"][0]) is False
hub.publish("account", "a", "position.updated", {"position_id": "p-3"})
gap = hub.connect({"account_cursors": {"a": 99}, "system_cursor": 1})
assert any(item["type"] == "snapshot.required" and item["broker_account_id"] == "a" for item in gap["events"])
print("ok")
`], { encoding: "utf8" });
  assert.match(output, /ok/);
});

test("Opportunities uses an account-scoped fail-closed MANUAL command flow", () => {
  assert.match(opportunities, /Perlu tindakan/);
  assert.match(opportunities, /Diblokir \/ expired/);
  assert.match(opportunities, /Riwayat/);
  assert.match(opportunities, /confirmation|Konfirmasi/i);
  assert.match(opportunities, /Diproses/);
  assert.match(opportunities, /command_id/);
  assert.match(opportunities, /disabled/);
  assert.match(opportunities, /Idempotency-Key/);
  assert.doesNotMatch(opportunities, /updatedSignal\.status = "APPROVED"/);
  assert.match(backend, /account_data_status/);
  assert.match(backend, /ACCOUNT_DATA_UNSAFE/);
});

test("Opportunities exposes signal evidence and backend-defined actions for every mode", () => {
  for (const evidence of ["technical", "fundamental", "ai", "market_snapshot_id", "policy_version", "supersedes_signal_id"]) {
    assert.match(opportunities, new RegExp(evidence));
  }
  assert.match(opportunities, /AuditEvent|audit-events/);
  assert.match(opportunities, /SEMI_AUTO/);
  assert.match(opportunities, /FULL_AUTO/);
  assert.match(opportunities, /exactly one Order|satu Order/i);
  assert.match(opportunities, /eligibility|kelayakan/i);
  assert.match(opportunities, /safe next action|Tindakan aman/i);
  assert.match(opportunities, /SIGNAL_EXPIRED|SIGNAL_REVISION_SUPERSEDED/);
  assert.match(backend, /schedule_automated_signal/);
  assert.match(backend, /execution_mode == "SEMI_AUTO"/);
});
