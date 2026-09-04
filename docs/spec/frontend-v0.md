# Trading Engine V0 — Frontend Specification

## 1. Purpose

This is the implementation contract for the single-user Indonesian dashboard. It exposes V0 market observation, account-scoped decision making, real-market execution controls, and operational recovery without weakening backend safety gates.

The UI is an operator surface, not a second trading engine. Backend state and command results are authoritative.

## 2. Scope

### Included

- Next.js + TypeScript + Tailwind + shadcn UI.
- Six surfaces: Dashboard, Markets, Opportunities, Positions, Strategies, System.
- Responsive desktop and mobile shell.
- Account-scoped REST snapshots and one resumable dashboard WebSocket.
- Lightweight Charts behind an application-owned chart adapter.
- Account-scoped commands, asynchronous command feedback, emergency controls, health visibility, and audit navigation.

### Excluded

- Direct broker, MT5, or WebSocket connector access from the browser.
- Generic write CRUD for Order, Fill, Position, or Signal.
- Paper execution, backtest UI, TradingView Advanced Charts, and chart-originated order entry.
- Production Telegram delivery implementation; V0 only defines alert-routing UI state and deep-link targets.

## 3. Non-negotiable UI invariants

1. **Account scope is visible and explicit.** Every account-detail route, query, chart, action, status, and deep link names one `BrokerAccount`. The UI never silently substitutes another account.
2. **Global summary is read-only.** It compares account summaries and critical/global alerts; it cannot submit account-specific commands or merge quotes/MarketState from distinct accounts.
3. **Exposure entry fails closed.** For an unavailable/stale/resyncing account stream, the UI disables entry-producing controls: approve, execute, start, activation, and mode changes. Emergency, close/reduce, reconciliation, and recovery remain reachable subject to backend legality.
4. **Acceptance is not final broker success.** A `202` command response is shown as `Diproses`; final UI state comes only from the matching event, authoritative snapshot, or final command resource.
5. **The browser never invents safety state.** It shows backend-derived readiness, risk, protection, calendar, and order state. A local UI transition cannot claim a command succeeded.
6. **Responsive safety controls remain reachable.** Emergency is compact but visually distinct and familiar: an icon-plus-label control in the shell, never a dominant full-width permanent banner. The confirmation flow names scope and close-all consequence.

## 4. Application shell and navigation

### Desktop

A persistent left sidebar contains Dashboard, Markets, Opportunities, Positions, Strategies, and System. The top context bar contains:

- selected account selector;
- small `DEMO`/`LIVE` badge;
- effective BotState/Mode and connection freshness;
- compact Emergency control;
- critical alert count.

### Mobile

A fixed bottom navigation contains Dashboard, Markets, Opportunities, Positions, and `Lainnya`. `Lainnya` opens a bottom sheet for Strategies and System. The same compact Emergency control remains in the top shell. Tap targets are at least 42px high; no safety control may be obscured by bottom navigation, keyboard, or a drawer.

### Critical-alert routing

Critical alerts are incident-keyed and account-scoped unless the backend marks them system/global. While the dashboard is active, a new incident renders one non-blocking toast/popup and the persistent critical-alert strip/count; repeated events for the same incident update that presentation rather than creating duplicate popups. When the operator is inactive, the UI creates a notification-handoff preview for the delivery layer (Telegram delivery itself is out of scope): title, concise incident state, named account/environment where applicable, and the canonical deep-link target.

Every alert deep link preserves the exact account context and opens the relevant detail or System recovery view. The all-account summary can link into an individual account alert but cannot convert that transition into an account command.

### Account context restoration

The landing view opens the last selected account only when it remains authorized and non-archived. Otherwise it opens **Ringkasan Semua Akun**, which is read-only. The selected account remains visible in the header and persists across page navigation. A route/query account ID is validated against the returned account list; invalid context falls back to the all-account summary with an explicit notice.

## 5. Dashboard

Dashboard uses the approved **Variant A — Watchlist-first** hierarchy.

1. Compact account/status bar and compact global critical-alert strip.
2. Account-scoped watchlist and one primary chart occupy the main visual priority.
3. Positions and eligible Signal actions follow the chart.
4. A hideable/reopenable pop-up **KPI drawer** contains only PnL hari ini, exposure, risk budget, and open-position count. Its small metadata line states the open account name and `Jenis akun: DEMO|LIVE`.

The all-account Dashboard contains only account summary cards, critical alerts, and global-emergency state. It never displays a combined price, combined MarketState, or account command.

## 6. Markets

Markets is account-scoped.

- Pair selector and timeframe selector lead the page.
- One primary `MarketChart` shows candles and read-only annotations for entry zone, SL/TP, active Position, and relevant Signal when available.
- A watchlist is scoped to the selected account.
- Expandable strips show Signal and MarketContext/EventRisk summary.
- No order-entry control originates from the chart. Users inspect and act through Opportunity/Signal flows.

