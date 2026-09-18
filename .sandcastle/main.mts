// Dependency-aware delivery with review and serial merge
//
// This template drives a multi-phase workflow:
//   Phase 1 (Plan):             A planner analyzes open issues, builds a
//                               dependency graph, and outputs a <plan> JSON
//                               listing unblocked issues with branch names.
//   Phase 2 (Execute + Review): For each issue, a sandbox is created via
//                               createSandbox(). The implementer runs first
//                               (100 iterations). If it produces commits, a
//                               reviewer runs in the same sandbox on the same
//                               branch (1 iteration). All issue pipelines run
//                               concurrently via Promise.allSettled().
//   Phase 3 (Merge):            A single agent merges all completed branches
//                               into the current branch.
//
// The outer loop repeats up to MAX_ITERATIONS times so that newly unblocked
// issues are picked up after each round of merges.
//
// Usage:
//   npx tsx .sandcastle/main.mts
// Or add to package.json:
//   "scripts": { "sandcastle": "npx tsx .sandcastle/main.mts" }

import * as sandcastle from "@ai-hero/sandcastle";
import { docker } from "@ai-hero/sandcastle/sandboxes/docker";
import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { promisify } from "node:util";
import type { SandboxRunResult } from "@ai-hero/sandcastle";
import { z } from "zod";
import {
  selectDispatchableIssues,
  type ReadyIssue,
} from "./issue-selection.mts";

if (existsSync(".sandcastle/.env")) {
  process.loadEnvFile(".sandcastle/.env");
}

const ghToken = process.env.GH_TOKEN;
if (!ghToken) {
  throw new Error("GH_TOKEN must be set in .sandcastle/.env or the environment.");
}

const anthropicBaseUrl = process.env.ANTHROPIC_BASE_URL;
if (!anthropicBaseUrl) {
  throw new Error(
    "ANTHROPIC_BASE_URL must be set to the local Claude Code proxy URL.",
  );
}

const sandboxEnv = {
  GH_TOKEN: ghToken,
  ANTHROPIC_BASE_URL: anthropicBaseUrl,
  ANTHROPIC_AUTH_TOKEN: process.env.ANTHROPIC_AUTH_TOKEN || "unused",
  ANTHROPIC_MODEL: process.env.ANTHROPIC_MODEL || "gpt-5.6-sol[1m]",
  ANTHROPIC_SMALL_FAST_MODEL:
    process.env.ANTHROPIC_SMALL_FAST_MODEL || "gpt-5.6-luna[1m]",
  CLAUDE_CODE_AUTO_COMPACT_WINDOW: "272000",
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
  CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK: "1",
};

const planSchema = z.object({
  issues: z.array(
    z.object({ id: z.string(), title: z.string(), branch: z.string() }),
  ),
});

// The planner emits its plan as JSON inside <plan> tags; Output.object extracts
// and validates it against this schema. We use Zod here, but any Standard
// Schema validator works just as well — Valibot, ArkType, etc. See
// https://standardschema.dev.
type PlannedIssue = {
  id: string;
  title: string;
  branch: string;
};

const execFileAsync = promisify(execFile);

function phaseIssueNumbers(): Set<number> | undefined {
  const rawIssueNumbers = process.env.SANDCASTLE_ISSUES;
  if (!rawIssueNumbers) {
    return undefined;
  }

  const issueNumbers = rawIssueNumbers.split(",").map((value) => Number(value.trim()));
  if (
    issueNumbers.length === 0 ||
    issueNumbers.some((number) => !Number.isSafeInteger(number) || number < 1)
  ) {
    throw new Error(
      "SANDCASTLE_ISSUES must be a comma-separated list of positive issue numbers.",
    );
  }

  return new Set(issueNumbers);
}

const allowedPhaseIssues = phaseIssueNumbers();
const phaseDescription = allowedPhaseIssues
  ? [...allowedPhaseIssues].sort((left, right) => left - right).join(", ")
  : "all ready-for-agent issues";

