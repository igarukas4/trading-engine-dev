# Sandcastle delivery workflow

Use this runbook to configure, start, resume, or recover autonomous delivery
of this repository's `ready-for-agent` GitHub tickets. `Sandcastle` is the
tool; `Sentra` is the name sometimes used for the same delivery workflow.

The runner changes code, Git branches, merged history, and GitHub issue state.
Treat this document as the operational authority before invoking it.

## Delivery boundary

- Obtain explicit user approval before starting `npm run sandcastle` or
  `npm run sandcastle:watch`.
- Work only on open issues labelled `ready-for-agent`. The label means the
  ticket is specified well enough for autonomous work; `## Blocked by` still
  decides whether it is runnable.
- `ready-for-human` tickets need a human action or decision. In particular,
  D2 and D3 cover DEMO/LIVE operational gates and are outside autonomous code
  delivery.
- Preserve dirty host changes as user work. Resolve their scope before a merge;
  never reset, clean broadly, or use them as an implicit part of a ticket.
- Tests, fixtures, and local development never submit an order to a real
  broker. Production secrets never enter source, prompts, issue comments, or
  logs.

## What persists across agents and models

The durable context is the GitHub issue, committed code, ticket branch,
Sandcastle log, `CONTEXT.md`, `docs/spec/`, and this runbook—not a model's
hidden reasoning. A new coordinator or a later Sandcastle invocation must read
those artifacts and can safely continue a committed branch.

Changing a model does not alter an agent invocation already in progress. Stop
at a phase boundary, update the checked-in runner configuration, run its
verification, commit the change, then start the next invocation. Do not assume
a new model inherits an earlier model's private context.

## Effective runner profile

The checked-in runner is the source of truth. At the time of reading, inspect
`.sandcastle/main.mts` rather than relying on this table if the file has
changed.

| Runner role | Effective profile | Responsibility |
| --- | --- | --- |
| Planner | `gpt-5.6-luna`, `high` | Read backlog context and emit a plan; issue selection remains deterministic. |
| Implementer | `gpt-5.6-luna`, `high` | Complete one ticket, test it, and commit on its ticket branch. |
| Reviewer | `gpt-5.6-luna`, `high` | Review only that branch, make justified corrections, and verify. |
| Merger | `gpt-5.6-luna`, `high` | Merge reviewed committed work, verify, and close the issue. |
| Human-facing coordinator | Active-session choice | Obtains approval, interprets evidence, and handles human gates. |

The `codeAgent` object is currently shared by all four autonomous roles. If a
different worker-only profile is wanted, split that configuration deliberately,
cover it in `tests/sandcastle-runner.test.mjs`, run `npm run typecheck` and
`npm test`, and commit it before starting. A model/effort change is a runner
configuration change, not an in-flight steering mechanism.

## Admission: ticket and repository

Before a run:

1. Read `AGENTS.md`, this runbook, `CONTEXT.md`, the relevant `docs/spec/`
   contracts, and any applicable ADRs.
2. Read the selected GitHub issue, comments, parent issue/PRD, and relevant
   code/tests. Confirm that its acceptance criteria remain actionable.
3. Ensure the issue is `ready-for-agent`. Record real blockers as links under
   `## Blocked by`; use `ready-for-human` for a required human action,
   credential, external system, DEMO exercise, or LIVE decision.
4. Start from current `main`. Inspect `git status --short` and `git worktree
   list --porcelain`. Preserve unrelated work and stale worktree evidence.
5. Ensure runner files, `package.json`, `package-lock.json`, `.sandcastle/`,
   `AGENTS.md`, and `docs/agents/` are committed. Git worktrees inherit commits,
   not untracked configuration.

The runner queries all open `ready-for-agent` tickets and resolves blockers
locally. It selects the lowest-numbered issue whose linked blockers are not
also open eligible tickets. Keep labels and blocker links accurate; otherwise
the deterministic selector cannot represent the real dependency graph.

## Credentials and trust boundary

Use two independent credentials:

| Credential | Purpose | Where it belongs |
| --- | --- | --- |
| `GH_TOKEN` | GitHub issue read/comment/close operations in the sandbox | `.sandcastle/.env` or process environment; never commit it. |
| Codex ChatGPT login | Agent model authentication | Host `~/.codex`; mounted only into the trusted local sandbox. |

Before starting, verify without printing sensitive values:

```powershell
gh auth status
codex login status
docker info
```

Create `.sandcastle/.env` from `.sandcastle/.env.example`; it must contain a
non-empty `GH_TOKEN` with repository metadata and GitHub Issues read/write
permission. The runner loads it, passes the token as sandbox environment, and
keeps the source file ignored.

The host Codex cache is mounted read-only at `/home/agent/.codex-source` and
copied into container-native `/tmp/codex` for a run. This protects the host
cache from container writes while allowing session refresh in the container.
Use this mount only with the repository's trusted local Docker image. Never
paste tokens into prompts, shell output, issue comments, or test fixtures.

## Reproducible environment preflight

Run these checks before the first run, after changing Dockerfile/lockfile, or
after repairing a sandbox environment:

```powershell
npm ci
npm run typecheck
npm test
docker build --tag sandcastle:trading-engine-v0 --file .sandcastle/Dockerfile .
```