`MarketChart` is an application adapter with inputs for candle data, quote telemetry, viewport, and annotations. Page components do not import Lightweight Charts directly. V0 implements the adapter with Lightweight Charts; a future Advanced Charts integration replaces the adapter implementation only after license and product fit are accepted.

## 7. Opportunities

Opportunities lists Signal/Opportunity records in descending creation time across the selected account. Actionability is explicit through labels and filters, not a grouping that overrides recency:

- `Perlu tindakan`: eligible, approved, or expiring;
- `Diblokir / expired`: shows reason code and safe next action;
- `Riwayat`: completed, rejected, superseded, or otherwise inactive.

A compact row/card opens a detail drawer/page with technical/fundamental/AI evidence, MarketStateSnapshot references, Signal expiry, RiskAssessment, policy/version information, and audit trail.

Mode-specific actions:

- `MANUAL`: Approve records approval only; Execute is a later distinct command.
- `SEMI_AUTO`: Approve schedules exactly one Order.
- `FULL_AUTO`: no per-Signal approval action; the UI shows why a Signal was or was not eligible.

`Approve` and `Execute` always require a compact, explicit operator confirmation that restates the selected account, Pair, action, and non-empty reason before the command is sent. `SEMI_AUTO` confirmation makes clear that confirming Approve schedules the Order; `MANUAL` confirmation makes clear that Approve and Execute are distinct. The confirmation is not a claim of final execution success. Every action is account-scoped and follows the command lifecycle in section 11.

## 8. Positions

Positions is the account-scoped operational source of truth for exposure.

The list exposes Pair, direction, volume, entry, current PnL, confirmed protection state, and a clear canonical-status label. Position detail exposes associated Order lifecycle, append-only Fills, reconciliation/audit facts, and broker-native protection changes.

Close and reduce-only actions require an explicit target Position, selected volume/all, and confirmation. They do not provide direct price editing in a table. The UI displays `UNKNOWN`, partial-fill, netting, and hedging information without collapsing them into misleading success labels.

## 9. Strategies

Strategies is the per-account control plane for `StrategyConfig`.

It lists active and inactive config versions with Pair, current version, EnrichmentPolicy/risk summary, and capability/coverage readiness. Detail is a drawer/page, not an inline mass editor.

Supported actions are Create/Edit version, Activate, Disable, inspect versions, and Copy to a specified target BrokerAccount. Copy creates an independent disabled target config; it never implies synchronization. Activation failure surfaces the backend reason: Pair mapping, calendar coverage, connector capability, or RiskLimits.

## 10. System

System is a health and audit hub, not a second trading dashboard. It contains:

- API/readiness, dashboard WebSocket, connector, and calendar health;
- account state/mode and freshness;
- global emergency status;
- recovery and reconciliation entries for `UNKNOWN`, blocked dispatch, quarantine, or `ATTENTION_REQUIRED`;
- chronological AuditEvent and command timeline;
- safe BrokerAccount, connector binding, Pair, and RiskLimits configuration entry points.

Routine trading actions stay on Opportunities and Positions. System may surface a recovery action only when the backend declares it legal.

## 11. Command interaction contract

Every account-scoped state-changing request uses its account path as the authority and sends `Idempotency-Key`, current `expected_version`, and non-empty `reason` as required by the backend. `POST /global-emergency` is the sole V0 global exposure-control acceptance command: it sends `Idempotency-Key` and non-empty `reason`, deliberately omits `expected_version`, and is tracked through its returned global operation resource. For an unresolved parent, `POST /global-emergency-operations/{operation_id}/resume-reconcile` is the only global recovery command; it sends `Idempotency-Key`, non-empty `reason`, and the current parent-operation `expected_version`.

The UI produces reasons with quick reason chips plus optional notes. Emergency dialogs use short explicit preset reasons and concise copy. Technical idempotency/version tokens are generated and tracked by the client; operators never type them.

On acceptance:

1. Freeze the initiating control for its idempotent request.
2. Show `Diproses` with a short command ID and link to command/detail status.
3. Do not show `Berhasil` until an event, snapshot, or command resource records final durable state.
4. Show `FAILED`, `UNKNOWN`, `ATTENTION_REQUIRED`, `ACCOUNT_CONTEXT_MISMATCH`, stale-version, expiry, or account-context errors with backend reason and available recovery/deep link.

Emergency presents one compact entry point that opens `Stop exposure`, `Stop + close akun ini`, and global `Stop + close all` choices. Close-all has a short confirmation and no extra acknowledgement checkbox. Global status is shown per immutable target: the operation remains `IN_PROGRESS` until every target converges to `COMPLETED`; any absent, offline, `UNKNOWN`, or `ATTENTION_REQUIRED` child remains visibly unresolved and exposes `resume-reconcile` only with the current parent version and only when backend-legal.

## 12. Realtime state model

### Initial load

