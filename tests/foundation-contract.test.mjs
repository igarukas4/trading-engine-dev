import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const backend = readFileSync("backend/app/main.py", "utf8");
const dashboard = readFileSync("frontend/app/page.tsx", "utf8");
const compose = readFileSync("compose.dev.yml", "utf8");
const migration = readFileSync("backend/migrations/001_foundation.sql", "utf8");

test("backend exposes read-only health and operator status contracts", () => {
  assert.match(backend, /@app\.get\("\/health\/live"/);
  assert.match(backend, /@app\.get\("\/api\/v1\/system\/status"/);
  assert.match(backend, /execution_available/);
  assert.match(backend, /unavailable/);
  assert.doesNotMatch(backend, /@app\.(put|patch|delete)\(/);
  assert.doesNotMatch(backend, /@app\.post\("\/api\/v1\/(orders|commands)/);
  assert.doesNotMatch(backend, /order_send|broker_connector/i);
});

test("operator surface renders backend unavailability instead of inferring health", () => {
  assert.match(dashboard, /Tidak tersedia/);
  assert.match(dashboard, /execution_available/);
  assert.match(dashboard, /NEXT_PUBLIC_API_BASE_URL/);
  assert.doesNotMatch(dashboard, /password|secret|token|order_send/i);
});

test("development composition and migration are present", () => {
  assert.match(compose, /postgres:/);
  assert.match(compose, /backend:/);
  assert.match(compose, /frontend:/);
  assert.match(compose, /psql .*ON_ERROR_STOP=1/);
  assert.match(migration, /CREATE TABLE IF NOT EXISTS schema_migrations/);
  assert.match(migration, /CREATE TABLE IF NOT EXISTS application_metadata/);
});
