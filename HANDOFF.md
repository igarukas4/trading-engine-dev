# Trading Engine V0 — handoff (2026-09-15)

## Next-session objective

Start with GitHub issue [#38, D2 DEMO rollout](https://github.com/igarukas4/trading-engine-dev/issues/38). It is open and labeled `ready-for-human`; all issues listed as its blockers are closed. D2 requires an operator to run real DEMO drills. Read `docs/operations/D2-human-prerequisites.md` and `docs/operations/D2-operator-drill-runbook.md` first.

Issue [#39, D3 LIVE/FULL_AUTO rollout](https://github.com/igarukas4/trading-engine-dev/issues/39) is also open, but it is blocked by D2 and issues #34-#37. Do not start D3 until D2 has complete evidence and the operator gives separate approval. No connector credential, BrokerAccount activation, execution-mode change, or broker Order is authorized by this handoff.

The canonical specification is [#24](https://github.com/igarukas4/trading-engine-dev/issues/24).
Read `docs/agents/sandcastle-delivery-workflow.md` before any Sandcastle/Sentra
operation, and `deploy/README.md` before any release-related action.

## Durable implementation state

- Branch `main` is synchronized with `origin/main` at `b2c453d`.
- Issue #48 is closed. Its final acceptance record is `docs/operations/issue-48-acceptance.md`.
- Production runs frontend commit `6ca74c1`, image digest `sha256:412252f9d438611792579ef8da2dc1b49d7e3c0b6d6d85f9ecd34972efc03ade`, release `/srv/trading-engine-v0/deploy/releases/release-20260915T011630Z-19244.env`. The shared-host release smoke passed.
- No BrokerAccount is active in production. Strategies displays an account-selection empty state until an account exists.
- Issues #38 and #39 are open. D2 is the next work item; D3 follows D2 and still needs its own operator approval.
- Backend/frontend test and image details are recorded in the issue acceptance record and GitHub Actions runs. Refer there instead of copying the release history here.

## Worktree ownership — preserve

Do not reset, clean, stage, or absorb these user-owned/generated changes:

- Modified: `.sandcastle/tsconfig.json`, `frontend/next-env.d.ts`,
  `tests/news-enrichment.test.mjs` (the `datetime.now(...)` change is user work).
- Untracked: `.agents/`, `.scratch-t5-diff.txt`, `CHANGE-BASIC-AUTH.md`,
  `backend/app/__pycache__/`, `docs/operations/D2-*.md`, `frontend/.next/`,
  `frontend/tsconfig.tsbuildinfo`, `scripts/d2-demo-vps-wizard.sh`, and
  `skills-lock.json`.
- `.venv/` is local test tooling created by `npm run setup:backend-test`; it
  should remain untracked.

Git may emit permission warnings for stale `.git/worktrees/sandcastle-issue-*`
during commits. Do not broadly delete them; follow the scoped Windows-worktree
recovery steps in `docs/agents/sandcastle-delivery-workflow.md` if cleanup is
actually needed.

## Suggested skills

- `wizard` only if the operator requests help preparing a human-run provisioning or drill workflow.
- `diagnosing-bugs` if a D2 preflight or drill fails.
- Read `deploy/README.md` before any deployment action.
