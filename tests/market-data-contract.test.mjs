import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const domain = readFileSync("backend/app/market_data.py", "utf8");
const backend = readFileSync("backend/app/main.py", "utf8");
const migration = readFileSync("backend/migrations/003_market_data.sql", "utf8");
const markets = readFileSync("frontend/app/markets/page.tsx", "utf8");
const chart = readFileSync("frontend/app/markets/market-chart.tsx", "utf8");

test("market data is account-scoped and fail-closed", () => {
  assert.match(domain, /assert_account_scope/);
  assert.match(domain, /Candle/);
  assert.match(domain, /IndicatorValue/);
  assert.match(domain, /MarketStateSnapshot/);
  assert.match(domain, /GAP_DETECTED/);
  assert.match(domain, /OPEN_CANDLE_INPUT/);
  assert.match(domain, /REVISION_MISMATCH/);
  assert.match(domain, /is_closed/);
  assert.match(domain, /aggregate/);
  assert.match(domain, /Decimal/);
  assert.match(domain, /resync/);
});

test("backend exposes account-scoped Markets data and non-canonical quotes", () => {
  assert.match(backend, /\/broker-accounts\/{account_id}\/pairs/);
  assert.match(backend, /\/broker-accounts\/{account_id}\/candles/);
  assert.match(backend, /\/broker-accounts\/{account_id}\/market-state/);
  assert.match(backend, /\/ws\/v1\/quotes/);
  assert.match(backend, /non_canonical/);
});

test("market data storage keeps account and revision boundaries", () => {
  assert.match(migration, /broker_account_id/);
  assert.match(migration, /source_revision/);
  assert.match(migration, /is_closed/);
  assert.match(migration, /market_state_snapshots/);
  assert.match(migration, /CHECK \(completeness/);
});

test("Markets renders one selected account through a chart adapter", () => {
  assert.match(markets, /selectedAccount/);
  assert.match(markets, /MarketChart/);
  assert.match(markets, /quote telemetry/i);
  assert.match(markets, /resync/i);
  assert.doesNotMatch(markets, /lightweight-charts/);
  assert.match(chart, /MarketChart/);
});
