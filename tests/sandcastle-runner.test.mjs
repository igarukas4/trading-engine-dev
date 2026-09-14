import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";

const main = readFileSync(".sandcastle/main.mts", "utf8");
const planner = readFileSync(".sandcastle/plan-prompt.md", "utf8");
const supervisor = readFileSync(".sandcastle/run-supervisor.ps1", "utf8");

test("Sandcastle selects a bounded batch of ready agent tickets", () => {
  assert.match(planner, /--label ready-for-agent/);
  assert.match(planner, /up to three/i);
  assert.match(main, /const MAX_CONCURRENT_ISSUES = 3;/);
  assert.match(main, /selectDispatchableIssues\(/);
  assert.match(main, /plan\.output\.issues/);
});

test("closed blockers do not prevent the next ready issue", () => {
  const program = `
    import assert from "node:assert/strict";
    import { selectNextUnblockedIssue } from "./.sandcastle/issue-selection.mts";
    const issue = selectNextUnblockedIssue([
      { number: 29, title: "T3", body: "## Blocked by\\n\\n- [T2](https://github.com/example/repo/issues/28)" },
      { number: 30, title: "T4", body: "## Blocked by\\n\\n- [T3](https://github.com/example/repo/issues/29)" },
    ]);
    assert.equal(issue?.number, 29);
  `;
  execFileSync("node", ["--import", "tsx", "--input-type=module", "--eval", program]);
});

test("open blockers outside the ready batch prevent dispatch", () => {
  const program = `
    import assert from "node:assert/strict";
    import { selectDispatchableIssues } from "./.sandcastle/issue-selection.mts";
    const issues = [
      { number: 44, title: "Ready", body: "## Blocked by\\n\\n- [Human gate](https://github.com/example/repo/issues/38)" },
      { number: 46, title: "Independent", body: "## Blocked by\\n\\nNone - can start immediately." },
      { number: 47, title: "Independent", body: "## Blocked by\\n\\nNone - can start immediately." },
      { number: 48, title: "Independent", body: "## Blocked by\\n\\nNone - can start immediately." },
      { number: 49, title: "Independent", body: "## Blocked by\\n\\nNone - can start immediately." },
    ];
    assert.deepEqual(
      selectDispatchableIssues(
        issues,
        new Set([38, 44, 46, 47, 48, 49]),
        new Set([44, 46, 47, 48, 49]),
        3,
      ).map((issue) => issue.number),
      [46, 47, 48],
    );
  `;
  execFileSync("node", ["--import", "tsx", "--input-type=module", "--eval", program]);
});

test("Sandcastle passes GitHub auth to trusted sandbox runs", () => {
  assert.match(main, /process\.loadEnvFile\("\.sandcastle\/\.env"\)/);
  assert.match(main, /const sandboxEnv = \{ GH_TOKEN: ghToken, CODEX_HOME: "\/tmp\/codex" \}/);
  assert.match(main, /sandboxPath: "\/home\/agent\/\.codex-source"/);
  assert.match(main, /readonly: true/);
});

test("Sandcastle uses the approved Codex model and effort", () => {
  assert.match(main, /codex\("gpt-5\.6-luna", \{\s+effort: "medium",/);
  assert.match(main, /captureSessions: false/);
});

test("sandbox installs the committed Node dependency graph", () => {
  assert.match(main, /command: "npm ci"/);
  assert.doesNotMatch(main, /command: "npm install"/);
});

test("supervisor retries detected rate limits with bounded backoff", () => {
  assert.match(supervisor, /MaxRetries = 12/);
  assert.match(supervisor, /rate\[ -\]\?limit\|rate_limit/);
  assert.match(supervisor, /\$delayMinutes \* 2/);
  assert.match(supervisor, /cmd\.exe \/d \/c/);
});