- All-account landing loads `GET /dashboard-summary-snapshot`, which is also the authoritative consistent snapshot and `system_watermark` for the system stream.
- Account detail loads `GET /broker-accounts/{id}/dashboard-snapshot` for its account plus `GET /dashboard-summary-snapshot` for the globally visible system/critical-alert state; it never treats the account snapshot as a system snapshot.
- The client records each returned stream watermark before applying live events.

### WebSocket merge

One authenticated `/ws/v1/dashboard` connection resumes independent cursors for each available account and the system stream. The client:

- deduplicates `event_id`;
- applies events in the named account/system stream only;
- tracks sequence and aggregate version independently per stream;
- treats `replay.complete` as stream-local;
- never assumes a cross-account total order.

A stream gap, `snapshot.required`, stale cursor, or backpressure event reloads only that stream's consistent snapshot and resumes from its watermark: an account stream reloads `GET /broker-accounts/{id}/dashboard-snapshot`; the system stream reloads `GET /dashboard-summary-snapshot` and its `system_watermark`. Other healthy account streams continue without reload. The UI makes stale/resync state and last update time visible.

Quote telemetry uses `/ws/v1/quotes` separately. It is non-resumable and is never promoted to canonical lifecycle state.

## 13. API ownership map

| UI responsibility | Backend source |
| --- | --- |
| account context and summary | `/broker-accounts`, `/dashboard-summary-snapshot`, `/broker-accounts/{id}/dashboard-snapshot` |
| markets | `/broker-accounts/{id}/pairs`, `/broker-accounts/{id}/candles`, `/broker-accounts/{id}/market-state`, `/ws/v1/quotes` |
| opportunities | `/broker-accounts/{id}/opportunities`, `/broker-accounts/{id}/signals`, `/broker-accounts/{id}/risk-assessments` |
| positions | `/broker-accounts/{id}/positions`, `/broker-accounts/{id}/orders`, `/broker-accounts/{id}/fills`, `/broker-accounts/{id}/audit-events` |
| strategies | `/broker-accounts/{id}/strategy-configs`, config versions, enrichment policies, activation/copy/disable commands |
| system | `/health/*`, `/system/status`, `/commands/{id}`, `/global-emergency-operations/{operation_id}`, `POST /global-emergency`, `POST /global-emergency-operations/{operation_id}/resume-reconcile`, account-scoped bot-state/emergency/reconciliation/recovery commands |
| realtime | `/ws/v1/dashboard`, `/ws/v1/quotes` |

All list views use backend cursor pagination, stable ordering, filters, and explicit `has_more`; the frontend does not infer completeness.

## 14. Acceptance criteria

1. On desktop, all six pages are directly navigable; on mobile, the first four are in bottom nav and Strategies/System are reachable via `Lainnya`.
2. A compact emergency control remains visible and usable on both layouts without dominating the shell.
3. The selected account is present in every account-detail route and header; stale/invalid selection falls back explicitly to read-only all-account summary.
4. No all-account page enables an account-scoped command or combines broker-derived quote/MarketState values.
5. Dashboard Variant A uses watchlist + one chart as the primary visual surface and a hideable KPI drawer with account/environment small text.
6. A stale/resyncing account disables all entry-producing actions while keeping Emergency and close/reduce reachable when backend-legal.
7. Opportunities remain newest-first while exposing clear actionability labels/filters and exact reason codes.
8. MANUAL, SEMI_AUTO, and FULL_AUTO show the backend-defined approval/execute behavior; every visible `Approve` or `Execute` path requires compact explicit operator confirmation with account, Pair, action, and reason.
9. Position detail shows canonical order status, append-only Fills, protection state, and audit facts without claiming an `UNKNOWN` result succeeded.
10. Strategies is a per-account `StrategyConfig` control plane with version inspection, Activate/Disable, and Copy that creates an independent disabled target configuration rather than synchronization.
11. System is a health and chronological AuditEvent/command hub with global emergency and backend-legal recovery state, not a second trading dashboard.
12. Every risk-bearing command displays `Diproses` after acceptance and reaches final UI status only from authoritative event/snapshot/command state; quick reason chips plus optional notes produce a non-empty backend reason. The client supplies `Idempotency-Key` and `expected_version` for account-scoped commands, except that global emergency acceptance deliberately omits `expected_version`, while global `resume-reconcile` supplies the current parent-operation version; neither version token is exposed as an operator input.
13. WebSocket duplicate delivery is deduplicated; a gap/snapshot-required condition resyncs only the affected account or system stream.
14. Markets uses one primary account-scoped chart plus watchlist; chart implementation is consumed through `MarketChart` with no page importing Lightweight Charts directly. UI uses shadcn components, Indonesian text, and unambiguous `DEMO`/`LIVE`, command scope, freshness, and critical risk state.
15. A new critical incident produces one non-blocking toast/popup while the dashboard is active; repeats deduplicate by incident, inactive routing yields a Telegram-handoff preview, and every alert deep link preserves its exact account/detail context.
