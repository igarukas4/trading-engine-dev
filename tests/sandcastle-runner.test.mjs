import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const main = readFileSync(".sandcastle/main.mts", "utf8");
const planner = readFileSync(".sandcastle/plan-prompt.md", "utf8");
const supervisor = readFileSync(".sandcastle/run-supervisor.ps1", "utf8");

test("Sandcastle only plans ready agent tickets serially", () => {
  assert.match(planner, /--label ready-for-agent/);
  assert.match(main, /plan\.output\.issues\.slice\(0, 1\)/);
});

test("Sandcastle passes GitHub auth to trusted sandbox runs", () => {
  assert.match(main, /process\.loadEnvFile\("\.sandcastle\/\.env"\)/);
  assert.match(main, /const sandboxEnv = \{ GH_TOKEN: ghToken \}/);
});

test("Sandcastle uses the approved Codex model and effort", () => {
  assert.match(main, /codex\("gpt-5\.6-luna", \{ effort: "medium" \}\)/);
});

test("sandbox installs the committed Node dependency graph", () => {
  assert.match(main, /command: "npm ci"/);
  assert.doesNotMatch(main, /command: "npm install"/);
});

test("supervisor retries detected rate limits with bounded backoff", () => {
  assert.match(supervisor, /MaxRetries = 12/);
  assert.match(supervisor, /rate\[ -\]\?limit\|rate_limit/);
  assert.match(supervisor, /\$delayMinutes \* 2/);
});
