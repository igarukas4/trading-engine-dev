import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const backend = readFileSync("backend/app/main.py", "utf8");
const system = readFileSync("frontend/app/system/page.tsx", "utf8");

test("System contract exposes authoritative health, recovery, and emergency progress", () => {
  for (const term of ["dashboard-summary-snapshot", "dashboard-snapshot", "calendar", "freshness", "resume-reconcile", "ATTENTION_REQUIRED", "UNKNOWN", "Diproses", "target_account_ids"]) assert.match(system, new RegExp(term));
  assert.match(backend, /global-emergency-operations\/\{operation_id\}\/resume-reconcile/);
  assert.match(backend, /expected_version/);
  assert.match(backend, /recovery_legal/);
});
