# Trading Engine V0 — handoff (2026-09-14)

## Next-session focus

Prioritise a UI/dashboard remediation plan and implementation before D2. The
deployed UI is functional but visibly bare: the source uses mostly unstyled
HTML, despite the canonical frontend decision calling for
Next.js/TypeScript/Tailwind/shadcn. Do not begin D2, D3, MT5 setup, DEMO/LIVE
operations, SEMI_AUTO/FULL_AUTO, or any broker order as part of UI work.

The relevant backlog references are:

- D1 deployment: [#26](https://github.com/igarukas4/trading-engine-dev/issues/26)
- Dashboard implementation: [#37](https://github.com/igarukas4/trading-engine-dev/issues/37)
  (closed, but UI quality should be reassessed)
- DEMO rollout: [#38](https://github.com/igarukas4/trading-engine-dev/issues/38)
- LIVE/FULL_AUTO rollout: [#39](https://github.com/igarukas4/trading-engine-dev/issues/39)
- Canonical specification: [#24](https://github.com/igarukas4/trading-engine-dev/issues/24)
- Frontend contract: `docs/spec/frontend-v0.md`

Do not create, close, or comment on an issue unless the operator asks.

## Current deployed state

- Public application: `https://trading.optitek.xyz/`.
- The shared host Caddy is the only process serving public 80/443. No second
  Caddy container is permitted.
- Basic Auth protects the dashboard and API; `/healthz` intentionally remains
  public and returns `ok`. Do not request, read, print, or accept its password
  in chat.
- Dashboard frontend is deployed through host Caddy to loopback-only
  `127.0.0.1:13000`; backend is loopback-only `127.0.0.1:18000`.
  PostgreSQL and Redis have no host ports and remain on the private network.
- The deployed backend reports `execution_available=false` and
  `trading_enabled=false`. No BrokerAccount, MT5 connector, execution mode,
  or broker order was configured or invoked during this deployment.
- The active release is the frontend-enabled shared-host-Caddy release made on
  2026-09-14. Use the documented release/rollback workflow in
  `deploy/README.md`; do not edit production secret files or print their
  contents.

## Deployment implementation references

- Commit `895a9d7` (`Deploy dashboard through shared host Caddy`) added the
  frontend production image, shared-host Compose service, Caddy routing, and
  release/smoke checks. It is pushed to `main`.
- The frontend GHCR build/runtime workflow passed:
  <https://github.com/igarukas4/trading-engine-dev/actions/runs/34820814124>.
- Shared-host architecture and operations: `deploy/README.md`,
  `deploy/compose.shared-host-caddy.yml`, and
  `deploy/caddy/Caddyfile.shared-host.example`.
- Production checkout is `/srv/trading-engine-v0` via SSH alias `hermes-vps`.
  Never alter `/home/ubuntu/trading-engine-dev`.
- Host Caddy site and protected local environment files are VPS-managed;
  validate before reloading Caddy and preserve the existing Hermes/Invoice/
  Booking routes.

## Verification already performed

- The frontend image workflow built the production image, started it, and
  asserted the Dashboard entrypoint.
- VPS Compose configuration and host Caddy configuration validated before
  reload.
- `scripts/release.sh deploy` smoke check passed after the frontend deploy.
- Public verification: unauthenticated `/` returns `401`; `/healthz` returns
  `ok`; authenticated API status asserts both execution flags are false.

## Working tree — preserve

Do not absorb these user-owned/unrelated changes into UI work:

- Modified `tests/news-enrichment.test.mjs`.
- Untracked `.agents/`, `.scratch-t5-diff.txt`, `CHANGE-BASIC-AUTH.md`,
  `backend/app/__pycache__/`, `docs/operations/`,
  `scripts/d2-demo-vps-wizard.sh`, and `skills-lock.json`.
- `HANDOFF.md` itself is intentionally untracked and was refreshed for this
  session transition.

## Suggested skills

- `codebase-design`: shape the dashboard design system and component seams
  before changing the UI.
- `tdd`: add UI/browser-level regression coverage while implementing the
  redesign.
- `diagnosing-bugs`: only if a deployed dashboard/API/Caddy behavior fails.
- `handoff`: before the next session transition.

## Latest session state (2026-09-14)

- Dashboard UI remediation has started and is deployed to production at
  `https://trading.optitek.xyz/`.
- Commit `1f597c8` added the shared operator shell: desktop sidebar, mobile
  bottom navigation, account context header, DEMO/LIVE badge, freshness
  indicator, and compact emergency dialog. It also added the global dark UI
  styling and Dashboard Variant A first slice.
- Commit `8c3f1dd` added the account-scoped Markets slice: pair/timeframe
  controls, watchlist, read-only candle visualization through the `MarketChart`
  adapter, and market stream resync status.
- Both frontend image workflows passed their dashboard entrypoint checks and
  the corresponding releases passed the production smoke check. The latest
  production frontend release is recorded on the VPS as
  `/srv/trading-engine-v0/deploy/releases/release-20260914T085207Z-26741.env`.
- No BrokerAccount has been configured or made active for the operator yet, so
  account-scoped Markets data cannot currently be demonstrated with live
  account context. This is expected and no broker/execution setup was done.
- The next recommended UI slice is **Opportunities**: newest-first signal
  list, actionability filters/labels, detail evidence, and explicit
  account/pair/reason confirmation for Approve and Execute. Deploy the slice
  through the existing frontend image workflow and shared-host release flow.
- Do not start D2 until the remaining UI remediation has had an acceptance
  pass, especially Opportunities/Positions/System safety states and command
  lifecycle feedback.
- Minor branding copy update is included in the next frontend release:
  sidebar subtitle `By O-O` and Dashboard greeting `Good morning, Chief`.
