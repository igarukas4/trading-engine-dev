# Sandcastle delivery workflow

Use this runbook to configure, start, resume, or recover autonomous delivery
of this repository's `ready-for-agent` GitHub tickets. `Sandcastle` is the
tool; `Sentra` is the name sometimes used for the same delivery workflow.

The runner changes code, Git branches, merged history, and GitHub issue state.
Treat this document as the operational authority before invoking it.

## Delivery boundary

- Obtain explicit user approval before starting `npm run sandcastle` or
  `npm run sandcastle:watch`.
- Before asking for approval, show the default profiles from this runbook:
  planner, implementer, and reviewer use `gpt-5.6-luna` with `high` effort;
  merger uses `gpt-5.6-sol` with `low` effort. Ask the user to confirm those
  profiles before starting the runner.
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
| Merger | `gpt-5.6-sol`, `low` | Merge reviewed committed work, verify, and close the issue. |
| Human-facing coordinator | Active-session choice | Obtains approval, interprets evidence, and handles human gates. |

The four role profiles in `.sandcastle/main.mts` are the canonical
configuration for a default run. Keep them explicit rather than sharing one
agent object. Token usage may require changing these profiles at any time. If
the user requests a different profile after seeing the defaults, change only
`.sandcastle/main.mts` for that run and leave this runbook unchanged. Record
the confirmed profiles and any override in the Sandcastle log or the relevant
GitHub issue. A profile change does not affect an agent that is already
running.

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

To run an approved delivery phase without changing ticket labels, set
`SANDCASTLE_ISSUES` to its comma-separated issue numbers. The host selector
filters to that list before it dispatches, while still treating every open
issue as a blocker. The planner receives the same scope, but the host selector
remains authoritative for a scoped phase even if the planner returns an empty
or stale list. Omit the variable only when the whole ready backlog is approved
for one delivery run.

## Credentials and trust boundary

Use two independent credentials:

| Credential | Purpose | Where it belongs |
| --- | --- | --- |
| `GH_TOKEN` | GitHub issue read/comment/close operations in the sandbox | `.sandcastle/.env` or process environment; never commit it. |
| Proxy-managed Codex OAuth | ChatGPT Plus/Pro model access for Claude Code | Host `claude-code-proxy` state; never mount native CLI credentials into the sandbox. |

The local `claude-code-proxy` is an Anthropic-compatible gateway. It owns its
separate ChatGPT OAuth login and forwards the configured `gpt-*` models. Verify
it without printing sensitive values:

```powershell
claude-code-proxy codex auth status
docker info
curl.exe --connect-timeout 5 -sS -o NUL -w "HTTP %{http_code}\n" http://127.0.0.1:18765/v1/models
```

Create `.sandcastle/.env` from `.sandcastle/.env.example`; it must contain a
non-empty `GH_TOKEN` and the proxy settings. Docker Desktop reaches a host
loopback service through `host.docker.internal`, not `localhost`:

```text
ANTHROPIC_BASE_URL=http://host.docker.internal:18765
ANTHROPIC_AUTH_TOKEN=unused
ANTHROPIC_MODEL=gpt-5.6-sol[1m]
ANTHROPIC_SMALL_FAST_MODEL=gpt-5.6-luna[1m]
```

The proxy has no incoming client authentication. Keep it loopback-only or
otherwise restrict it with a firewall; never expose it through port forwarding,
public tunnels, permissive IPv6, or an untrusted network. Never paste tokens
into prompts, shell output, issue comments, or test fixtures.

## Reproducible environment preflight

Run these checks before the first run, after changing Dockerfile/lockfile, or
after repairing a sandbox environment:

```powershell
npm ci
npm run typecheck
npm test
docker build --tag sandcastle:trading-engine-v0 --file .sandcastle/Dockerfile .
docker run --rm --entrypoint claude sandcastle:trading-engine-v0 --version
docker run --rm --entrypoint sh sandcastle:trading-engine-v0 -lc 'curl --connect-timeout 5 -sS -o /dev/null -w "HTTP %{http_code}\n" http://host.docker.internal:18765/v1/models'
```