The Docker image supplies Node, Git, GitHub CLI, Codex CLI, Python `pip`, and
Python `venv`; the project lockfile supplies Node dependencies. Sandcastle's
hook uses `npm ci`, never an unconstrained install. `.dockerignore` excludes
the credential file, worktrees, logs, and local dependency artifacts from the
build context.

Before a long unattended run, do a non-agent sandbox smoke test: in a temporary
container filesystem, run `npm ci`, `npm run typecheck`, and `npm test`; also
confirm `python3 -m pip --version` and a temporary `python3 -m venv` succeed.
This proves the image and lockfile, not broker integration.

## Windows worktree health

Sandcastle creates temporary Git worktrees under `.sandcastle/worktrees/`.
Windows ACL `Deny` entries can prevent a container or Git from cleaning a
planner/issue worktree even when the source repository is healthy.

Before or after a failed cleanup, inspect without changing files:

```powershell
git worktree list --porcelain
(Get-Acl .sandcastle/worktrees).Access |
  Where-Object AccessControlType -eq Deny
```

The healthy state is no `Deny` rules and no unexpected registered worktree.
When cleanup fails, preserve the ticket branch and logs first. Prefer
Sandcastle's close path and Git worktree cleanup scoped to the affected
worktree; avoid broad host deletion. Once ACL inheritance is healthy, retry
from the existing deterministic ticket branch.

## Sequential delivery: default

Sequential delivery is mandatory for the current runner and is the correct
mode for the V0 dependency chain, shared API/schema decisions, overlapping
modules, and constrained ChatGPT quota.

```powershell
npm run sandcastle
```

One iteration has this lifecycle:

```text
planner (context only)
        |
deterministic next unblocked ready issue
        |
create sandbox + sandcastle/issue-<number>
        |
implementer -> reviewer (same sandbox and branch)
        |
merge + verification + GitHub issue close
        |
next iteration
```

The planner's structured output is context, not authority for dispatch. The
host-side selector in `.sandcastle/issue-selection.mts` enforces one eligible
ticket, so a planner output cannot create parallel work accidentally.

For unattended rate-limit recovery, use:

```powershell
npm run sandcastle:watch
```

The supervisor records logs in `.sandcastle/logs/`, retries only rate-limit or
quota-shaped failures with bounded exponential backoff, and stops for other
failures. A clean exit can mean either no runnable `ready-for-agent` ticket or
completion; inspect the log and issue state before declaring the roadmap done.

## Per-ticket completion contract

### Implementer

- Work on exactly the selected issue and branch.
- Read the issue and its contract before changing code.
- Apply `.sandcastle/CODING_STANDARDS.md`, preserving account scope,
  fail-closed behavior, broker truth, and no-real-order test boundaries.
- Use a tight red-green-refactor loop where applicable.
- Run `npm run typecheck` and `npm test` before committing.
- Commit with the required `RALPH:` format. If incomplete, leave a concise
  issue comment describing completed evidence and remaining blocker.
- Leave closing to the merge phase.

### Reviewer

- Review `main...<ticket branch>` and the issue acceptance criteria.
- Verify observable behavior, failure paths, account isolation, security,
  maintainability, and relevant trading safety invariants.
- Make only justified corrective/refinement commits on the same branch.
- Re-run verification. Preserve ticket scope and leave issue closing to merger.

### Merger

- Merge only reviewed branches containing commits.
- Resolve conflicts deliberately, verify after each merge, and preserve
  unmerged branches/worktrees as evidence when verification fails.
- Close only an issue whose branch was merged successfully, with the configured
  completion comment.

## Parallel delivery: exceptional mode

Do not enable parallelism merely because several tickets are labelled
`ready-for-agent`. It requires all of these to be true:

- no open dependency between the tickets;
- no shared migration, schema, API, infrastructure, or likely file/module
  overlap;
- separate branch and sandbox per complete ticket pipeline;
- an explicit small concurrency cap compatible with model quota; and
- a reviewed, tested, committed runner configuration change.

Review each branch in its own sandbox after its implementer. Merge serially and
verify each merge. Restore sequential mode after the independent batch.

## Recovery playbook

| Symptom | Evidence-first response |
| --- | --- |
| Rate limit or quota | Use `sandcastle:watch` or wait for its bounded retry; retain the ticket branch and inspect the log before changing model/profile. |
| Codex authentication failure | Check host `codex login status`, then the trusted mount/copy setup. Treat this separately from GitHub authentication. |
| GitHub CLI failure | Check `gh auth status`, `.sandcastle/.env` presence, and token permission without printing its value. |
| No runnable issue | Inspect `ready-for-agent` labels, issue links in `## Blocked by`, and the selector/log. Do not force a blocked ticket. |
| No commit produced | Read the implementer log and issue state. Resolve the actual scope, test, or environment problem before a retry. |
| Worktree cleanup/ACL failure | Inspect ACL and `git worktree list`; preserve the affected branch/log; repair only the scoped worktree lifecycle. |
| Dirty host tree before merge | Treat it as user-owned work, isolate it from the merge decision, and request direction if scopes overlap. |

## Completion and handoff

After every stopped run, record the selected issue, branch, commits, test
results, log path, and unresolved state in the issue or `HANDOFF.md`. Before
swapping coordinator/model/session, update `HANDOFF.md` with those durable
facts. A replacement agent begins by reading `AGENTS.md`, this runbook,
`HANDOFF.md`, the current Git status, and the active GitHub issue.