async function nextUnblockedIssues(
  plannedIssues: PlannedIssue[],
): Promise<PlannedIssue[]> {
  const [readyResult, openResult] = await Promise.all([
    execFileAsync("gh", [
      "issue",
      "list",
      "--state",
      "open",
      "--label",
      "ready-for-agent",
      "--limit",
      "100",
      "--json",
      "number,title,body",
    ]),
    execFileAsync("gh", [
      "issue",
      "list",
      "--state",
      "open",
      "--limit",
      "100",
      "--json",
      "number",
    ]),
  ]);
  const readyIssues = JSON.parse(readyResult.stdout) as ReadyIssue[];
  const phaseReadyIssues = allowedPhaseIssues
    ? readyIssues.filter((issue) => allowedPhaseIssues.has(issue.number))
    : readyIssues;
  const openIssueNumbers = new Set(
    (JSON.parse(openResult.stdout) as Array<{ number: number }>).map(
      (issue) => issue.number,
    ),
  );
  const plannedIssueNumbers = allowedPhaseIssues
    ? new Set(phaseReadyIssues.map((issue) => issue.number))
    : new Set(plannedIssues.map((issue) => Number(issue.id)));

  return selectDispatchableIssues(
    phaseReadyIssues,
    openIssueNumbers,
    plannedIssueNumbers,
    MAX_CONCURRENT_ISSUES,
  ).map((issue) => ({
    id: String(issue.number),
    title: issue.title,
    branch: `sandcastle/issue-${issue.number}`,
  }));
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

// A run may progress through the whole code backlog with a bounded number of
// independent issues. If an agent or provider fails (including a rate limit),
// the loop stops cleanly; re-running resumes deterministic ticket branches.
const MAX_ITERATIONS = 100;
const MAX_CONCURRENT_ISSUES = 3;

// These checked-in role profiles are the canonical Sandcastle configuration.
// Change a profile only as a reviewed runner configuration change.
const plannerAgent = sandcastle.claudeCode("haiku", {
  effort: "medium",
  captureSessions: false,
});

const implementerAgent = sandcastle.claudeCode("haiku", {
  effort: "medium",
  captureSessions: false,
});

const reviewerAgent = sandcastle.claudeCode("haiku", {
  effort: "medium",
  captureSessions: false,
});

const mergerAgent = sandcastle.claudeCode("sonnet", {
  effort: "medium",
  captureSessions: false,
});

// npm ci installs the exact versions recorded in the committed lockfile.
const hooks = {
  sandbox: {
    onSandboxReady: [{ command: "npm ci" }],
  },
};

const sandboxProvider = docker({
  imageName: "sandcastle:trading-engine-v0",
  env: sandboxEnv,
});

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

for (let iteration = 1; iteration <= MAX_ITERATIONS; iteration++) {
  console.log(`\n=== Iteration ${iteration}/${MAX_ITERATIONS} ===\n`);

  // -------------------------------------------------------------------------
  // Phase 1: Plan
  //
  // The planning agent reads the open issue list,
  // builds a dependency graph, and selects the issues that can be worked in
  // parallel right now (i.e., no blocking dependencies on other open issues).
  //
  // It outputs a <plan> JSON block — Output.object parses and validates it.
  // -------------------------------------------------------------------------
  const plan = await sandcastle.run({
    hooks: hooks,
    sandbox: sandboxProvider,
    name: "planner",
    // One iteration is enough: the planner just needs to read and reason,
    // not write code. (Structured output requires maxIterations: 1.)
    maxIterations: 1,
    agent: plannerAgent,
    promptFile: "./.sandcastle/plan-prompt.md",
    promptArgs: { ALLOWED_ISSUES: phaseDescription },
    // Extract and validate the <plan> JSON into a typed object. Throws
    // StructuredOutputError if the tag is missing, the JSON is malformed, or
    // validation fails — which aborts the loop.
    output: sandcastle.Output.object({ tag: "plan", schema: planSchema }),
  });

  // Planner selection excludes likely-overlapping work; host-side selection
  // validates ready status and open blockers before enforcing the batch cap.
  const issues = await nextUnblockedIssues(plan.output.issues);

  if (issues.length === 0) {
    // No unblocked work — either everything is done or everything is blocked.
    console.log("No unblocked issues to work on. Exiting.");
    break;
  }

  console.log(
    `Planning complete. ${issues.length} issue(s) to work in parallel:`,
  );
  for (const issue of issues) {
    console.log(`  ${issue.id}: ${issue.title} → ${issue.branch}`);
  }

  // -------------------------------------------------------------------------
  // Phase 2: Execute + Review
  //
  // For each issue, create a sandbox via createSandbox() so the implementer
  // and reviewer share the same sandbox instance per branch. The implementer
  // runs first; if it produces commits, the reviewer runs in the same sandbox.
  //
  // Promise.allSettled means one failing pipeline doesn't cancel the others.
  // -------------------------------------------------------------------------

  const settled = await Promise.allSettled(
    issues.map(async (issue): Promise<SandboxRunResult> => {
      const sandbox = await sandcastle.createSandbox({
        branch: issue.branch,
        sandbox: sandboxProvider,
        hooks,
      });

      try {
        // Run the implementer
        const implement = await sandbox.run({
          name: "implementer",
          maxIterations: 100,
          agent: implementerAgent,
          promptFile: "./.sandcastle/implement-prompt.md",
          promptArgs: {
            TASK_ID: issue.id,
            ISSUE_TITLE: issue.title,
            BRANCH: issue.branch,
          },
        });

        // Only review if the implementer produced commits
        if (implement.commits.length > 0) {
          const review = await sandbox.run({
            name: "reviewer",
            maxIterations: 1,
            agent: reviewerAgent,
            promptFile: "./.sandcastle/review-prompt.md",
            promptArgs: {
              BRANCH: issue.branch,
            },
          });

          // Merge commits from both runs so the merge phase sees all of them.
          // Each sandbox.run() only returns commits from its own run.
          return {
            ...review,
            commits: [...implement.commits, ...review.commits],
          };
        }

        return implement;
      } finally {
        try {
          await sandbox.close();
        } catch (error) {
          console.warn(
            `Preserving ${issue.branch} worktree after cleanup failed: ${String(error)}`,
          );
        }
      }
    }),
  );

  // Log any agents that threw (network error, sandbox crash, etc.).
  for (const [i, outcome] of settled.entries()) {
    if (outcome.status === "rejected") {
      console.error(
        `  ✗ ${issues[i]!.id} (${issues[i]!.branch}) failed: ${outcome.reason}`,
      );
    }
  }

  if (settled.some((outcome) => outcome.status === "rejected")) {
    console.error(
      "A pipeline failed. Stopping so the next run can resume without spending more quota.",
    );
    process.exitCode = 75;
    break;
  }

  // Only pass branches that actually produced commits to the merge phase.
  // An agent that ran successfully but made no commits has nothing to merge.
  const completedIssues = settled
    .map((outcome, i) => ({ outcome, issue: issues[i]! }))
    .filter(
      (entry) =>
        entry.outcome.status === "fulfilled" &&
        entry.outcome.value.commits.length > 0,
    )
    .map((entry) => entry.issue);

  const completedBranches = completedIssues.map((i) => i.branch);

  console.log(
    `\nExecution complete. ${completedBranches.length} branch(es) with commits:`,
  );
  for (const branch of completedBranches) {
    console.log(`  ${branch}`);
  }

  if (completedBranches.length === 0) {
    // All agents ran but none made commits — nothing to merge this cycle.
    console.log("No commits produced. Stopping for human review before retrying.");
    process.exitCode = 2;
    break;
  }

  // -------------------------------------------------------------------------
  // Phase 3: Merge
  //
  // One agent merges all completed branches into the current branch,
  // resolving any conflicts and running tests to confirm everything works.
  //
  // The {{BRANCHES}} and {{ISSUES}} prompt arguments are lists that the agent
  // uses to know which branches to merge and which issues to close.
  // -------------------------------------------------------------------------
  await sandcastle.run({
    hooks: hooks,
    sandbox: sandboxProvider,
    name: "merger",
    maxIterations: 1,
    agent: mergerAgent,
    promptFile: "./.sandcastle/merge-prompt.md",
    promptArgs: {
      // A markdown list of branch names, one per line.
      BRANCHES: completedBranches.map((b) => `- ${b}`).join("\n"),
      // A markdown list of issue IDs and titles, one per line.
      ISSUES: completedIssues.map((i) => `- ${i.id}: ${i.title}`).join("\n"),
    },
  });

  console.log("\nBranches merged.");
}

console.log("\nAll done.");
