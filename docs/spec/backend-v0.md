# Trading Engine V0 — Backend Specification

**Status:** Accepted — revised for simultaneous multi-account execution

**Original decision ticket:** [Spec: Backend V0 — data model, Strategy interface, pipeline, API/WS](https://github.com/igarukas4/trading-engine-dev/issues/5)

**Revision ticket:** [Revise Backend V0 for simultaneous multi-account execution](https://github.com/igarukas4/trading-engine-dev/issues/11)

**Domain language:** [`CONTEXT.md`](../../CONTEXT.md)

## 1. Objective

Build an implementation-ready FastAPI backend for a modular algorithmic trading engine that consumes live MT5 market data, evaluates deterministic strategy plugins, enriches Opportunities with news/context, applies a final deterministic risk gate, relays real-market orders to MT5, reconciles broker truth, and serves an Indonesian realtime dashboard.

V0 acceptance-tests at least three active BrokerAccounts simultaneously, in any `DEMO`/`LIVE` mix. Three is the minimum verified concurrent operating capacity, not a schema, configuration, or application hard cap. Each account owns an independent MT5 terminal/connector session, connector binding, BotState, Mode, live unlock, RiskLimits, StrategyConfig activation, Signal-to-execution pipeline, ordered AccountDispatch stream, reconciliation state, audit trail, and failure domain. The schema supports both MT5 netting and hedging accounts.

## 2. Scope

### Included

- Forex market data and real-market execution through a Windows MT5 connector.
- Persisted M1/M5/M15/H1/H4/D1 candles and computed indicators.
- Deterministic plug-and-play Strategy interface.
- Opportunity → Signal → RiskAssessment → Order → Fill → Position lifecycle.
- News ingestion and two-tier LLM analysis.
- Versioned per-StrategyConfig EnrichmentPolicy.
- Official first-party economic-calendar adapters and manual event overrides.
- Event-risk blackouts with implementation-configured windows.
- Per-account manual, semi-auto, and full-auto execution with the semantics in section 13.
- REST queries/commands and resumable WebSocket events.
- Permanent decision/execution audit trail.

### Excluded

- Backtesting and simulated/paper execution.
- MT4 and Binance connectors.
- TradingView Advanced Charts.
- Equity fundamentals and per-asset-class fundamental engines.
- Automated Forex Factory scraping, JSON ingestion, screenshots, or OCR.

## 3. Non-negotiable invariants

1. MT5 is authoritative for broker Order, Deal/Fill, and Position state.
2. An outbound broker intent is persisted before the connector receives it.
3. `order_send` timeout is `UNKNOWN`, never implicit failure and never a blind retry.
4. Every broker side effect uses an idempotency key and is reconciled after ambiguity/reconnect.
5. RiskEngine is deterministic and runs again immediately before an Order is relayed.
6. Expired Signals cannot be approved or executed.
7. New exposure is blocked in `STOPPED` and `EMERGENCY_STOP`; monitoring and exits remain active.
8. Emergency close-all runs only when an explicit command sets `close_all=true`.
9. MT5-native SL/TP is mandatory on entry; backend monitoring is not the sole protection.
10. Fill and AuditEvent records are append-only.
11. PostgreSQL is domain truth. Redis is cache/realtime fanout, never Order truth.
12. Dashboard summary statuses never drive domain behavior.
13. Storage timestamps are UTC. Device timezone affects display only, never candle boundaries.
14. A Strategy is deterministic and independent of news/AI.
15. External MT5 order, deal, and position identifiers are unique only within a BrokerAccount.
16. Every exposure-increasing command carries the current PostgreSQL `execution_epoch`; workers and connector reject a stale epoch immediately before MT5 invocation.
17. PRE_ORDER risk assessment, risk reservation, Order intent, and OutboxEvent commit atomically while holding the account execution/risk lock.
18. Requested broker protection is not treated as active until reconciled MT5 state confirms acceptable native SL/TP.
19. All account broker side effects and safety fences use one ordered account dispatch stream; the connector invokes at most one item at a time under an account mutex.
20. Automated netting reductions and trailing modifications are capability-gated; unsupported atomic/exclusive semantics fail closed rather than approximating safety.
21. Broker-derived quotes, candles, Pair mappings, spreads, and MarketState are account-scoped and never cross account boundaries.
22. RiskLimits, RiskAssessments, reservations, exposure, and execution locks are account-local; V0 has no GlobalRiskLimits or cross-account exposure/correlation cap.
23. No transaction acquires or holds execution/risk/dispatch locks for more than one BrokerAccount.
24. Connector loss/gap, reconciliation failure, blocked/dead-letter dispatch, UNKNOWN broker result, protection quarantine, and account-local overload block only the affected account. Other healthy accounts continue.
25. Only shared PostgreSQL/backend infrastructure failure and an explicit global emergency may have cross-account operational impact in V0.
26. BrokerAccount identity is immutable and unique by `(provider, broker_server, external_account_id)`; connector credentials and sessions bind to that full identity.
27. Connector generations are durable. Commands, journal records, and connector messages from a stale generation are rejected and cannot authorize or conceal broker effects.

## 4. Architecture

```text
Windows PC                                      Lighthouse VPS
┌──────────────────────────┐   outbound WSS    ┌─────────────────────────┐
│ MT5 Terminals (1/account)│◀─────────────────▶│ Connector Gateway       │
│ MetaTrader5 Python       │ quotes/M1/account │ FastAPI                  │
│ Connector Agent          │ + commands        └──────────┬──────────────┘
└──────────────────────────┘                               │
                                              ┌──────────────┴─────────────┐
                                              │ Data/Feature/Strategy      │
Official calendars ──▶ Event adapters ───────▶│ Decision/Risk/Execution   │
RSS/news ────────────▶ News + LLM pipeline ──▶│ Engines + Workers         │
                                              └───────┬──────────┬─────────┘
                                                      │          │
                                          PostgreSQL/Timescale  Redis
                                          ledger + outbox       cache/fanout
                                                      │
                                               REST + dashboard WS
```

FastAPI owns transport and command validation. Idempotent workers process a PostgreSQL outbox. A worker framework may be selected during implementation, but its task state is not domain state.

## 5. Common storage conventions

- Internal IDs: UUIDv7.
- External MT5 tickets/IDs: strings stored as unique external identifiers, never internal primary keys.
- Price, volume, money, percentages, scores: PostgreSQL `NUMERIC`; Python `Decimal`; no binary float in domain calculations.
- Time: UTC `timestamptz`.
- JSONB: limited to versioned parameters/configuration and raw external payloads.
- Mutable projections carry `version` for optimistic concurrency.
- Account-scoped records carry `broker_account_id`.
- Every account-derived reference must resolve to the same BrokerAccount; orphan or mixed-account graphs are rejected by constraints and command validation.
- Commands carry a caller-supplied `idempotency_key` unique within the normative command scope.
- Account-scoped uniqueness and idempotency keys include `broker_account_id`; shared source records remain explicitly account-neutral.

## 6. Data model

### 6.1 BrokerAccount — PostgreSQL

Lifecycle, environment, identity, and capabilities of one connected MT5 account.

Required fields:

- `id`, `provider`, immutable `broker_server`, immutable `external_account_id`, `display_name`, `environment`: `DEMO | LIVE`
- `lifecycle_status`: `DISABLED | ENABLED | ARCHIVED`; durable `live_execution_enabled`
- `accounting_mode`: `NETTING | HEDGING`
- `currency`, `leverage`, `trade_allowed`
- `execution_mode`: `MANUAL | SEMI_AUTO | FULL_AUTO`
- `execution_epoch`, `dispatch_fence_status`, `exposure_gate`: `OPEN | FENCE_PENDING | QUARANTINED`
- exposure-gate reason/version, `connector_status`, `reconciliation_status`
- account operating policy: connector-exclusive order/protection ownership flags; connector key/binding bound to `(provider, broker_server, external_account_id)`
- durable current connector `generation`, optional pending generation, lease owner/expiry, takeover state, and last drained generation
- `last_heartbeat_at`, `last_reconciled_at`, `reconciliation_watermark`
- `created_at`, `updated_at`, `version`

A new account is `DISABLED`, has BotState `STOPPED`, Mode `MANUAL`, and `live_execution_enabled=false`. Registration rejects an existing `(provider, broker_server, external_account_id)`. Provider, broker server, or external account changes require a new BrokerAccount; history is never rebound to another broker identity. Enabling requires a valid full-identity connector binding, complete reconciliation, active RiskLimits, valid Pair mappings, and healthy readiness gates. Disabling immediately closes the exposure gate and installs an ordered entry fence, then issues idempotent pending-entry cancellations without auto-closing positions. The command completes only after fence acknowledgement and broker convergence. A Fill that wins the cancellation race is reconciled, consumes risk, and quarantines the account; policy then requires a capability-safe reduce-only close or an `ATTENTION_REQUIRED` operator path. Monitoring, protection, close/reduce, reconciliation, and emergency remain available. An account may be archived only when it has no pending Order, open or UNKNOWN Position, UNKNOWN Command or unresolved broker-side effect, active RiskReservation, unresolved EmergencyOperation child, or unresolved GlobalEmergencyOperation target membership; archival preserves all history.

For `LIVE`, setting `live_execution_enabled=true` is a separate explicit audited command allowed only while effective BotState is `STOPPED` and connector, reconciliation, risk, calendar, mapping, and protection gates are healthy. It requires no minimum account age, elapsed demo period, or completed-order count. It changes neither Mode nor BotState and therefore never starts execution; after unlock, every Mode including `FULL_AUTO` is available subject to the ordinary gates. Setting it to false immediately closes the exposure gate, installs an ordered account fence, then issues idempotent pending-entry cancellations; monitoring, protection, close/reduce, reconciliation, and emergency actions continue, and existing positions are not auto-closed. Completion requires fence acknowledgement and broker convergence; a racing Fill follows the quarantine/reconcile/risk/close-or-attention rule above. The durable flag and any unacknowledged live fence survive backend/connector restart; recovery applies the fence before exposure eligibility and rejects queued or stale entry work that cannot prove it precedes the fence. `DEMO` accounts do not require live unlock. `ENABLED` means operationally eligible subject to all other gates; it does not imply `RUNNING`.

### 6.2 Pair — PostgreSQL

Canonical traded instrument and broker mapping.

Required fields:

- `id`, `canonical_code`, `broker_account_id`
- `base_currency`, `quote_currency`
- `broker_symbol`
- `digits`, `point_size`, `contract_size`
- `min_volume`, `max_volume`, `volume_step`
- `supported_order_types`, `enabled`
- `created_at`, `updated_at`

Pair routing uses metadata, never string parsing of an MT5 symbol with possible prefix/suffix.
The same canonical code may map differently in each account; `(broker_account_id, broker_symbol)` and `(broker_account_id, canonical_code)` are account-scoped unique. A Pair and all broker-derived observations through it belong to exactly one BrokerAccount.

### 6.3 Candle — TimescaleDB hypertable

Closed OHLCV candle.

Required fields:

- `broker_account_id`, `pair_id`, `timeframe`, `open_time`, `close_time`
- `open`, `high`, `low`, `close`
- `tick_volume`, `real_volume`, `spread`
- `source`, `source_revision`, `is_closed`, `ingested_at`

Unique key: `(broker_account_id, pair_id, timeframe, open_time, source_revision)` with a current-revision projection. Closed M1 is canonical input. Backend builds M5/M15/H1/H4. D1 follows that account's broker session boundary while timestamps remain UTC. Candles, quote-derived spread baselines, and derived indicators are never reused across accounts. Canonical closed candles are retained permanently and compressed; only derived caches and ingestion staging tables may use deletion-based retention policies.

### 6.4 IndicatorDefinition — PostgreSQL

Versioned indicator identity and parameter schema.

Fields: `id`, `key`, `implementation_version`, `parameter_schema`, `created_at`.

### 6.5 IndicatorValue — TimescaleDB hypertable

Computed numeric result for a Candle.

Fields:

- `broker_account_id`, `pair_id`, `timeframe`, `candle_open_time`
- `indicator_definition_id`, `parameter_hash`
- `value_name`, `value_numeric`, `computed_at`

Unique key covers BrokerAccount, Pair, timeframe, candle, definition version, parameter hash, and value name.

### 6.6 MarketStateSnapshot — PostgreSQL

Immutable input record for one Strategy evaluation.

Fields:

- `id`, `broker_account_id`, `pair_id`, `trigger_timeframe`, `trigger_time`
- ordered input sets for every required timeframe: exact Candle revision IDs, ascending open-time order, and configured lookback cardinality
- exact IndicatorValue IDs/definition versions/parameter hashes
- `market_regime`, `regime_model_version`
- `quote_observed_at`, `spread_at_evaluation`
- `completeness`: `COMPLETE | GAP_DETECTED`, plus missing-range reason codes
- `created_at`

`MarketState` is the immutable account-scoped in-memory value reconstructed from this snapshot: that account's Pair mapping; ordered closed bars for M5/M15/H1/H4/D1; exact indicator values; regime; and observed quote/spread. Every reference must belong to the same BrokerAccount. Strategies never receive incomplete input: lookback shortage, a gap, an open candle, revision mismatch, or account mismatch skips evaluation and writes an AuditEvent. It references normalized records rather than storing an opaque market-state JSON blob.

### 6.7 StrategyConfig — PostgreSQL

Account-owned parameterized Strategy instance.

Fields:

- `id` (immutable version ID), `logical_config_id`, `version_number`
- `strategy_key`, `strategy_version`, `name`
- `broker_account_id`, enabled Pair references
- `trigger_timeframe`, required timeframes
- versioned `parameters`
- `minimum_score`, `signal_ttl`
- `enrichment_policy_id`, `activation_status`: `DISABLED | ACTIVE`
- `effective_from`, `superseded_at`, `created_at`

Each update creates a new immutable row with a new `id` under a stable account-owned `logical_config_id`; `(broker_account_id, logical_config_id, version_number)` is unique. REST `{config_id}` always means the logical ID within its account; responses identify both logical ID and immutable version ID. Multiple configs may use the same Strategy plugin. Activation is account-specific and validates that account's Pair mappings, calendar coverage, connector capabilities, and RiskLimits.

Copying a StrategyConfig to another account creates a new logical config, initial immutable config version, and initial EnrichmentPolicy version owned by the target account. The copy is `DISABLED`, has no ongoing synchronization with its source, and must pass target-account activation independently.

### 6.8 EnrichmentPolicy — PostgreSQL

Immutable versioned policy owned by a StrategyConfig.

Fields:

- `id`, `strategy_config_id`, `version`, `effective_from`, `superseded_at`
- source rules: `REQUIRED | ADVISORY | DISABLED`
- source freshness TTLs
- explicit fallback weights/behavior
- event-risk enabled currencies and event kinds
- configurable pre/post blackout windows
- behavior for `FRESH | DEGRADED | STALE`
- `created_by`, `reason`, `created_at`

A Signal records the exact policy version used. StrategyConfig activation for a referenced Pair whose currencies lack required official-calendar coverage fails closed.

### 6.9 Opportunity — PostgreSQL

Single raw Strategy candidate.

Fields:

- `id`, `broker_account_id`, `evaluation_key` (account-scoped unique)
- `strategy_config_id`, `strategy_config_version_id`, `market_state_snapshot_id`, `pair_id`
- `direction`: `LONG | SHORT`
- `confidence`, `reason_codes`
- `created_at`, `status`

`evaluation_key` includes the immutable StrategyConfig version ID. No setup is represented by absence, not `Direction.NONE`. A LONG/SHORT conflict returns no Opportunity and records `CONFLICTING_SETUPS` in the evaluation AuditEvent.

### 6.10 NewsEvent — PostgreSQL

Normalized raw article/feed input.

Fields:

- `id`, `provider`, `external_id`, `source_url`
- `headline`, `published_at`, `observed_at`
- content hash/dedup key
- raw payload reference and expiry

Google News RSS is primary headline input; Investing.com RSS supplements forex coverage. NewsEvent and NewsAnalysis are shared account-neutral source records. Applying an analysis resolves its canonical pair codes/currencies through each target BrokerAccount's Pair mapping and persists an account-owned MarketContext/impact projection with the exact NewsAnalysis and Pair references used by Signal; it never imports broker-derived data from another account.

### 6.11 NewsAnalysis — PostgreSQL

Immutable structured LLM output.

Fields:

- `id`, `news_event_id`, affected canonical pair codes and currencies; never account-owned Pair IDs
- `directional_bias`, `sentiment`, `severity`, `confidence`
- `trade_impact`, `reason`, `expires_at`
- `model`, `prompt_version`, `schema_version`
- `raw_payload_ref`, `created_at`

Extractor uses a cheap structured-output model. Analyzer uses the next tier and escalates only low-confidence/conflicting bundles. Account application creates an account-owned context/impact projection that records `broker_account_id`, resolved `pair_id`, `news_analysis_id`, mapping version, policy version, and timestamps; Signal stores or references this exact projection set.

### 6.12 EconomicEventRevision — PostgreSQL

Immutable normalized revision from an official first-party calendar adapter.

Fields:

- `id`, `logical_event_id`, `revision`
- `provider`, `provider_event_id`, `source_url`
- `affected_currency`, `event_kind`, `impact`
- `scheduled_at`, `timing_precision`, `status`
- `observed_at`, `superseded_at`
- raw payload/hash

Only currencies used by active StrategyConfigs require adapters. EconomicEventRevision is a shared source record and may be reused, but calendar policy evaluation, blackout state, and safety fences fan out only to accounts whose active configs/Pairs are affected. Forex Factory is not ingested.

### 6.13 ManualEconomicEventOverride — PostgreSQL

Operator-created safety event.

Fields:

- `id`, `affected_currency`, `event_kind`, `impact`
- `scheduled_at`, `blackout_start`, `blackout_end`
- official `source_url`, `reason`, `expires_at`
- `created_by`, `created_at`, `revoked_at`

It may add or extend protection. Any shortening/removal requires an explicit audited operation. No OCR/screenshot workflow exists.

### 6.14 Signal — PostgreSQL

Enriched trade proposal.

Fields:

- `id`, `broker_account_id`, `opportunity_id`, `pair_id`, `strategy_config_id`
- `direction`, entry zone, SL, TP levels
- technical/fundamental/AI/final scores
- enrichment quality and policy version
- `created_at`, `expires_at`
- immutable `revision`, `supersedes_signal_id`, lifecycle status and reason codes

Normative lifecycle:

- `GENERATED → DEGRADED | BLOCKED_ENRICHMENT | BLOCKED_RISK | PENDING_APPROVAL | ELIGIBLE`
- `DEGRADED → BLOCKED_RISK | PENDING_APPROVAL | ELIGIBLE | EXPIRED | INVALIDATED`
- `PENDING_APPROVAL → APPROVED | REJECTED | EXPIRED | INVALIDATED`
- `APPROVED | ELIGIBLE → ORDERED | BLOCKED_RISK | EXPIRED | INVALIDATED`
- `BLOCKED_ENRICHMENT | BLOCKED_RISK | REJECTED | EXPIRED | ORDERED | INVALIDATED` are terminal for that immutable revision.

MANUAL and SEMI_AUTO route otherwise eligible Signals to `PENDING_APPROVAL`; FULL_AUTO routes them to `ELIGIBLE`. Re-scoring creates a new immutable Signal revision, supersedes and invalidates the prior revision, and invalidates its approval. Signal, Opportunity, StrategyConfig, MarketStateSnapshot, and Pair must share one BrokerAccount.

### 6.15 RiskLimits — PostgreSQL

Versioned account-level safety configuration.

Fields:

- `id`, `broker_account_id`, `version`
- max risk/trade, daily loss, open positions, total open risk, currency/correlation exposure
- spread/slippage/volatility guards and their baseline definitions/minimum sample requirements
- `protection_confirmation_timeout`, `max_protection_repair_attempts`
- effective timestamps, author, reason

All mechanisms are mandatory, versioned, and configurable per account. Accepted Moderate defaults are: maximum `0.50%` of equity risk per trade; `2%` daily loss; `3` open positions; `2%` total open risk; `1%` per currency/correlation bucket; spread no greater than `2x` the median for that Pair and broker session; slippage no greater than `0.15R`; and volatility no greater than `2.5x` median ATR. Baseline windows, session definitions, and minimum sample counts are explicit configuration; insufficient samples or stale baselines fail closed.

Daily loss is the decline from broker-trading-day starting equity and includes realized and floating PnL. A RiskLimits/configuration change does not auto-close an existing Position; existing exposure consumes current capacity. Tightening immediately closes the affected exposure gate and installs an ordered fence before issuing idempotent cancellation of newly noncompliant pending entries. Completion requires fence acknowledgement and broker convergence. If a Fill wins the race, reconciliation projects it, consumes/accounts risk, quarantines the account, and performs a capability-safe reduce-only close when policy requires; otherwise the operation becomes `ATTENTION_REQUIRED` for operator/broker-native resolution.

### 6.16 RiskAssessment — PostgreSQL

Immutable RiskEngine decision for a Signal and account snapshot.

Fields:

- `id`, `broker_account_id`, `signal_id`, `risk_limits_id`
- account snapshot timestamp/equity
- `approved`, reason codes
- requested/calculated PositionSize
- exposure before/after
- event-risk decision and event revision references
- `assessed_at`, `purpose`: `INITIAL | PRE_ORDER`
- immutable references to account/position/order snapshot, MarketStateSnapshot, BotState version, execution epoch, calendar health/observation, EconomicEvent revisions, and current quote/spread/volatility inputs
- `valid_until`, plus optional `risk_reservation_id`

An approved initial assessment does not replace mandatory pre-order assessment. PRE_ORDER uses server UTC, current safety policy/calendar health even when stricter than the Signal's historical enrichment policy, and expires before dispatch. Any changed epoch, input version, blackout revision, stale required source, or exceeded `valid_until` invalidates it.

### 6.17 RiskReservation — PostgreSQL

Durable reservation preventing concurrent Signals from over-allocating the same account risk. Fields: `id`, `broker_account_id`, `risk_assessment_id`, reserved monetary/currency/correlation exposure, `ACTIVE | PARTIALLY_CONSUMED | CONSUMED | RELEASED`, timestamps, and version. PRE_ORDER assessment, reservation, Order intent, and outbox commit atomically under a per-account PostgreSQL lock. Reject/cancel/expiry releases it; partial Fill consumes proportionally; reconciliation owns final release.

### 6.18 Order — PostgreSQL

Persisted broker instruction/intent.

Fields:

- `id`, `broker_account_id`, `signal_id`, `pre_order_risk_assessment_id`
- `command_id`, `idempotency_key`, canonical `request_hash`, `execution_epoch`
- `external_order_id`, unique as `(broker_account_id, external_order_id)` when present
- `order_type`, `direction`, requested volume and prices
- `requested_volume`, `cumulative_filled_volume`, `remaining_volume`
- `requested_sl`, `requested_tp`, time-in-force
- canonical status, raw broker retcode/payload reference
- timestamps and version

Enabled V0 types: Market, Buy/Sell Limit, Buy/Sell Stop. Buy/Sell Stop Limit exists in schema/capabilities but remains disabled until capability tests pass.

Canonical lifecycle:

```text
INTENT ──failed order_check──▶ REJECTED
   └──passed order_check──▶ CHECKED → DISPATCHING → SUBMITTED ─┬→ PARTIALLY_FILLED → FILLED
                                                              ├→ CANCELLED | REJECTED | EXPIRED
                                                              └→ UNKNOWN → reconciliation → canonical state
```

`order_send` is prohibited unless `order_check` passed and the execution fence still matches. A connector crash/timeout from `DISPATCHING` or later becomes `UNKNOWN`; it is never sent again automatically.

Dashboard may derive `PENDING | SUCCESS | FAILED` plus a detailed label. The summary is display-only.

### 6.19 Fill — PostgreSQL

Append-only broker execution.

Fields:

- `id`, `broker_account_id`, `order_id`, `position_id`
- `external_order_id`, `external_deal_id`, `external_position_id`
- broker-native deal `entry`: `IN | OUT | INOUT | OUT_BY`, deal type and reason
- normalized exposure effect: `INCREASE | REDUCE | REVERSE | CLOSE_BY | CHARGE | CORRECTION`
- `price`, `volume`, commission, swap
- `corrects_fill_id`, `filled_at`, raw payload reference

Unique `(broker_account_id, external_deal_id)` prevents duplicate replay. Late fee/swap/correction observations append records; they never mutate a Fill. Projection rules explicitly cover partial entry/close, INOUT reversal, OUT_BY close-by, late arrival, and charge/correction records.

### 6.20 Position — PostgreSQL projection

Latest known MT5 Position state.

Fields:

- `id`, `broker_account_id`, `pair_id`, `external_position_id`
- accounting mode, direction, volume, average entry
- requested/confirmed native SL and TP, `protection_state`, confirmation timestamp and broker retcodes
- trailing configuration, requested/confirmed protection version
- realized/unrealized PnL
- `OPEN | CLOSING | CLOSED | UNKNOWN`
- opened/updated/closed timestamps, version

Netting and hedging behavior follows BrokerAccount capability. `(broker_account_id, external_position_id)` is unique. Backend never invents aggregation that contradicts MT5. V0 allows one native TP per Position; multiple Strategy target levels become durable reduce-only PositionCommand children, never fictitious simultaneous native TPs.

### 6.21 PositionCommand — PostgreSQL

Durable canonical lifecycle for close/reduce/protection/trailing and pending-order cancel/modify commands. Fields include `id`, `broker_account_id`, target IDs, command type, idempotency key, request hash, execution epoch, expected target version/volume, reduce-only flag, requested/confirmed protection version, `RECEIVED | DISPATCHING | CONFIRMED | UNKNOWN | REJECTED | CANCELLED`, result, and timestamps. Hedging closes require exact external position ticket. Netting reductions are bounded by a fresh broker snapshot and connector-enforced `reduce_only`; any request capable of crossing zero is rejected.

### 6.22 Command — PostgreSQL

Canonical REST command receipt for all state-changing endpoints: `id`, authenticated principal, scope (`ACCOUNT | SYSTEM`), nullable account, endpoint/type, target, idempotency key, canonical request hash, expected version, status, result/error, created/completed timestamps. Account-command key scope is `(principal, broker_account_id, endpoint, idempotency_key)`; system/global-command key scope is `(principal, endpoint, idempotency_key)`. Records are retained permanently with audit. Same key+hash in the same scope returns the same resource; same key+different hash returns `409 IDEMPOTENCY_KEY_REUSED`; an ambiguous/in-progress broker result remains `202` with a poll URL.

### 6.23 ConnectorCommandJournal — durable connector-local store

The Windows connector fsyncs `RECEIVED` with `(broker_account_id, command_id)`, connector generation, dispatch sequence, idempotency key, canonical request hash, execution epoch, and request before processing. Backend dispatch and every connector request, acknowledgement, observation, and replay carry the current durable generation; backend and connector reject stale-generation traffic. One journal state machine applies everywhere:

```text
RECEIVED → INVOKING → CHECKING → CHECKED → DISPATCHING → RESULT_CONFIRMED
              │          │         │              └──────→ UNKNOWN
              └──────────┴─────────┴──restart──→ ABORTED_NOT_INVOKED
                         └──failed──────────────→ REJECTED
```

`INVOKING` orders the item against fences but does not mean a side-effecting MT5 call occurred. `DISPATCHING` is fsynced immediately before the side-effecting MT5 call and is the ambiguity boundary. Restart behavior is normative: RECEIVED re-enters ordered processing; INVOKING/CHECKING/CHECKED fsync `ABORTED_NOT_INVOKED`, store the outcome, advance the ordered sequence, idempotently reject/cancel the backend intent, and release its reservation without sending; DISPATCHING becomes UNKNOWN and reconciles, never resends; terminal states return stored results. Non-order side effects omit CHECKING/CHECKED but use the same RECEIVED → INVOKING → DISPATCHING boundary. Both command ID and idempotency key are account-scoped unique. Duplicate same-hash commands return stored state/result; different hashes are rejected. A stable correlation token is encoded in MT5 `magic` and/or `comment` where broker capability permits.

### 6.24 BotState — PostgreSQL projection

Per-BrokerAccount projection stores `effective_state: RUNNING | STOPPED | EMERGENCY_STOP` separately from nullable `requested_state`, plus reason, trusted actor, changed/requested timestamps, version, execution epoch, and durable pending `STATE_FENCE` sequence. A transition transaction records the request and pending sequence but leaves `effective_state` unchanged until the current connector generation applies and acknowledges that exact fence; only the acknowledgement transaction advances effective state and clears the request. API/UI expose both values and a pending-transition status and must never treat requested state as effective. Redis mirrors this projection for display/fast rejection only; dispatch fences always read PostgreSQL.

### 6.25 EmergencyOperation and GlobalEmergencyOperation — PostgreSQL

EmergencyOperation is the durable per-account convergence operation for `stop-only` or `stop+close-all`, recorded explicitly. Fields include `id`, `broker_account_id`, optional `global_operation_id`, explicit policy, immutable accepted target facts, `IN_PROGRESS | ATTENTION_REQUIRED | COMPLETED`, reason/actor, command correlation, and timestamps. It has one child action per pending entry Order and, for close-all, open or UNKNOWN Position and each unresolved broker-side effect. It repeatedly reconciles cancellation/close outcomes, adds a child if an entry fills while cancellation is in flight, accounts the Fill and quarantines before any capability-safe reduce-only close, and reaches `COMPLETED` only when MT5 confirms the requested state. Ambiguous children remain `UNKNOWN`, keep only that account quarantined, and raise an alert.

GlobalEmergencyOperation is the durable parent for the only global control in V0. The acceptance transaction persists the parent and one immutable target-membership row for every target, unique `(global_operation_id, broker_account_id)`, without creating account children in that transaction. Stop-only targets every BrokerAccount that is `ENABLED` at acceptance, including offline accounts. Close-all targets those accounts plus every non-`ARCHIVED` account—including `DISABLED`—that has any open or UNKNOWN Position, pending entry Order, active RiskReservation, unresolved broker-side effect or Command, or unresolved emergency target/child. Accounts created later are excluded. Membership facts include lifecycle and qualifying reasons observed at acceptance.

Child materialization then runs idempotently in separate single-account transactions until every membership has exactly one linked EmergencyOperation with the same policy; uniqueness on both membership and child linkage prevents duplication after crashes. Parent queries list every target and materialization/status state even before a child exists. The parent cannot become `COMPLETED` until every membership has a materialized child and every child has converged. An absent, offline, UNKNOWN, or `ATTENTION_REQUIRED` child remains unresolved and prevents completion. Bounded retries avoid hot polling; explicit reconcile/resume continues journal-first convergence. Archive validation treats unresolved target membership as a blocker independently of whether its child has been materialized.

### 6.26 AccountDispatchItem — PostgreSQL and connector journal

One monotonically sequenced stream per BrokerAccount contains every broker side effect plus `STATE_FENCE`, `LIVE_FENCE`, `CALENDAR_FENCE`, and generic `SAFETY_FENCE`. The backend allocates sequence numbers while holding the account dispatch lock. Connector processes exactly in sequence under one account mutex and never starts a second invocation concurrently. It verifies the item against its last applied fence and exposure gate, then fsyncs `INVOKING`; that journal write is the ordering linearization point. A fence linearizes when connector fsyncs/applies it and acknowledges its sequence; BrokerAccount `dispatch_fence_status` remains `FENCE_PENDING` until then without changing the BotState enum. Earlier commands may linearize before a later fence and are reconciled/closed as required; later or stale-epoch commands are rejected. Missing sequence causes quarantine and replay, not out-of-order execution.

### 6.27 AuditEvent — PostgreSQL append-only

Permanent record of strategy evaluations, conflicts, enrichment, risk decisions, commands, broker lifecycle, reconciliation, state transitions, and manual overrides.

Fields: `id`, scope (`ACCOUNT | SYSTEM`), nullable account/entity correlation IDs, event type, actor, reason codes, policy/config versions, timestamps, payload reference. Account activity always carries its BrokerAccount; only true global/system activity uses null account scope.

### 6.28 OutboxEvent — PostgreSQL

Transactional delivery record: stable `id`, stream kind (`ACCOUNT | SYSTEM`), owning `broker_account_id` when account-scoped, monotonic sequence within that stream, aggregate/type/version, payload reference, idempotency key, created/available timestamps, lease owner/expiry, published timestamp, attempts, next retry, dead-letter timestamp, and last error. Workers process at least once; consumers are idempotent. There is no total order across account streams or between an account stream and the system stream.

Raw broker and LLM payloads are retained for 90 days. AuditEvent remains permanently.

## 7. Strategy interface

```python
class Strategy(Protocol):
    key: str
    version: str

    def evaluate(
        self,
        market_state: MarketState,
        config: StrategyConfig,
    ) -> Opportunity | None: ...
```

Rules:

- Pure/deterministic for the supplied state and config.
- At most one candidate for one Pair/StrategyConfig/evaluation.
- Unique `evaluation_key = strategy_config_version_id + pair_id + trigger_time`.
- A conflict is rejected deterministically as `None` and audited as `CONFLICTING_SETUPS`.
- News, LLM output, mutable account state, and broker calls are not available inside Strategy.

## 8. Broker interface

```python
class Broker(Protocol):
    async def capabilities(self) -> BrokerCapabilities: ...
    async def account_snapshot(self) -> AccountSnapshot: ...
    async def market_snapshot(self, pairs: list[Pair]) -> MarketSnapshot: ...
    async def open_orders(self) -> list[BrokerOrder]: ...
    async def open_positions(self) -> list[BrokerPosition]: ...
    async def deals_since(self, cursor: str | None) -> DealPage: ...
    async def check_order(self, request: OrderRequest) -> OrderCheckResult: ...
    async def send_order(self, request: OrderRequest) -> OrderSendResult: ...
    async def modify_pending_order(self, request: ModifyPendingOrderRequest) -> BrokerResult: ...
    async def modify_position_protection(self, request: ModifyProtectionRequest) -> BrokerResult: ...
    async def cancel_order(self, request: CancelOrderRequest) -> BrokerResult: ...
    async def close_position(self, request: ClosePositionRequest) -> BrokerResult: ...
```

One Broker instance/session is bound to exactly one BrokerAccount identity `(provider, broker_server, external_account_id)` and current connector generation. The MT5 implementation is a remote adapter over outbound WSS. Every request and response carries that account context and generation, and every method that can change broker state receives `command_id`, idempotency key, canonical request hash, execution epoch, and expected target/protection version. The connector owns MT5 translation/IPC, its durable command journal, and last-moment fence/reduce-only enforcement; domain, risk, and strategy logic remain on the backend.

## 9. Pipeline — 14 observable steps

1. **Subscribe:** each account connector pushes only its quote updates and closed M1 candles.
2. **Build candles:** backend validates account-scoped M1 and closes higher timeframes within that account.
3. **Update indicators:** compute/persist account-scoped IndicatorValue and update ephemeral state.
4. **Detect events:** candle trigger, calendar blackout, news arrival, reconnect, or command.
5. **Evaluate strategies:** eligible StrategyConfigs evaluate on their declared closed trigger timeframe.
6. **Generate opportunities:** persist at most one Opportunity per evaluation key.
7. **Enrich:** assemble versioned MarketContext from news/calendar/fundamental inputs.
8. **AI validate:** structured extraction/reasoning when policy enables it; never per tick.
9. **Final score:** DecisionEngine applies the exact EnrichmentPolicy version.
10. **Risk engine:** create initial RiskAssessment and mark blocked/degraded/eligible state.
11. **Execute:** account Mode/approval/live/state gate, expiry check, serialize only that BrokerAccount, create fresh pre-order RiskAssessment + RiskReservation + Order intent/outbox atomically, revalidate fence/context, `order_check`, then journaled `order_send`.
12. **Monitor:** reconcile Order/Fill/Position/protection and manage versioned monotonic trailing modifications.
13. **Exit:** MT5-native SL/TP, manual close, strategy exit, or explicit emergency close-all.
14. **Log:** every transition writes AuditEvent and emits an outbox event.

The pipeline is event-driven. Quotes update ephemeral realtime state; they do not run all strategies/LLMs. News may update MarketContext and re-score active candidates but does not create a technical Opportunity.

## 10. Enrichment and event-risk behavior

- Each StrategyConfig owns a versioned EnrichmentPolicy.
- Every source is `REQUIRED`, `ADVISORY`, or `DISABLED` with explicit freshness and fallback behavior.
- Missing data is never silently treated as zero and weights are never silently renormalized.
- Official first-party calendar adapters cover currencies used by active StrategyConfigs.
- StrategyConfig activation for a referenced Pair fails closed when policy-required calendar coverage is absent.
- ManualEconomicEventOverride adds/extends safety windows with an official source URL and audit.
- Blackout numbers have no V0 hardcoded default in this spec; implementation must require explicit versioned configuration.
- When blackout begins, immediately close each affected exposure gate and install its ordered fence, then cancel pending entry orders idempotently. The transition completes only after fence acknowledgement and broker convergence. If a Fill wins the cancellation race, reconcile and account it, quarantine the account, then capability-safe reduce-only close when policy requires or enter `ATTENTION_REQUIRED` for operator/broker-native resolution. Close, reduce-only, SL/TP, and emergency actions remain permitted.
- Existing positions are not automatically liquidated before an event.
- Forex Factory is visual/manual reference only and is never automatically ingested.
- Freshness uses provider `observed_at` compared with server UTC; excessive adapter lateness or clock skew marks coverage unhealthy. Required stale/unhealthy coverage fails closed.
- PRE_ORDER references current event revisions and current calendar health. Safety-relevant calendar mutation means: any official event create/revision/reschedule/cancel, ManualEconomicEventOverride create/extend/revoke/expire, required-adapter health/freshness transition, or clock-skew/coverage transition that can change a blackout or fail-closed result. Shared source records are projected to the affected-account set from active Pair currencies and policies. For each affected account independently, a transaction under only that account's dispatch lock increments its safety version, allocates the CALENDAR_FENCE sequence, sets `dispatch_fence_status=FENCE_PENDING` and `exposure_gate=FENCE_PENDING`, and emits account outbox/audit rows. Unaffected accounts receive no fence. New exposure is rejected immediately on each affected account. Commands linearized before its barrier are ordered before its effective trading fence and reconciled; items after it see the new blackout. Fence acknowledgement opens the exposure gate only if no blackout/unhealthy/quarantine condition remains. No historical approval overrides current safety policy.
- News re-scoring creates a new immutable Signal revision and invalidates approval on the superseded Signal.

## 11. Execution lifecycle

```text
Signal
  → expiry/mode/approval check
  → per-account lock + PRE_ORDER RiskAssessment + RiskReservation
  → persist Order(INTENT) + reservation + OutboxEvent atomically
  → worker appends a sequenced AccountDispatchItem
  → connector processes it under the ordered account mutex after all earlier fences
  → connector journals idempotent command, applies fence state, and fsyncs INVOKING
  → MT5 order_check
  → MT5 order_send
  → SUBMITTED or UNKNOWN
  → reconcile orders/deals/positions
  → append Fill(s)
  → project Position
  → monitor native SL/TP + confirmed trailing changes
  → exit Fill(s)
  → Position CLOSED
```

- `order_check` is advisory, not execution.
- Failed `order_check` moves Order to REJECTED, releases its reservation, and prohibits `order_send`.
- The connector fsyncs RECEIVED before MT5 and DISPATCHING before `order_send`. A crash in DISPATCHING becomes UNKNOWN and cannot be replayed into another send.
- Every deal observation locks BrokerAccount, then affected Order, Position, and RiskReservation rows in deterministic ID order (or uses SERIALIZABLE isolation with bounded retry). In that transaction it first inserts the account-scoped external deal ID; a uniqueness conflict aborts the whole projection as a duplicate. It then appends Fill/charge/correction rows, updates Order cumulative/remaining volume, projects Position, consumes the matching reservation portion, releases terminal remainder, writes AuditEvent, and emits outbox events. `PARTIALLY_FILLED` may transition to `FILLED | CANCELLED | EXPIRED | REJECTED | UNKNOWN`; executed volume remains projected while only the unfilled reservation remainder is released at terminal resolution.
- MT5-native SL/TP is sent with entry, then requested versus broker-confirmed protection is reconciled. Before releasing its account mutex after observing a first Fill or unexpected protection/deal change, the connector durably closes its local exposure gate. Under the backend account-dispatch lock, the same projection transaction sets `PROTECTION_UNCONFIRMED`, `exposure_gate=QUARANTINED`, increments safety version, allocates a SAFETY_FENCE, and emits the dispatch/audit records. Thus queued later exposure fails at connector and backend. It starts the explicit `protection_confirmation_timeout` from RiskLimits. Repair attempts are bounded by `max_protection_repair_attempts`. A sequenced clear SAFETY_FENCE reopens the gate only after confirmation and no other blocking condition. At timeout/exhaustion, an idempotent reduce-only close is mandatory only when the Broker capability proves it cannot reverse exposure; otherwise the account remains quarantined with a critical alert and requires an operator or broker-native protective exit. If disconnected, state remains quarantined/alerting and reconciliation/confirm-or-safe-close is the first action after reconnect; the Position is never represented as safely protected without confirmation.
- Trailing uses closed/declared market inputs, broker digit/tick quantization, stop/freeze-level validation, and monotonically tighter SL changes. Automated trailing is enabled only when the Broker capability plus account operating policy establish connector-exclusive protection ownership. Unexpected external/manual protection change quarantines the account and disables automated modification pending reconciliation. Without atomic conditional-modify capability, V0 uses fixed native SL rather than automated trailing. Requested and confirmed protection versions differ until MT5 confirms; timeout becomes UNKNOWN and is reconciled, not blindly retried.
- Connector disconnect leaves the last confirmed native protection active.
- A close/reduce command in HEDGING mode names the exact MT5 position ticket. Automated NETTING reduction is enabled only after connector capability tests prove the provider's position-targeted close cannot reverse and the account accepts connector-exclusive order ownership. Otherwise it fails closed and requires an operator/broker-native protective exit. When enabled, it carries `reduce_only=true`, expected fresh volume/version, is serialized with every account side effect, and cannot cross zero; an unexpected external deal quarantines the account before further automation.
- On reconnect or UNKNOWN: quarantine new exposure; query order history, deals, and positions over an overlapping server-time window; match first by durable correlation and then authoritative broker relationships—never only symbol/volume/price/time; commit observations and cursor transactionally; resolve only a unique match or authoritative rejection. Ambiguous matches remain UNKNOWN with an operator alert. Cursor invalidation triggers bounded full-history fallback. Execution resumes only after a complete snapshot and explicit reconciliation watermark are committed.

## 12. Risk gate

RiskEngine evaluates each BrokerAccount independently against its current RiskLimits and evaluates at least:

- max risk per trade;
- broker-trading-day loss from starting equity, including realized and floating PnL;
- max open positions;
- total exposure;
- correlation/currency exposure;
- spread, slippage, and volatility guard;
- event-risk blackout/calendar health;
- BotState and emergency stop;
- Signal expiry;
- broker/account trade capability.

The Moderate defaults and fail-closed baseline requirements in section 6.15 apply unless an account activates another explicit RiskLimits version. Exposure-increasing assessment and reservation are serialized per BrokerAccount; separate accounts may reserve and execute concurrently. Existing exposure is included under current limits, while a stricter change never auto-liquidates it. Newly noncompliant pending entries use the ordered fence/cancel/racing-Fill convergence rule in section 6.15. A rejected assessment emits reason codes and never creates an executable broker command. A PRE_ORDER assessment is usable only until `valid_until` and only while every referenced account-local snapshot, event revision, effective BotState version, execution epoch, and live-unlock state still matches. V0 defines neither GlobalRiskLimits nor aggregate cross-account exposure/correlation controls.

## 13. Bot state machine

```text
STOPPED ──start──▶ RUNNING ──stop──▶ STOPPED
   │                   │
   └──── emergency ◀───┴──▶ EMERGENCY_STOP
                              │
                         reset-to-stopped
```

- State is persisted per BrokerAccount and mirrored in Redis.
- A newly created account starts `DISABLED` and `STOPPED`; first setup never enters RUNNING automatically.
- `STOPPED`: no new entry/order preparation; monitor and exits continue.
- `EMERGENCY_STOP`: no new exposure; monitor and exits continue.
- Every state transition increments `execution_epoch`, persists `requested_state` and pending STATE_FENCE sequence, allocates that sequenced fence, and sets BrokerAccount `dispatch_fence_status=FENCE_PENDING` in one PostgreSQL transaction. New exposure is rejected while pending. Effective BotState changes only when the current connector generation acknowledges that exact fence. A command whose INVOKING point precedes the barrier is ordered before the transition; later/stale commands are rejected. EmergencyOperation reconciles any earlier command that fills.
- `close_all=false` is the default explicit payload value for emergency command.
- `close_all=true` starts an EmergencyOperation that converges by reconciling audited idempotent close children for open positions and cancellation children for pending entries, including an entry that fills while cancellation is in flight.
- Restart reads each account's durable effective and requested state independently. Recovery replays or journal-first resolves any pending STATE_FENCE before considering automatic resume. A prior effective `RUNNING` account is displayed as `RECOVERING` readiness while its exposure gate is closed; `RECOVERING` is not a BotState enum value. It resumes `RUNNING` with its last Mode automatically only after no state fence remains pending and that account's connector, complete reconciliation, RiskLimits, calendar, mapping, live-unlock when applicable, and protection gates are healthy. Effective `STOPPED` and `EMERGENCY_STOP` never auto-resume; UI continues showing any requested transition separately.

Execution Mode is separate from BotState and persisted on BrokerAccount:

- `MANUAL`: pipeline creates Signals in PENDING_APPROVAL; approval alone does not trade. A separate explicit execute command creates exactly one Order intent.
- `SEMI_AUTO`: eligible Signals enter PENDING_APPROVAL; approval schedules exactly one Order intent under the Signal uniqueness constraint.
- `FULL_AUTO`: eligible Signals may schedule exactly one Order intent automatically.
- Mode changes are explicit per-account audited commands, require expected account version, increment `execution_epoch`, and never auto-execute pre-existing Signals. Start, stop, and Mode are never global in V0.

An account-local emergency creates one EmergencyOperation. A global emergency creates the GlobalEmergencyOperation snapshot and children defined in section 6.25. Each child installs and processes its own account fence; failure or delay on one child does not prevent healthy children from progressing.

## 14. REST contract

Base path: `/api/v1`. Caddy Basic Auth protects the single-user dashboard surface.

### Read/query endpoints

- `GET /health/live`, `GET /health/ready`
- `GET /broker-accounts`, `GET /broker-accounts/{id}`
- `GET /broker-accounts/{id}/pairs`, `GET /broker-accounts/{id}/pairs/{pair_id}`
- `GET /broker-accounts/{id}/candles?pair_id=&timeframe=&from=&to=`
- `GET /broker-accounts/{id}/market-state?pair_id=`
- `GET /broker-accounts/{id}/opportunities`, `GET /broker-accounts/{id}/opportunities/{opportunity_id}`
- `GET /broker-accounts/{id}/signals`, `GET /broker-accounts/{id}/signals/{signal_id}`
- `GET /broker-accounts/{id}/risk-assessments?signal_id=`
- `GET /broker-accounts/{id}/risk-limits`, `GET /broker-accounts/{id}/risk-limits/{version_id}`
- `GET /broker-accounts/{id}/orders`, `GET /broker-accounts/{id}/orders/{order_id}`
- `GET /broker-accounts/{id}/fills?order_id=&position_id=`
- `GET /broker-accounts/{id}/positions`, `GET /broker-accounts/{id}/positions/{position_id}`
- `GET /broker-accounts/{id}/bot-state`
- `GET /broker-accounts/{id}/audit-events`
- `GET /broker-accounts/{id}/emergency-operations/{operation_id}`
- `GET /system/status`
- `GET /commands/{id}`
- `GET /global-emergency-operations/{operation_id}`
- `GET /broker-accounts/{id}/dashboard-snapshot`
- `GET /dashboard-summary-snapshot`

All account paths reject a target or filter owned by another account with `409 ACCOUNT_CONTEXT_MISMATCH`; they never silently substitute or omit it. List endpoints use cursor pagination, stable ordering, filters, and an explicit `has_more` value.

### StrategyConfig and policy CRUD/versioning

- `GET|POST /broker-accounts/{id}/strategy-configs`
- `GET|PATCH|DELETE /broker-accounts/{id}/strategy-configs/{config_id}`; delete means disable/archive, never history deletion.
- `GET|POST /broker-accounts/{id}/strategy-configs/{config_id}/enrichment-policies`; POST creates a new immutable version.
- `POST /broker-accounts/{id}/strategy-configs/{config_id}/activate`
- `POST /broker-accounts/{id}/strategy-configs/{config_id}/disable`
- `POST /broker-accounts/{id}/strategy-configs/{config_id}/copy` with explicit `{ "target_broker_account_id": "...", "reason": "..." }`
- `GET /broker-accounts/{id}/strategy-configs/{config_id}/versions`
- `GET /broker-accounts/{id}/strategy-config-versions/{version_id}`

Activate is the only activation boundary: in one target-account transaction it validates every Pair mapping referenced by the immutable config version, connector capabilities, current RiskLimits, and required official-calendar coverage before selecting that version as active. `Pair.enabled` means administratively available and is changed only through the account Pair commands below; it never bypasses StrategyConfig activation checks. Copy creates the disconnected, disabled target-owned logical config and policy described in section 6.7.

### Calendar operations

- `GET /economic-events`
- `GET /event-calendar/health`
- `POST /manual-economic-event-overrides`
- `POST /manual-economic-event-overrides/{id}/revoke`

There is no OCR/screenshot endpoint.

### Explicit idempotent commands

- `POST /broker-accounts` creates a `DISABLED`/`STOPPED`/`MANUAL` account with live execution disabled.
- `POST /broker-accounts/{id}/connector-binding` creates or replaces the account-specific connector key binding while STOPPED; replacement does not bypass takeover quarantine/reconciliation.
- `POST /broker-accounts/{id}/enable`, `/disable`, `/archive`
- `POST /broker-accounts/{id}/live-execution/enable`, `/disable`
- `POST /broker-accounts/{id}/pairs/{pair_id}/enable`, `/disable`
- `POST /broker-accounts/{id}/risk-limits` creates a new immutable version; `/risk-limits/{version_id}/activate` selects it.
- `POST /broker-accounts/{id}/signals/{signal_id}/approve`, `/reject`
- `POST /broker-accounts/{id}/signals/{signal_id}/execute` (MANUAL only; requires APPROVED and creates exactly one entry Order)
- `POST /broker-accounts/{id}/orders/{order_id}/cancel`
- `POST /broker-accounts/{id}/positions/{position_id}/close`
- `POST /broker-accounts/{id}/bot/start`, `/stop`
- `POST /broker-accounts/{id}/bot/emergency-stop` with explicit `{ "close_all": false|true, "expected_version": n, "reason": "..." }`
- `POST /broker-accounts/{id}/bot/reset-to-stopped`
- `POST /broker-accounts/{id}/bot/execution-mode` with `{ "mode": "MANUAL|SEMI_AUTO|FULL_AUTO", "expected_version": n, "reason": "..." }`
- `POST /broker-accounts/{id}/reconciliation` starts/resumes authoritative account reconciliation.
- `POST /broker-accounts/{id}/dispatch-recovery` resolves a blocked dispatch sequence.
- `POST /broker-accounts/{id}/emergency-operations/{operation_id}/resume-reconcile`
- `POST /global-emergency` with explicit `{ "close_all": false|true, "reason": "..." }`
- `POST /global-emergency-operations/{operation_id}/resume-reconcile`

Every command requires `Idempotency-Key`, a non-empty `reason`, and `expected_version` for each mutable target except creation commands and global emergency acceptance. Unknown fields are rejected. The account path is authoritative: Signal, Order, Position, Pair, RiskLimits, StrategyConfig, and operation targets must derive to the same account or the backend returns `409 ACCOUNT_CONTEXT_MISMATCH` before creating a Command. The backend derives actor/principal from trusted authentication; caller-supplied actor fields are rejected. Only `/global-emergency` issues global control; shared calendar/news writes create targeted per-account policy projections, not a global state command.

Every endpoint validates strict payload, legal source state, account ownership, readiness/live gates, and expected version before creating a Command. The canonical response is `{command_id, status, resource_url, domain_result?}`. A newly accepted asynchronous command returns `202`; a completed first execution or same-key/same-hash replay returns `200` with the same Command/resource; in-progress or `UNKNOWN` replay returns `202` with the same poll URL. Same key with a different canonical hash returns `409 IDEMPOTENCY_KEY_REUSED`; illegal transition or account mismatch returns `409`; stale expected version returns `412`; expired Signal returns `422`. No accepted command returns success before its defined domain effect is durable. Approval transitions the Signal. In MANUAL, only `/execute` schedules; in SEMI_AUTO, successful approval schedules exactly once; in FULL_AUTO, ELIGIBLE schedules exactly once. All paths consume exactly once under a unique `(broker_account_id, signal_id, purpose=ENTRY)` Order constraint. Polling Command distinguishes acceptance from final broker outcome. Domain lifecycle entities do not expose generic write CRUD.

Recovery commands are legal while the target account is `QUARANTINED`, a broker result is `UNKNOWN`, a dispatch is blocked, or an emergency operation is `IN_PROGRESS | ATTENTION_REQUIRED`; global resume/reconcile is legal while any target is unmaterialized or unresolved. They require the current account/operation `expected_version` (the global command requires the parent version), never reopen exposure merely by acceptance, and journal-first inspect the connector record for the generation that owns each unresolved command—not merely the current generation. A narrowly scoped authenticated read-only journal lookup may name a prior generation; its response can supply reconciliation evidence but cannot acknowledge a dispatch fence, advance a sequence, renew a lease, or authorize a broker side effect. Re-delivery is allowed only when that generation's journal authoritatively proves the side-effecting broker call was never invoked; otherwise recovery queries broker truth and leaves ambiguity `UNKNOWN`/`ATTENTION_REQUIRED` rather than blindly resending. Each accepted recovery writes Command, AuditEvent, and OutboxEvent before external work. Completion requires broker/domain convergence, resolved ordered sequence and reservations, final snapshot/watermark, and acknowledged clear fence when safe.

Global emergency acceptance atomically persists the GlobalEmergencyOperation and every immutable target-membership row, then returns `202` with its operation URL; it does not claim that children materialized or completed. Same-key/same-hash returns that parent. The parent query returns explicit policy, aggregate status, and every target's qualifying facts, materialization state, and optional child status/link, including targets whose child does not yet exist and offline/`UNKNOWN`/`ATTENTION_REQUIRED` children.

Position close request includes target Position, `volume` or explicit `all`, `reduce_only=true`, expected Position version and expected broker volume. HEDGING requires the exact external ticket; NETTING rejects any volume that could reverse exposure. Protection modification includes requested SL/TP and expected confirmed protection version. Pending-order modification and cancellation use separate schemas and legal states.

## 15. WebSocket contract

### Dashboard stream

`GET /ws/v1/dashboard`

Authenticated through the dashboard boundary. Delivery is at least once.

Durable envelope:

```json
{
  "event_id": "uuidv7",
  "stream": "account",
  "stream_sequence": 18442,
  "type": "position.updated",
  "occurred_at": "2026-09-02T09:00:00Z",
  "broker_account_id": "uuidv7",
  "aggregate_id": "uuidv7",
  "aggregate_version": 7,
  "correlation_id": "uuidv7",
  "payload": {}
}
```

For a system event, `stream="system"` and `broker_account_id=null`. Account events always name exactly one BrokerAccount. `stream_sequence` is monotonic only within its named account stream or the system stream; consumers must not infer a total order across streams.

Required event families:

- `market.candle.closed`, `market.state.updated`
- `opportunity.created`
- `signal.created`, `signal.updated`, `signal.expired`, `signal.blocked`
- `risk.assessed`
- `order.updated`, `fill.recorded`, `position.updated`
- `bot.state.changed`
- `connector.status.changed`
- `calendar.health.changed`, `event.blackout.changed`
- `emergency.operation.updated` on an account stream, linked by `global_operation_id` when applicable
- `global_emergency.operation.updated`, `system.alert.raised` on the system stream

One physical dashboard connection multiplexes one durable cursor per available BrokerAccount plus one system cursor. The client hello sends `{ "account_cursors": {"<broker_account_id>": <last_acknowledged_sequence>, ...}, "system_cursor": <last_acknowledged_sequence> }`. Each cursor is validated independently: an unknown, unauthorized, archived, stale, or malformed account cursor emits a terminal error for only that named stream, while valid account and system streams continue. Server replays each valid stream independently, sends per-stream `replay.complete`, then live events. Client acknowledges `(stream, broker_account_id?, sequence)` and deduplicates event IDs. Replay is retained for at least 24 hours. Fair account-keyed bounded scheduling prevents one account from monopolizing delivery. An unavailable sequence, aggregate-version gap, or per-stream queue overflow produces `snapshot.required` only for that account stream or the system stream and discards/rebases only that stream's queued backlog. The whole socket closes only for authentication, transport, protocol framing, or connection-wide resource failure—not one account's invalid cursor, gap, or backlog.

`GET /broker-accounts/{id}/dashboard-snapshot` runs in one PostgreSQL REPEATABLE READ transaction and returns only that account's dashboard projections plus its account `stream_watermark`. Large results use an expiring signed snapshot token so every page remains bound to the same exported snapshot. The client resumes that account from `watermark + 1`, closing the snapshot/live race. `GET /dashboard-summary-snapshot` returns the all-account summary, critical alerts, global emergency status, an `account_watermarks` map for exactly the included accounts, and `system_watermark` from one consistent transaction. It is not a cross-stream total-order claim; each returned watermark closes the race only for its stream. Heartbeat, error, authentication-expiry, and bounded-backpressure/disconnect frames are normative.

The landing view opens the last selected account when it remains available (authorized and not archived); otherwise it falls back to the all-account summary. Critical alerts from every available account and GlobalEmergencyOperation status remain globally visible while any account detail is selected.

`market.quote.updated` is account-scoped, non-resumable telemetry on a separate `/ws/v1/quotes` stream with explicit `broker_account_id`, per-account session sequence, and gap notification. Quote subscriptions name allowed accounts; a quote is never reused or routed across accounts. Closed candles and all domain lifecycle events remain durable/resumable.

### MT5 connector stream

`GET /ws/v1/connector`

- Outbound connection initiated by Windows connector.
- One account-bound static API key per connector binding without scheduled rotation, separate from Caddy credentials.
- TLS required. The Windows secret is stored in secure credential storage; server stores only a strong salted hash plus key ID, account binding, created/disabled/revoked timestamps. Comparison is constant-time. Logs redact credentials. Manual replacement permits a short explicit overlap before revocation.
- Authenticated handshake binds exactly one active connector/terminal session to one BrokerAccount's full immutable identity, and one session can never represent multiple accounts. At least three sessions are acceptance-tested concurrently; there is no schema, configuration, or application cap.
- Session ownership uses a durable monotonic connector generation and expiring lease. Takeover is two-phase: its first transaction closes the exposure gate, records `pending_generation=current_generation+1`, and audits/quarantines the account while the old session remains the current generation solely to drain. A clean takeover asks that still-current old session to stop accepting work and return an authenticated drain acknowledgement proving no invocation remains; only then does one transaction expire the old lease and promote the pending generation, after which the new session may receive work. If no drain acknowledgement arrives, the old lease expires without promotion-side trust; takeover remains quarantined until the configured bounded broker-call ambiguity window also expires, every old-generation journal state is inspected through the read-only prior-generation recovery path and reconciled against authoritative MT5 orders/deals/positions, and a final account snapshot/reconciliation watermark is committed. The pending generation is promoted only after that convergence. Only then may an acknowledged clear fence reopen exposure. No old in-flight MT5 side effect may be ignored because a newer session is waiting.
- Heartbeat, monotonic per-session sequence, message ID, BrokerAccount ID and full identity, connector generation, command ID, execution epoch, and idempotency key are mandatory and covered by the authenticated session. Normal traffic must match the current generation; a pending-generation session may only complete its handshake/wait and receives no broker work before promotion. After promotion, stale generations are rejected except for the authenticated read-only journal lookup defined in section 14, which cannot mutate connector/backend state or authorize broker effects. Replayed message IDs/sequences and wrong-account external IDs are rejected and rate limited.
- Connector pushes only its bound account's quotes, closed M1 candles, account/order/deal/position snapshots, command acknowledgements, and errors; every message and external identifier is interpreted in that account scope.
- Backend pushes check/send/modify/cancel/close/reconcile commands.
- Gap detection forces replay/reconciliation before execution resumes.
- Connector payloads never supply a trusted actor identity.

## 16. Reliability and reconciliation

- Domain change + OutboxEvent commit atomically.
- Workers claim outbox rows with owner and expiring lease, bounded exponential backoff, attempt limits, and dead-letter alerts. They publish then mark published; a crash between those steps may redeliver the same stable event ID.
- Broker-side-effect dispatch rows never become an ordinary terminal dead letter. Under the account dispatch lock, exhaustion sets `exposure_gate=QUARANTINED`, allocates a SAFETY_FENCE, leaves Command/Order UNKNOWN or INTENT and RiskReservation active, blocks later account dispatch sequences, and raises a critical alert. Recovery is an explicit audited reconcile/replay operation: query connector journal first; re-deliver only when it proves MT5 was never invoked, otherwise reconcile broker truth. Aggregate ordering remains blocked until resolution; a clear fence is issued only after domain and broker state converge.
- Per-aggregate events publish in aggregate-version order. Consumers deduplicate by event/command/idempotency/external IDs and reject/buffer version gaps before snapshot recovery.
- Connector maintains bounded local replay for unsent market/account updates.
- Market gaps are marked; backend backfills closed candles after reconnect.
- Broker side-effect timeout opens reconciliation; it does not invoke generic retry. Connector journal and backend Command remain UNKNOWN until convergence.
- After shared infrastructure readiness, startup/recovery progresses independently per account: state load → connector handshake → reconciliation → account execution eligibility. One account's loss, gap, reconciliation failure, blocked dispatch, UNKNOWN result, protection quarantine, or overload cannot close another account's exposure gate or block its workers/stream.
- Fair account-keyed bounded scheduling and isolation prevent one account's overload or failures from starving healthy accounts; this is a behavioral requirement and does not require separate per-account infrastructure stacks, queues, budgets, or circuit breakers. Account dispatch ordering/locks remain as specified. Shared PostgreSQL/backend infrastructure failure may block all accounts; otherwise only an explicit global emergency intentionally fans out across accounts.
- Cross-account fan-out enumerates targets and performs separate account transactions. It never acquires or holds more than one account execution/risk/dispatch lock at once.
- API/news/calendar failures never stop monitoring or protective exits.

## 17. Security

- Dashboard: Caddy Basic Auth over HTTPS for the V0 single user.
- Connector: separate account-bound static API keys over WSS.
- Backend listens only on loopback/private interface reachable by Caddy; firewall blocks direct public bypass. Caddy overwrites trusted principal headers and strips client-supplied copies.
- Browser state-changing requests require exact allowed `Origin`/`Host` and a CSRF token in addition to Basic Auth. Dashboard WS validates Origin and closes on authentication loss/expiry.
- Secrets never appear in repository, payload logs, URLs, or AuditEvent.
- Request bodies and raw external payloads are size-limited.
- REST command inputs use strict schemas and reject unknown fields.
- Every state-changing command records actor, reason, idempotency key, and result.
- Emergency commands have explicit payload fields; provider defaults are not trusted.
- Credentials in URLs/query strings are rejected. Connector keys are account-scoped and server-side hash-only.

## 18. Migration impact

The repository is pre-implementation and documentation-only, so implementation starts from this contract as a clean baseline. No compatibility shim or live-data migration is required now.

Initial implementation schema work must:

- start without the obsolete one-active-account assumption or any corresponding uniqueness/check constraint; it must never be introduced;
- introduce immutable BrokerAccount `(provider, broker_server, external_account_id)` identity with its uniqueness constraint, lifecycle, durable live-execution flag, full-identity connector binding/generation/lease, and account readiness/reconciliation fields;
- add `broker_account_id` to every account-owned entity and broker-derived timeseries/projection missing it;
- make external IDs, logical config IDs, evaluation keys, idempotency scopes, dispatch sequences, and durable dashboard sequences unique in their required account scope;
- create independent account dispatch/reconciliation cursors and dashboard account streams plus the system stream;
- add GlobalEmergencyOperation, immutable unique target-membership rows, and their optional one-to-one relation to separately materialized per-account EmergencyOperation children;
- expose account REST paths, account snapshots, the global summary snapshot, cursor map, and system watermark defined above; and
- assert account-consistent foreign-key graphs and reject orphan or ambiguous records rather than assigning a default account.

## 19. Acceptance criteria

1. Duplicate candle, evaluation, event, command, and broker callback delivery does not duplicate Opportunity, Order, or Fill.
2. Same MarketStateSnapshot + StrategyConfig version yields the same Strategy result.
3. LONG/SHORT conflict emits no Opportunity and audits `CONFLICTING_SETUPS`.
4. Quote stream stays realtime without persisting every tick.
5. Higher-timeframe candle boundaries and device-local rendering do not alter Strategy inputs.
6. Signal approval after `expires_at` is rejected.
7. Every Order references a fresh approved PRE_ORDER RiskAssessment.
8. `order_send` timeout produces UNKNOWN and no blind retry; reconciliation finds accepted orders without duplication.
9. Partial fills create correct append-only Fill rows and Position volume.
10. Netting and hedging reconciliation fixtures both pass.
11. Connector loss after entry leaves confirmed MT5-native SL/TP active.
12. STOPPED/EMERGENCY_STOP block new exposure while monitoring/exit continues.
13. Emergency stop without `close_all` leaves positions open; with `close_all` creates idempotent close/cancel commands and waits for broker convergence.
14. Missing required official-calendar coverage prevents StrategyConfig activation for a referenced Pair.
15. Blackout closes the exposure gate and orders its fence before pending-entry cancellation while allowing close/reduce/SL/TP; completion waits for fence acknowledgement and broker convergence.
16. Dashboard reconnect deduplicates resumed events or reloads a consistent snapshot.
17. Permanent AuditEvent data explains why every Signal was accepted, blocked, rejected, ordered, or expired.
18. Raw broker/LLM payload retention removes eligible payloads after 90 days without removing permanent audit facts.
19. Connector crash immediately after MT5 accepts `order_send` leaves one journaled UNKNOWN command; restart reconciles it without a second send.
20. Same idempotency key plus same canonical hash returns the original Command; a different hash returns `409 IDEMPOTENCY_KEY_REUSED`.
21. Emergency stop racing a queued entry is ordered by the account dispatch stream: an entry INVOKING before the fence is reconciled, risk-accounted, quarantined, and capability-safely closed as policy requires or raised as `ATTENTION_REQUIRED`; one after the fence is rejected.
22. Emergency close-all converges when cancellation races an entry Fill and does not finish while a pending entry, open/UNKNOWN Position, active reservation, unresolved broker side effect, or emergency child remains.
23. Two concurrent eligible Signals cannot reserve more account/correlation risk than RiskLimits allow.
24. Failed `order_check` never calls `order_send` and releases its RiskReservation.
25. First Fill without confirmed native protection immediately quarantines new exposure; versioned timeout/attempt bounds force confirm, bounded repair, capability-safe reduce-only close, or alerted operator/broker-native protective exit, including reconnect-first handling.
26. Trailing-stop timeout cannot loosen protection or replay a stale modification.
27. Partial close, simultaneous Fill, netting reversal, hedging exact-ticket close, INOUT, and OUT_BY fixtures project exposure correctly without crossing zero accidentally.
28. Safety-relevant calendar revision creates an ordered fence: commands before its linearization are reconciled, commands after it obey the new blackout, and no new exposure enters while the fence is pending.
29. Mode changes are persisted, audited, fenced, and do not auto-execute pre-existing Signals.
30. REST snapshot watermark plus WS resume produces no event gap; quote telemetry explicitly reports non-resumable gaps.
31. Outbox worker crash after publish redelivers the same event ID; consumers deduplicate and detect aggregate-version gaps.
32. Direct backend bypass, forged actor headers, cross-origin state commands, replayed connector messages, and wrong-account connector keys are rejected.
33. Every calendar mutation class that can change safety state atomically installs a CALENDAR_FENCE and closes affected exposure gates under their account dispatch locks.
34. First Fill/unexpected protection change closes the connector-local gate before releasing its mutex and atomically persists QUARANTINED plus SAFETY_FENCE with backend projections.
35. Concurrent/out-of-order deal observations lock deterministically or serialize; duplicate external deal insertion aborts all projection deltas.
36. Connector restart fixtures cover every journal state: pre-side-effect states become durable ABORTED_NOT_INVOKED and release intent/reservation; DISPATCHING always becomes UNKNOWN and never resends.
37. At least three BrokerAccounts—covering a mixed `DEMO`/`LIVE` set—run connector sessions, pipelines, ordered dispatch, and reconciliation concurrently; three is the minimum verified capacity with no schema, configuration, or application hard cap.
38. Account A quotes, candles, Pair mappings, spread/baselines, MarketState, Signals, external IDs, and Orders cannot be observed, evaluated, or dispatched as account B data; REST and connector mismatch attempts fail explicitly.
39. Connector loss/gap, reconciliation failure, blocked/dead-letter dispatch, UNKNOWN broker result, protection quarantine, and induced overload on one account leave the other two healthy accounts executing and reconciling.
40. Concurrent reservations and fills on separate accounts use only local RiskLimits/locks and proceed independently; within each account they cannot exceed its limits, and no test or model references GlobalRiskLimits or a cross-account exposure/correlation cap.
41. Moderate defaults enforce `0.50%` risk/trade, `2%` daily loss including floating PnL from broker-day starting equity, `3` open positions, `2%` total open risk, `1%` currency/correlation, `2x` median spread, `0.15R` slippage, and `2.5x` median ATR volatility; insufficient baseline data fails closed.
42. Tightening RiskLimits leaves old positions open but consuming capacity, immediately closes the exposure gate and orders a fence before cancelling newly noncompliant pending entries, and completes only after acknowledgement/convergence; a racing Fill is accounted, quarantined, and safely closed or raised as `ATTENTION_REQUIRED`.
43. One shared calendar/news source revision fans out policy evaluation and CALENDAR_FENCE only to affected accounts; unrelated accounts receive neither changed safety state nor broker-derived context from affected accounts.
44. First setup is `DISABLED`/`STOPPED`. Startup keeps prior effectively RUNNING accounts visibly `RECOVERING` with exposure closed, resolves any durable pending STATE_FENCE first, then restores RUNNING and last Mode only after their own gates are healthy; effective STOPPED/EMERGENCY_STOP accounts never auto-resume.
45. LIVE unlock succeeds only while STOPPED and healthy, requires no account-age, demo-period, or completed-order probation, changes neither Mode nor BotState, and makes every Mode including FULL_AUTO available subject to ordinary gates.
46. Disabling live execution immediately closes the exposure gate, installs and acknowledges an ordered entry fence, then converges pending-entry cancellation without auto-closing pre-existing Positions; a racing Fill follows the risk-account/quarantine/safe-close-or-attention rule while monitoring, protection, close/reduce, reconciliation, and emergency processing continue.
47. Both values of `live_execution_enabled` persist across restart. An unacknowledged live fence recovers before exposure eligibility, and queued or stale entry work cannot cross it after reconnect.
48. Disabling an `ENABLED` BrokerAccount immediately closes its exposure gate, installs and acknowledges its ordered entry fence, then converges pending-entry cancellation without auto-closing pre-existing Positions; a racing Fill follows the risk-account/quarantine/safe-close-or-attention rule while monitoring, protection, close/reduce, reconciliation, and emergency processing remain active while `DISABLED`.
49. BrokerAccount enable fails until full-identity binding, reconciliation, RiskLimits, Pair mapping, and readiness are complete. Archive fails while any pending Order, open/UNKNOWN Position, UNKNOWN Command or broker effect, active RiskReservation, unresolved emergency child, or unresolved global target membership exists, even before child materialization; successful archive preserves history.
50. StrategyConfig copy creates a new target-owned logical config plus initial config/policy versions in DISABLED state with no synchronization; target activation independently validates mappings, calendar, connector capabilities, and RiskLimits.
51. Stop-only global emergency persists target memberships for exactly all `ENABLED` accounts at acceptance. Close-all includes those plus every exposed/unresolved non-`ARCHIVED` account, including `DISABLED` accounts with open/UNKNOWN Positions, pending entries, active reservations, unresolved broker effects/Commands, or unresolved emergency targets/children; later accounts are excluded.
52. Parent acceptance atomically persists all unique target memberships; crash-restarted idempotent fan-out creates exactly one child per target in separate account transactions. Queries show unmaterialized targets, and the parent cannot complete until every target has a converged child; offline/UNKNOWN/ATTENTION_REQUIRED or absent children remain unresolved without hot polling.
53. Dashboard hello resumes valid account/system cursors even when another cursor is unknown, unauthorized, archived, stale, or malformed; that cursor alone receives an error. Fair account-keyed bounded scheduling confines gap/overflow `snapshot.required` to one stream, and one account backlog never closes the socket or interrupts healthy streams.
54. Per-account snapshot contains only that account and one watermark. Global summary snapshot contains exactly its account watermark map plus system watermark; neither implies cross-stream ordering, and replay from each watermark closes its own snapshot/live race.
55. Last-selected available account opens on landing, fallback opens all-account summary, and critical alerts from other accounts plus global emergency status remain visible from account detail.
56. Global emergency parent events occur on the system stream; children occur on their account streams and carry `global_operation_id`; consumers do not depend on a total order across them.
57. Connector takeover is two-phase: the old session remains current while it drains and can acknowledge under its valid generation; only then is the pending generation promoted. Without acknowledgement, promotion waits for old-lease and ambiguity-window expiry, read-only inspection plus MT5 reconciliation of every old-generation journal state, and a final snapshot/watermark. The pending session receives no broker work before promotion, stale generations cannot mutate state afterward, and no old in-flight side effect is ignored while other accounts continue.
58. Duplicate `(provider, broker_server, external_account_id)` registration is rejected; credentials/session match the full immutable identity, and changing any identity component creates a new BrokerAccount rather than rebinding history.
59. NewsAnalysis stores only account-neutral canonical pair codes/currencies. Application through two accounts resolves each account's own Pair mapping and records exact account-owned context/impact references used by each Signal without cross-account Pair IDs.
60. BotState tests distinguish durable requested from effective state: API/UI show a pending STATE_FENCE transition without applying it, only the current-generation acknowledgement changes effective state, and restart resolves the fence before auto-resume.
61. Account reconciliation, blocked-dispatch recovery, per-account emergency resume/reconcile, and global emergency resume/reconcile are idempotent, expected-version guarded, legal in their documented quarantined/UNKNOWN/ATTENTION_REQUIRED states, journal-first, never blindly resend, and complete only after broker/domain/fence/watermark convergence.
62. Risk tightening, live disable, account disable, blackout, and emergency cancellation-versus-Fill fixtures prove the exposure gate/fence is installed before cancel dispatch, never claim cancellation when Fill wins, account the resulting risk, quarantine the account, and converge through capability-safe reduce-only close or an alerted `ATTENTION_REQUIRED` path.

## 20. Deferred decisions owned by later tickets

- Exact event blackout durations; EnrichmentPolicy must require explicit versioned values.
- Initial Strategy set and indicator parameters.
- Deployment/DNS/Caddy provisioning details.
