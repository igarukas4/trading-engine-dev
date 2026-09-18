import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";

const main = readFileSync(".sandcastle/main.mts", "utf8");
const settings = readFileSync(".claude/settings.json", "utf8");
const envExample = readFileSync(".sandcastle/.env.example", "utf8");
const planner = readFileSync(".sandcastle/plan-prompt.md", "utf8");
const supervisor = readFileSync(".sandcastle/run-supervisor.ps1", "utf8");

test("Sandcastle selects a bounded batch of ready agent tickets", () => {
  assert.match(planner, /--label ready-for-agent/);
  assert.match(planner, /up to three/i);
  assert.match(main, /const MAX_CONCURRENT_ISSUES = 3;/);
  assert.match(main, /selectDispatchableIssues\(/);
  assert.match(main, /plan\.output\.issues/);
});

test("Sandcastle can constrain a delivery phase without weakening blockers", () => {
  assert.match(main, /process\.env\.SANDCASTLE_ISSUES/);
  assert.match(main, /phaseReadyIssues/);
  assert.match(main, /allowedPhaseIssues\s+\? new Set\(phaseReadyIssues\.map\(\(issue\) => issue\.number\)\)/);
  assert.match(main, /readyIssues\.filter\(\(issue\) => allowedPhaseIssues\.has\(issue\.number\)\)/);
  assert.match(main, /promptArgs: \{ ALLOWED_ISSUES: phaseDescription \}/);
  assert.match(planner, /This run is limited to these issue IDs: \{\{ALLOWED_ISSUES\}\}/);
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

test("Sandcastle passes GitHub and proxy auth to sandbox runs", () => {
  assert.match(main, /process\.loadEnvFile\("\.sandcastle\/.env"\)/);
  assert.match(main, /GH_TOKEN: ghToken/);
  assert.match(main, /ANTHROPIC_BASE_URL: anthropicBaseUrl/);
  assert.match(main, /ANTHROPIC_AUTH_TOKEN: process\.env\.ANTHROPIC_AUTH_TOKEN \|\| "unused"/);
  assert.match(envExample, /host\.docker\.internal/);
  assert.doesNotMatch(main, /CODEX_HOME|codex-source|\.codex/);
});

test("Sandcastle uses the canonical role-specific Claude Code profiles", () => {
  assert.match(
    main,
    /const plannerAgent = sandcastle\.claudeCode\("haiku", \{\s+effort: "high",/,
  );
  assert.match(
    main,
    /const implementerAgent = sandcastle\.claudeCode\("haiku", \{\s+effort: "high",/,
  );
  assert.match(
    main,
    /const reviewerAgent = sandcastle\.claudeCode\("haiku", \{\s+effort: "high",/,
  );
  assert.match(
    main,
    /const mergerAgent = sandcastle\.claudeCode\("sonnet", \{\s+effort: "low",/,
  );
  assert.match(main, /agent: plannerAgent/);
  assert.match(main, /agent: implementerAgent/);
  assert.match(main, /agent: reviewerAgent/);
  assert.match(main, /agent: mergerAgent/);
});

test("Sandcastle uses configurable gateway model aliases", () => {
  assert.match(main, /sandcastle\.claudeCode\("haiku"/);
  assert.match(main, /sandcastle\.claudeCode\("sonnet"/);
  assert.match(settings, /CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY/);
  assert.match(settings, /ANTHROPIC_DEFAULT_HAIKU_MODEL.*gpt-5\.6-luna/);
  assert.match(settings, /ANTHROPIC_DEFAULT_SONNET_MODEL.*gpt-5\.6-sol/);
  assert.match(settings, /ANTHROPIC_DEFAULT_OPUS_MODEL.*gpt-6-astra/);
  assert.match(settings, /ANTHROPIC_DEFAULT_FABLE_MODEL.*gpt-5\.6-terra/);
});

test("sandbox image installs Claude Code", () => {
  const dockerfile = readFileSync(".sandcastle/Dockerfile", "utf8");
  assert.match(dockerfile, /npm install -g @anthropic-ai\/claude-code/);
  assert.doesNotMatch(dockerfile, /@openai\/codex/);
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
