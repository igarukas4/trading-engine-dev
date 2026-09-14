# Issue 48 acceptance record

Date: 2026-09-14

- Desktop navigation exposes Dashboard, Markets, Opportunities, Positions, Strategies, and System. Mobile keeps Dashboard, Markets, Opportunities, Positions, and an explicit `Lainnya` menu for Strategies and System; Emergency remains in the header above the mobile navigation.
- Route account context is retained during navigation and action scope. An unavailable context is removed from the route and replaced by the explicit `Ringkasan Semua Akun (read-only)` fallback. Critical alerts deep-link to the matching account System surface.
- The dashboard stream model deduplicates event IDs, records snapshot-required recovery separately per account or system stream, and keeps entry actions fail-closed through the existing account data status gate. It does not merge account cursors, MarketState, or quote data.
- `npm run typecheck`, frontend typecheck, frontend production build, and `node --test tests/release-acceptance.test.mjs` passed. The frontend image was built locally as `trading-engine-frontend:issue-48`.
- The full `npm test` command was attempted. Its 22 Python-backed tests could not start because this host resolves `python3` to the Windows Store stub; this is an environment prerequisite, not an assertion failure.
- No D2 operation, connector action, BrokerAccount activation, execution-mode change, broker Order, release command, or deployment was started for this acceptance pass.