The Docker image supplies Node, Git, GitHub CLI, Claude Code CLI, Python
`pip`, and Python `venv`; the project lockfile supplies Node dependencies.
Sandcastle's hook uses `npm ci`, never an unconstrained install.
`.dockerignore` excludes the credential file, worktrees, logs, and local
dependency artifacts from the build context.

Before a long unattended run, do a non-agent sandbox smoke test: in a temporary
container filesystem, run `npm ci`, `npm run typecheck`, and `npm test`; also
confirm `python3 -m pip --version`, a temporary `python3 -m venv`, and
container-to-proxy connectivity succeed. This proves the image and gateway,
not broker integration.

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

## Bounded parallel delivery

The runner dispatches at most three independent tickets per iteration. This
is appropriate only when every selected ticket has no open declared blocker
and no shared migration, schema, API, infrastructure, or likely file/module
overlap. The host-side selector is authoritative; the planner provides
context but cannot override that safety check.

```powershell
npm run sandcastle
```

For a two-phase delivery, run each command from a clean `main` worktree:

```powershell
$env:SANDCASTLE_ISSUES = "58,59"
npm run sandcastle:watch

# After the human review of #59 passes:
$env:SANDCASTLE_ISSUES = "60,61,62"
npm run sandcastle:watch
```

One iteration has this lifecycle for each selected ticket:

```text
planner (context only)
        |
bounded deterministic batch of unblocked ready issues
        |
create one sandbox + sandcastle/issue-<number> per issue
        |
implementer -> reviewer (same sandbox and branch)
        |
serial merge + verification + GitHub issue close
        |
next iteration
```

Each implementation/review pipeline runs in its own sandbox. The merger
remains serial and verifies after every branch. Any failed pipeline stops the
batch before merging so its evidence is preserved for recovery.

For unattended rate-limit recovery, use:

```powershell
npm run sandcastle:watch
```

The supervisor records logs in `.sandcastle/logs/`, retries only rate-limit or
quota-shaped failures with bounded exponential backoff, and stops for other
failures. A clean exit can mean either no runnable `ready-for-agent` ticket or
completion; inspect the log and issue state before declaring the roadmap done.
On Windows, it invokes `npm` through `cmd.exe` so routine npm stderr notices do
not terminate the Stop-mode PowerShell supervisor before the run exit code and
log are captured.

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

Do not add tickets to the same batch merely because they are labelled
`ready-for-agent`; their declared blockers and likely overlap still govern
dispatch. Lower the cap or restore a one-ticket batch when a run requires a
shared API/schema decision or model quota becomes constrained.

## Recovery playbook

| Symptom | Evidence-first response |
| --- | --- |
| Rate limit or quota | Use `sandcastle:watch` or wait for its bounded retry; retain the ticket branch and inspect the log before changing model/profile. |
| Proxy unavailable | Check `claude-code-proxy serve`, `curl` on `127.0.0.1:18765/v1/models`, and Docker access through `host.docker.internal`; do not expose the unauthenticated proxy publicly. |
| Proxy OAuth/model failure | Check `claude-code-proxy codex auth status` and `claude-code-proxy models`; treat upstream quota/provider errors separately from GitHub authentication. |
| GitHub CLI failure | Check `gh auth status`, `.sandcastle/.env` presence, and token permission without printing its value. |
| No runnable issue | Inspect `ready-for-agent` labels, issue links in `## Blocked by`, and the selector/log. Do not force a blocked ticket. |
| No commit produced | Read the implementer log and issue state. Resolve the actual scope, test, or environment problem before a retry. |
| Worktree cleanup/ACL failure | Inspect ACL and `git worktree list`; preserve the affected branch/log; repair only the scoped worktree lifecycle. |
| Dirty host tree before merge | Treat it as user-owned work, isolate it from the merge decision, and request direction if scopes overlap. |

## Completion and resumption

After every stopped run, record the selected issue, branch, commits, test
results, log path, and unresolved state in the GitHub issue and Sandcastle log.
A replacement coordinator begins by reading `AGENTS.md`, this runbook, the
current Git status, the active GitHub issue, the ticket branch, and the
relevant Sandcastle log.
