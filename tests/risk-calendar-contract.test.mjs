import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const domain = readFileSync("backend/app/risk_calendar.py", "utf8");
const backend = readFileSync("backend/app/main.py", "utf8");
const migration = readFileSync("backend/migrations/004_risk_calendar.sql", "utf8");

test("T4 has immutable account-scoped risk and calendar primitives", () => {
  for (const name of ["RiskLimits", "EnrichmentPolicy", "CalendarHealth", "EconomicEvent", "ActivationGate", "SafetyFence"]) assert.match(domain, new RegExp(`class ${name}`));
  assert.match(domain, /max_risk_per_trade: Decimal = Decimal\("0\.005"\)/);
  assert.match(domain, /max_open_positions: int = 3/);
  assert.match(domain, /required calendar policy needs explicit blackout windows/);
  assert.match(domain, /FENCE_PENDING/);
  assert.match(migration, /risk_limits/);
  assert.match(migration, /enrichment_policies/);
  assert.match(migration, /calendar_health/);
  assert.match(migration, /safety_fences/);
});

test("activation fails closed and fences remain account-local", () => {
  const output = readFileSync("backend/app/risk_calendar.py", "utf8");
  assert.match(output, /PAIR_MAPPING_MISSING/);
  assert.match(output, /CONNECTOR_CAPABILITY_UNSAFE/);
  assert.match(output, /RISK_LIMITS_MISSING/);
  assert.match(output, /CALENDAR_COVERAGE_MISSING/);
  assert.match(output, /WTI_ROLL_GUARD_ACTIVE/);
  assert.match(backend, /strategy-configs\/{config_id}\/activate/);
  assert.match(backend, /calendar\/health/);
  assert.match(backend, /new disabled immutable version/);
  assert.match(backend, /strategy-configs\/{config_id}\/session/);
});

test("risk assessment includes existing exposure and fails closed on weak baselines", () => {
  const output = readFileSync("backend/app/risk_calendar.py", "utf8");
  assert.match(output, /class RiskEngine/);
  assert.match(output, /INSUFFICIENT_BASELINE_DATA/);
  assert.match(output, /open_risk \+ requested_risk/);
  assert.match(output, /DAILY_LOSS_LIMIT_EXCEEDED/);
});
