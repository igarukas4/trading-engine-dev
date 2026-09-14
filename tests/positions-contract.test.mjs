import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const backend = readFileSync("backend/app/main.py", "utf8");
const positions = readFileSync("frontend/app/positions/page.tsx", "utf8");

test("Positions contract keeps account and pair confirmation explicit", () => {
  for (const route of [
    "/api/v1/broker-accounts/{account_id}/positions/{position_id}",
    "/api/v1/broker-accounts/{account_id}/positions/{position_id}/close",
    "/api/v1/broker-accounts/{account_id}/positions/{position_id}/reduce",
  ]) assert.ok(backend.includes(route), `missing route ${route}`);
  for (const phrase of ["Pair", "Direction", "current_pnl", "UNKNOWN", "reduce-only", "Saya mengonfirmasi"]) {
    assert.match(positions, new RegExp(phrase));
  }
});
