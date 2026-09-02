# Trading Engine V0 — Backend Specification

**Status:** Accepted
**Decision ticket:** [Spec: Backend V0 — data model, Strategy interface, pipeline, API/WS](https://github.com/igarukas4/trading-engine-dev/issues/5)  
**Domain language:** [`CONTEXT.md`](../../CONTEXT.md)

## 1. Objective

Build an implementation-ready FastAPI backend for a modular algorithmic trading engine that consumes live MT5 market data, evaluates deterministic strategy plugins, enriches Opportunities with news/context, applies a final deterministic risk gate, relays real-market orders to MT5, reconciles broker truth, and serves an Indonesian realtime dashboard.

V0 supports one active MT5 BrokerAccount but retains `broker_account_id` on all account-scoped records. The schema supports both MT5 netting and hedging accounts.

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
- Manual, semi-auto, and full-auto execution capability; detailed mode behavior is finalized in the bot-mode decision ticket.
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

## 4. Architecture

```text
Windows PC                                      Lighthouse VPS
┌────────────────────┐        outbound WSS      ┌─────────────────────────┐
│ MT5 Terminal       │◀────────────────────────▶│ Connector Gateway       │
│ MetaTrader5 Python │ quotes/M1/account/order  │ FastAPI                  │
│ Connector Agent    │ snapshots + commands     └──────────┬──────────────┘
└────────────────────┘                                      │
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
- Commands carry a globally unique `idempotency_key`.

## 6. Data model

### 6.1 BrokerAccount — PostgreSQL

Identity and capabilities of the connected MT5 account.

Required fields:

- `id`, `provider`, `external_account_id`, `display_name`
- `accounting_mode`: `NETTING | HEDGING`
- `currency`, `leverage`, `trade_allowed`
- `execution_mode`: `MANUAL | SEMI_AUTO | FULL_AUTO`
- `execution_epoch`, `dispatch_fence_status`, `exposure_gate`: `OPEN | FENCE_PENDING | QUARANTINED`
- exposure-gate reason/version, `connector_status`, `reconciliation_status`
- account operating policy: connector-exclusive order/protection ownership flags
- `last_heartbeat_at`, `last_reconciled_at`, `reconciliation_watermark`
- `created_at`, `updated_at`, `version`

Only one row may be active in V0. The relational model permits more later.

### 6.2 Pair — PostgreSQL

Canonical traded instrument and broker mapping.

Required fields:

- `id`, `canonical_code`
- `base_currency`, `quote_currency`
- `broker_account_id`, `broker_symbol`
- `digits`, `point_size`, `contract_size`
- `min_volume`, `max_volume`, `volume_step`
- `supported_order_types`, `enabled`
- `created_at`, `updated_at`

Pair routing uses metadata, never string parsing of an MT5 symbol with possible prefix/suffix.

### 6.3 Candle — TimescaleDB hypertable

Closed OHLCV candle.

Required fields:

- `pair_id`, `timeframe`, `open_time`, `close_time`
- `open`, `high`, `low`, `close`
- `tick_volume`, `real_volume`, `spread`
- `source`, `source_revision`, `is_closed`, `ingested_at`

Unique key: `(pair_id, timeframe, open_time, source_revision)` with a current-revision projection. Closed M1 is canonical input. Backend builds M5/M15/H1/H4. D1 follows the broker session boundary while timestamps remain UTC. Canonical closed candles are retained permanently and compressed; only derived caches and ingestion staging tables may use deletion-based retention policies.

### 6.4 IndicatorDefinition — PostgreSQL

Versioned indicator identity and parameter schema.

Fields: `id`, `key`, `implementation_version`, `parameter_schema`, `created_at`.

### 6.5 IndicatorValue — TimescaleDB hypertable

Computed numeric result for a Candle.

Fields:

- `pair_id`, `timeframe`, `candle_open_time`
- `indicator_definition_id`, `parameter_hash`
- `value_name`, `value_numeric`, `computed_at`

Unique key covers Pair, timeframe, candle, definition version, parameter hash, and value name.

### 6.6 MarketStateSnapshot — PostgreSQL

Immutable input record for one Strategy evaluation.

Fields:

- `id`, `pair_id`, `trigger_timeframe`, `trigger_time`
- ordered input sets for every required timeframe: exact Candle revision IDs, ascending open-time order, and configured lookback cardinality
- exact IndicatorValue IDs/definition versions/parameter hashes
- `market_regime`, `regime_model_version`
- `quote_observed_at`, `spread_at_evaluation`
- `completeness`: `COMPLETE | GAP_DETECTED`, plus missing-range reason codes
- `created_at`

`MarketState` is the immutable in-memory value reconstructed from this snapshot: Pair metadata; ordered closed bars for M5/M15/H1/H4/D1; exact indicator values; regime; and the observed quote/spread. Strategies never receive incomplete input: lookback shortage, a gap, an open candle, or revision mismatch skips evaluation and writes an AuditEvent. It references normalized records rather than storing an opaque market-state JSON blob.

### 6.7 StrategyConfig — PostgreSQL

Active parameterized Strategy instance.

Fields:

- `id` (immutable version ID), `logical_config_id`, `version_number`
- `strategy_key`, `strategy_version`, `name`
- `broker_account_id`, enabled Pair references
- `trigger_timeframe`, required timeframes
- versioned `parameters`
- `minimum_score`, `signal_ttl`
- `enrichment_policy_id`, `enabled`
- `effective_from`, `superseded_at`, `created_at`

Each update creates a new immutable row with a new `id` under stable `logical_config_id`; `(logical_config_id, version_number)` is unique. REST `{config_id}` always means the logical ID; responses identify both logical ID and immutable version ID. Multiple configs may use the same Strategy plugin.

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

A Signal records the exact policy version used. Activation of a Pair whose currencies lack required official-calendar coverage fails closed.

### 6.9 Opportunity — PostgreSQL

Single raw Strategy candidate.

Fields:

- `id`, `evaluation_key` (unique)
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

Google News RSS is primary headline input; Investing.com RSS supplements forex coverage.

### 6.11 NewsAnalysis — PostgreSQL

Immutable structured LLM output.

Fields:

- `id`, `news_event_id`, affected Pair references
- `directional_bias`, `sentiment`, `severity`, `confidence`
- `trade_impact`, `reason`, `expires_at`
- `model`, `prompt_version`, `schema_version`
- `raw_payload_ref`, `created_at`

Extractor uses a cheap structured-output model. Analyzer uses the next tier and escalates only low-confidence/conflicting bundles.

### 6.12 EconomicEventRevision — PostgreSQL

Immutable normalized revision from an official first-party calendar adapter.

Fields:

- `id`, `logical_event_id`, `revision`
- `provider`, `provider_event_id`, `source_url`
- `affected_currency`, `event_kind`, `impact`
- `scheduled_at`, `timing_precision`, `status`
- `observed_at`, `superseded_at`
- raw payload/hash

Only currencies used by active StrategyConfigs require adapters. Forex Factory is not ingested.

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

- `id`, `opportunity_id`, `pair_id`, `strategy_config_id`
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

MANUAL and SEMI_AUTO route otherwise eligible Signals to `PENDING_APPROVAL`; FULL_AUTO routes them to `ELIGIBLE`. Re-scoring creates a new immutable Signal revision, supersedes and invalidates the prior revision, and invalidates its approval.

### 6.15 RiskLimits — PostgreSQL

Versioned account-level safety configuration.

Fields:

- `id`, `broker_account_id`, `version`
- max risk/trade, daily loss, open positions, total exposure, correlation exposure
- spread/slippage/volatility guards
- `protection_confirmation_timeout`, `max_protection_repair_attempts`
- effective timestamps, author, reason

All mechanisms are mandatory and configurable. Numeric defaults are finalized in the bot-mode/execution ticket.

### 6.16 RiskAssessment — PostgreSQL

Immutable RiskEngine decision for a Signal and account snapshot.

Fields:

- `id`, `signal_id`, `risk_limits_id`
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

Canonical REST command receipt for all state-changing endpoints: `id`, authenticated principal, account, endpoint/type, target, idempotency key, canonical request hash, expected version, status, result/error, created/completed timestamps. Key scope is `(principal, broker_account_id, endpoint, idempotency_key)` and records are retained permanently with audit. Same key+hash returns the same resource; same key+different hash returns `409 IDEMPOTENCY_KEY_REUSED`; an ambiguous/in-progress broker result remains `202` with a poll URL.

### 6.23 ConnectorCommandJournal — durable connector-local store

The Windows connector fsyncs `RECEIVED` with `(broker_account_id, command_id)`, dispatch sequence, idempotency key, canonical request hash, execution epoch, and request before processing. One journal state machine applies everywhere:

```text
RECEIVED → INVOKING → CHECKING → CHECKED → DISPATCHING → RESULT_CONFIRMED
              │          │         │              └──────→ UNKNOWN
              └──────────┴─────────┴──restart──→ ABORTED_NOT_INVOKED
                         └──failed──────────────→ REJECTED
```

`INVOKING` orders the item against fences but does not mean a side-effecting MT5 call occurred. `DISPATCHING` is fsynced immediately before the side-effecting MT5 call and is the ambiguity boundary. Restart behavior is normative: RECEIVED re-enters ordered processing; INVOKING/CHECKING/CHECKED fsync `ABORTED_NOT_INVOKED`, store the outcome, advance the ordered sequence, idempotently reject/cancel the backend intent, and release its reservation without sending; DISPATCHING becomes UNKNOWN and reconciles, never resends; terminal states return stored results. Non-order side effects omit CHECKING/CHECKED but use the same RECEIVED → INVOKING → DISPATCHING boundary. Both command ID and idempotency key are account-scoped unique. Duplicate same-hash commands return stored state/result; different hashes are rejected. A stable correlation token is encoded in MT5 `magic` and/or `comment` where broker capability permits.

### 6.24 BotState — PostgreSQL projection

Per-BrokerAccount state: `RUNNING | STOPPED | EMERGENCY_STOP`, reason, trusted actor, changed timestamp, version and execution epoch. Redis mirrors the latest value for display/fast rejection only; dispatch fences always read PostgreSQL.

### 6.25 EmergencyOperation — PostgreSQL

Durable close-all convergence operation with one child per pending entry Order and open Position. It repeatedly reconciles cancellation/close outcomes, adds a child if an entry fills while cancellation is in flight, and reaches `COMPLETED` only when MT5 confirms no pending entries and—when requested—no open positions. Ambiguous children remain UNKNOWN and keep the account quarantined with an alert.

### 6.26 AccountDispatchItem — PostgreSQL and connector journal

One monotonically sequenced stream per BrokerAccount contains every broker side effect plus `STATE_FENCE`, `CALENDAR_FENCE`, and generic `SAFETY_FENCE`. The backend allocates sequence numbers while holding the account dispatch lock. Connector processes exactly in sequence under one account mutex and never starts a second invocation concurrently. It verifies the item against its last applied fence and exposure gate, then fsyncs `INVOKING`; that journal write is the ordering linearization point. A fence linearizes when connector fsyncs/applies it and acknowledges its sequence; BrokerAccount `dispatch_fence_status` remains `FENCE_PENDING` until then without changing the BotState enum. Earlier commands may linearize before a later fence and are reconciled/closed as required; later or stale-epoch commands are rejected. Missing sequence causes quarantine and replay, not out-of-order execution.

### 6.27 AuditEvent — PostgreSQL append-only

Permanent record of strategy evaluations, conflicts, enrichment, risk decisions, commands, broker lifecycle, reconciliation, state transitions, and manual overrides.

Fields: `id`, account/entity correlation IDs, event type, actor, reason codes, policy/config versions, timestamps, payload reference.

### 6.28 OutboxEvent — PostgreSQL

Transactional delivery record: stable `id`, monotonic stream sequence, aggregate/type/version, payload reference, idempotency key, created/available timestamps, lease owner/expiry, published timestamp, attempts, next retry, dead-letter timestamp, and last error. Workers process at least once; consumers are idempotent.

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

The MT5 implementation is a remote adapter over outbound WSS. Every method that can change broker state receives `command_id`, idempotency key, canonical request hash, execution epoch, and expected target/protection version. The connector owns MT5 translation/IPC, its durable command journal, and last-moment fence/reduce-only enforcement; domain, risk, and strategy logic remain on the backend.

## 9. Pipeline — 14 observable steps

1. **Subscribe:** connector pushes quote updates and closed M1 candles.
2. **Build candles:** backend validates M1 and closes higher timeframes.
3. **Update indicators:** compute/persist IndicatorValue and update ephemeral state.
4. **Detect events:** candle trigger, calendar blackout, news arrival, reconnect, or command.
5. **Evaluate strategies:** eligible StrategyConfigs evaluate on their declared closed trigger timeframe.
6. **Generate opportunities:** persist at most one Opportunity per evaluation key.
7. **Enrich:** assemble versioned MarketContext from news/calendar/fundamental inputs.
8. **AI validate:** structured extraction/reasoning when policy enables it; never per tick.
9. **Final score:** DecisionEngine applies the exact EnrichmentPolicy version.
10. **Risk engine:** create initial RiskAssessment and mark blocked/degraded/eligible state.
11. **Execute:** mode/approval gate, expiry check, serialize by BrokerAccount, create fresh pre-order RiskAssessment + RiskReservation + Order intent/outbox atomically, revalidate fence/context, `order_check`, then journaled `order_send`.
12. **Monitor:** reconcile Order/Fill/Position/protection and manage versioned monotonic trailing modifications.
13. **Exit:** MT5-native SL/TP, manual close, strategy exit, or explicit emergency close-all.
14. **Log:** every transition writes AuditEvent and emits an outbox event.

The pipeline is event-driven. Quotes update ephemeral realtime state; they do not run all strategies/LLMs. News may update MarketContext and re-score active candidates but does not create a technical Opportunity.

## 10. Enrichment and event-risk behavior

- Each StrategyConfig owns a versioned EnrichmentPolicy.
- Every source is `REQUIRED`, `ADVISORY`, or `DISABLED` with explicit freshness and fallback behavior.
- Missing data is never silently treated as zero and weights are never silently renormalized.
- Official first-party calendar adapters cover currencies used by active StrategyConfigs.
- Pair activation fails closed when policy-required calendar coverage is absent.
- ManualEconomicEventOverride adds/extends safety windows with an official source URL and audit.
- Blackout numbers have no V0 hardcoded default in this spec; implementation must require explicit versioned configuration.
- During blackout, block opening/increasing/reversing exposure and cancel pending entry orders idempotently. Permit close, reduce-only, SL/TP, and emergency actions.
- Existing positions are not automatically liquidated before an event.
- Forex Factory is visual/manual reference only and is never automatically ingested.
- Freshness uses provider `observed_at` compared with server UTC; excessive adapter lateness or clock skew marks coverage unhealthy. Required stale/unhealthy coverage fails closed.
- PRE_ORDER references current event revisions and current calendar health. Safety-relevant calendar mutation means: any official event create/revision/reschedule/cancel, ManualEconomicEventOverride create/extend/revoke/expire, required-adapter health/freshness transition, or clock-skew/coverage transition that can change a blackout or fail-closed result. Under each affected account's dispatch lock, one PostgreSQL transaction persists the mutation, increments its safety version, allocates the CALENDAR_FENCE sequence, sets `dispatch_fence_status=FENCE_PENDING` and `exposure_gate=FENCE_PENDING`, and emits outbox/audit rows. New exposure is rejected immediately. Commands linearized before the barrier are ordered before its effective trading fence and reconciled; items after it see the new blackout. Fence acknowledgement opens the exposure gate only if no blackout/unhealthy/quarantine condition remains. No historical approval overrides current safety policy.
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

RiskEngine evaluates at least:

- max risk per trade;
- daily realized + configured floating loss policy;
- max open positions;
- total exposure;
- correlation/currency exposure;
- spread, slippage, and volatility guard;
- event-risk blackout/calendar health;
- BotState and emergency stop;
- Signal expiry;
- broker/account trade capability.

RiskLimits numbers are configurable and versioned. Exact defaults are finalized in the bot-mode/execution decision ticket. Exposure-increasing assessment and reservation are serialized per BrokerAccount. A rejected assessment emits reason codes and never creates an executable broker command. A PRE_ORDER assessment is usable only until `valid_until` and only while every referenced snapshot, event revision, BotState version, and execution epoch still matches.

## 13. Bot state machine

```text
STOPPED ──start──▶ RUNNING ──stop──▶ STOPPED
   │                   │
   └──── emergency ◀───┴──▶ EMERGENCY_STOP
                              │
                         reset-to-stopped
```

- State is persisted per BrokerAccount and mirrored in Redis.
- `STOPPED`: no new entry/order preparation; monitor and exits continue.
- `EMERGENCY_STOP`: no new exposure; monitor and exits continue.
- Every state transition increments `execution_epoch`, allocates a sequenced STATE_FENCE, and sets BrokerAccount `dispatch_fence_status=FENCE_PENDING` in one PostgreSQL transaction. New exposure is rejected while the fence is pending. The requested BotState becomes effective after connector applies/acknowledges the fence. A command whose INVOKING point precedes the barrier is ordered before the transition; later/stale commands are rejected. EmergencyOperation reconciles any earlier command that fills.
- `close_all=false` is the default explicit payload value for emergency command.
- `close_all=true` starts an EmergencyOperation that converges by reconciling audited idempotent close children for open positions and cancellation children for pending entries, including an entry that fills while cancellation is in flight.
- Restart reads persisted state; it never assumes RUNNING from process startup.

Execution Mode is separate from BotState and persisted on BrokerAccount:

- `MANUAL`: pipeline creates Signals in PENDING_APPROVAL; approval alone does not trade. A separate explicit execute command creates exactly one Order intent.
- `SEMI_AUTO`: eligible Signals enter PENDING_APPROVAL; approval schedules exactly one Order intent under the Signal uniqueness constraint.
- `FULL_AUTO`: eligible Signals may schedule exactly one Order intent automatically.
- Mode changes are explicit audited commands, require expected account version, increment `execution_epoch`, and never auto-execute pre-existing Signals. Detailed user-facing mode UX remains owned by the later mode ticket.

## 14. REST contract

Base path: `/api/v1`. Caddy Basic Auth protects the single-user dashboard surface.

### Read/query endpoints

- `GET /health/live`, `GET /health/ready`
- `GET /broker-accounts`, `GET /broker-accounts/{id}`
- `GET /pairs`, `GET /pairs/{id}`
- `POST /pairs/{id}/enable`, `POST /pairs/{id}/disable`
- `GET /candles?pair_id=&timeframe=&from=&to=`
- `GET /market-state?pair_id=`
- `GET /opportunities`, `GET /opportunities/{id}`
- `GET /signals`, `GET /signals/{id}`
- `GET /risk-assessments?signal_id=`
- `GET /orders`, `GET /orders/{id}`
- `GET /fills?order_id=&position_id=`
- `GET /positions`, `GET /positions/{id}`
- `GET /bot-state?broker_account_id=`
- `GET /audit-events`
- `GET /system/status`
- `GET /commands/{id}`
- `GET /dashboard-snapshot`

List endpoints use cursor pagination, stable ordering, filters, and an explicit `has_more` value.

### StrategyConfig and policy CRUD/versioning

- `GET /strategy-configs`
- `POST /strategy-configs`
- `GET /strategy-configs/{id}`
- `PATCH /strategy-configs/{id}`
- `DELETE /strategy-configs/{id}` means disable/archive, not history deletion.
- `GET /strategy-configs/{id}/enrichment-policies`
- `POST /strategy-configs/{id}/enrichment-policies` creates a new immutable version.
- `POST /strategy-configs/{id}/enable`
- `POST /strategy-configs/{id}/disable`
- `GET /strategy-configs/{config_id}/versions`
- `GET /strategy-config-versions/{version_id}`

Enable is the only activation boundary: in one transaction it validates every Pair referenced by the immutable config version, broker capabilities, and required official-calendar coverage before selecting that version as active. `Pair.enabled` means administratively available and is changed only through `POST /pairs/{id}/enable|disable`; it never bypasses StrategyConfig activation checks.

### Calendar operations

- `GET /economic-events`
- `GET /event-calendar/health`
- `POST /manual-economic-event-overrides`
- `POST /manual-economic-event-overrides/{id}/revoke`

There is no OCR/screenshot endpoint.

### Explicit idempotent commands

- `POST /signals/{id}/approve`
- `POST /signals/{id}/reject`
- `POST /signals/{id}/execute` (MANUAL only; requires APPROVED, expected version, and creates exactly one entry Order)
- `POST /orders/{id}/cancel`
- `POST /positions/{id}/close`
- `POST /bot/start`
- `POST /bot/stop`
- `POST /bot/emergency-stop` with explicit `{ "close_all": false|true }`
- `POST /bot/reset-to-stopped`
- `POST /bot/execution-mode` with `{ "mode": "MANUAL|SEMI_AUTO|FULL_AUTO", "expected_version": n, "reason": "..." }`

Commands require `Idempotency-Key`, `reason`, expected resource version, and an explicit BrokerAccount ID when the account is not unambiguously derived from the target. The backend derives actor/principal from trusted authentication; caller-supplied actor fields are rejected. A target-derived account must equal any explicit account or the request fails.

Every endpoint validates a strict request schema, legal source state, and expected version before creating a Command. The response is `{command_id, status, resource_url, domain_result?}`: `202` for accepted/in-progress/UNKNOWN, `200` for a completed duplicate or synchronous state command, `409` for illegal transition/idempotency payload mismatch, `412` for stale expected version, and `422` for an expired Signal. Approval transitions the Signal. In MANUAL, only `/execute` schedules; in SEMI_AUTO, the successful approval schedules; in FULL_AUTO, ELIGIBLE schedules. All paths consume exactly once under a unique `(broker_account_id, signal_id, purpose=ENTRY)` Order constraint. Polling Command distinguishes acceptance from final broker outcome. Domain lifecycle entities do not expose generic write CRUD.

Position close request includes target Position, `volume` or explicit `all`, `reduce_only=true`, expected Position version and expected broker volume. HEDGING requires the exact external ticket; NETTING rejects any volume that could reverse exposure. Protection modification includes requested SL/TP and expected confirmed protection version. Pending-order modification and cancellation use separate schemas and legal states.

## 15. WebSocket contract

### Dashboard stream

`GET /ws/v1/dashboard`

Authenticated through the dashboard boundary. Delivery is at least once.

Durable envelope:

```json
{
  "event_id": "uuidv7",
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

Required event families:

- `market.candle.closed`, `market.state.updated`
- `opportunity.created`
- `signal.created`, `signal.updated`, `signal.expired`, `signal.blocked`
- `risk.assessed`
- `order.updated`, `fill.recorded`, `position.updated`
- `bot.state.changed`
- `connector.status.changed`
- `calendar.health.changed`, `event.blackout.changed`
- `system.alert.raised`

Durable domain events receive a monotonic server `stream_sequence` when inserted into the outbox. On connect the client sends `hello` with its last acknowledged sequence; server sends replay, `replay.complete`, then live events. Client acknowledges sequences and deduplicates event IDs. Replay is retained for at least 24 hours. If unavailable, server sends `snapshot.required`. `GET /dashboard-snapshot` runs in one PostgreSQL REPEATABLE READ transaction and returns all dashboard projections plus the transaction's `stream_watermark`; large result sets use an expiring signed snapshot token so every page remains bound to the same exported snapshot. Client subscribes from `watermark + 1`, closing the snapshot/live race. Per-aggregate versions must be monotonic; a gap is buffered briefly and then forces snapshot recovery rather than silent advance. Heartbeat, error, authentication-expiry, and bounded-backpressure/disconnect frames are normative.

`market.quote.updated` is non-resumable telemetry on a separate `/ws/v1/quotes` stream with its own session sequence and explicit gap notification. Closed candles and all domain lifecycle events remain durable/resumable.

### MT5 connector stream

`GET /ws/v1/connector`

- Outbound connection initiated by Windows connector.
- One static API key without scheduled rotation, separate from Caddy credentials.
- TLS required. The Windows secret is stored in secure credential storage; server stores only a strong salted hash plus key ID, account binding, created/disabled/revoked timestamps. Comparison is constant-time. Logs redact credentials. Manual replacement permits a short explicit overlap before revocation.
- Authenticated handshake binds exactly one connector session to one BrokerAccount. A newer valid session takes over only via an audited policy and closes the old session.
- Heartbeat, monotonic per-session sequence, message ID, command ID, execution epoch, and idempotency key are mandatory and covered by the authenticated session. Replayed message IDs/sequences are rejected and rate limited.
- Connector pushes quotes, closed M1 candles, account/order/deal/position snapshots, command acknowledgements, and errors.
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
- Startup order: migrations → database readiness → Redis readiness → state load → connector handshake → reconciliation → execution eligibility.
- API/news/calendar failures never stop monitoring or protective exits.

## 17. Security

- Dashboard: Caddy Basic Auth over HTTPS for the V0 single user.
- Connector: separate static API key over WSS.
- Backend listens only on loopback/private interface reachable by Caddy; firewall blocks direct public bypass. Caddy overwrites trusted principal headers and strips client-supplied copies.
- Browser state-changing requests require exact allowed `Origin`/`Host` and a CSRF token in addition to Basic Auth. Dashboard WS validates Origin and closes on authentication loss/expiry.
- Secrets never appear in repository, payload logs, URLs, or AuditEvent.
- Request bodies and raw external payloads are size-limited.
- REST command inputs use strict schemas and reject unknown fields.
- Every state-changing command records actor, reason, idempotency key, and result.
- Emergency commands have explicit payload fields; provider defaults are not trusted.
- Credentials in URLs/query strings are rejected. Connector keys are account-scoped and server-side hash-only.

## 18. Acceptance criteria

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
13. Emergency stop without `close_all` leaves positions open; with `close_all` creates idempotent close/cancel commands.
14. Missing required official-calendar coverage prevents activation of affected StrategyConfig/Pair.
15. Blackout blocks new/increased exposure and pending entry while allowing close/reduce/SL/TP.
16. Dashboard reconnect deduplicates resumed events or reloads a consistent snapshot.
17. Permanent AuditEvent data explains why every Signal was accepted, blocked, rejected, ordered, or expired.
18. Raw broker/LLM payload retention removes eligible payloads after 90 days without removing permanent audit facts.
19. Connector crash immediately after MT5 accepts `order_send` leaves one journaled UNKNOWN command; restart reconciles it without a second send.
20. Same idempotency key plus same canonical hash returns the original Command; a different hash returns `409 IDEMPOTENCY_KEY_REUSED`.
21. Emergency stop racing a queued entry is ordered by the account dispatch stream: an entry INVOKING before the fence is reconciled/closed as required; one after the fence is rejected.
22. Emergency close-all converges when cancellation races an entry Fill and does not finish while a pending entry/open Position remains.
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

## 19. Deferred decisions owned by later tickets

- Exact numeric RiskLimits defaults.
- Detailed MANUAL/SEMI_AUTO/FULL_AUTO dashboard UX and numeric execution defaults; backend mode semantics are fixed above.
- Demo versus live MT5 account rollout.
- Exact event blackout durations; EnrichmentPolicy must require explicit versioned values.
- Initial Strategy set and indicator parameters.
- Deployment/DNS/Caddy provisioning details.
