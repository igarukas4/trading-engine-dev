# Issue 48 acceptance record

Date: 2026-09-15

- Desktop navigation exposes Dashboard, Markets, Opportunities, Positions, Strategies, and System. Mobile keeps Dashboard, Markets, Opportunities, Positions, and an explicit `Lainnya` menu for Strategies and System; Emergency remains in the header above the mobile navigation.
- Route account context is retained during navigation and action scope. An unavailable context is removed from the route and replaced by the explicit `Ringkasan Semua Akun (read-only)` fallback. Critical alerts deep-link to the matching account System surface.
- The dashboard stream model deduplicates event IDs, records snapshot-required recovery separately per account or system stream, and keeps entry actions fail-closed through the existing account data status gate. It does not merge account cursors, MarketState, or quote data.
- `npm run typecheck`, frontend typecheck, frontend production build, and `npm test` passed; the full suite reported 53 pass and 0 fail.
- The backend image workflow passed its unauthenticated liveness check and published `ghcr.io/igarukas4/trading-engine-v0-backend@sha256:295e59f5ca0febc195df0a4e6763be53cd18b90dc968ee19f1f2c09c0f379925` ([run 34868918315](https://github.com/igarukas4/trading-engine-dev/actions/runs/34868918315)).
- The frontend image workflow passed its root HTTP-document check and published `ghcr.io/igarukas4/trading-engine-v0-frontend@sha256:a3d8e68e0e427f1e63401bbc4446c666be5be9dfeeee44f8e577d26be2c3d465` ([run 34870063963](https://github.com/igarukas4/trading-engine-dev/actions/runs/34870063963)). The check deliberately validates HTTP success plus an HTML document rather than mutable UI copy.
- Before the VPS deployment recorded below, the local acceptance pass did not perform a D2 operation, connector action, BrokerAccount activation, execution-mode change, or broker Order.

## VPS deployment evidence

- On 2026-09-15, the authorised shared-host VPS deployment completed from `main` commit `6643674`. The host checkout fast-forwarded from the preflight revision before release.
- The protected release environment now pins backend `sha256:295e59f5ca0febc195df0a4e6763be53cd18b90dc968ee19f1f2c09c0f379925` and frontend `sha256:a3d8e68e0e427f1e63401bbc4446c666be5be9dfeeee44f8e577d26be2c3d465`.
- `scripts/release.sh deploy /etc/trading-engine/release.env` completed successfully with `SMOKE_BASIC_AUTH_PASSWORD_FILE` supplied by its protected `0600` file. Its shared-host smoke passed `/healthz`, unauthenticated and forged-header rejection, authenticated backend reachability, and an HTTP 200 HTML-document check for the frontend root.
- The first candidate encountered transient Caddy upstream readiness after container replacement; its automatic rollback restored the previous active release. The smoke check now retries the authenticated readiness request and uses the stable root HTTP-document contract. The regression test covers a `503` then `200` transition without exposing the Basic Auth password.

## Final Strategies acceptance

- Commit `6ca74c1` adds visible Strategies states for missing account context, loading, empty configuration, and API failure, with a retry action. The page follows account changes in the URL. It does not activate or modify a Strategy.
- Strategies UI regression tests passed (2/2), Strategy runtime tests passed (2/2), frontend typecheck passed, and the frontend production build passed.
- Frontend image workflow [34916448615](https://github.com/igarukas4/trading-engine-dev/actions/runs/34916448615) passed and published `ghcr.io/igarukas4/trading-engine-v0-frontend@sha256:412252f9d438611792579ef8da2dc1b49d7e3c0b6d6d85f9ecd34972efc03ade`.
- The shared-host deployment completed on 2026-09-15 as `/srv/trading-engine-v0/deploy/releases/release-20260915T011630Z-19244.env`. The release smoke passed for `trading.optitek.xyz`; the backend image and trading state were not changed.
- The operator reviewed the live Strategies page and accepted the UI. No BrokerAccount is active, so production account-specific Strategy data could not be reviewed. The empty account-context state is the expected production view.
- No D2/D3 operation, connector action, BrokerAccount activation, execution-mode change, or broker Order occurred during this final UI update or deployment.
