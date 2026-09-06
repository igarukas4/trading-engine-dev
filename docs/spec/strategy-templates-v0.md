# Trading Engine V0 — Initial Strategy Templates

**Status:** Accepted
**Decision ticket:** [Specify initial V0 strategies and exact indicator parameters](https://github.com/igarukas4/trading-engine-dev/issues/12)
**Backend contract:** [`backend-v0.md`](backend-v0.md)
**Domain language:** [`CONTEXT.md`](../../CONTEXT.md)

## 1. Purpose and evidence status

This specification defines the exact initial V0 Strategy plugins, StrategyConfig seeds, indicator definitions, entry and exit rules, minimum lookbacks, Signal TTLs, and deterministic acceptance fixtures.

The values are **editable V0 seeds**, selected from the completed research and design analysis. They are not described as optimal, validated against a target broker's history, or evidence of profitability. A later Wayfinder effort may recalibrate them after the engine is working and stable.

Research context:

- [Riset: Kandidat strategi utama XAUUSD untuk V0](https://github.com/igarukas4/trading-engine-dev/issues/13)
- [Riset: Kandidat strategi utama EURUSD untuk V0](https://github.com/igarukas4/trading-engine-dev/issues/14)
- [Riset: Kandidat strategi utama USDJPY untuk V0](https://github.com/igarukas4/trading-engine-dev/issues/15)
- [Riset: Pilih instrumen dan strategi utama OIL untuk V0](https://github.com/igarukas4/trading-engine-dev/issues/16)

Broker-history calibration is deferred until the application is operating smoothly. V0 users may evaluate the templates on a demo BrokerAccount and create a new immutable StrategyConfig version when changing a seed.

## 2. V0 template set

V0 ships two Strategy plugins and four disabled StrategyConfig templates:

| Template key | Pair | Strategy plugin | Trigger timeframe | Required timeframes | Default state |
|---|---|---|---|---|---|
| `xauusd-trend-pullback-v0` | `XAUUSD` | `TrendPullbackContinuationStrategy@0.1.0` | M15 | H4, H1, M15 | disabled |
| `eurusd-trend-pullback-v0` | `EURUSD` | `TrendPullbackContinuationStrategy@0.1.0` | M15 | H4, H1, M15 | disabled |
| `usdjpy-trend-pullback-v0` | `USDJPY` | `TrendPullbackContinuationStrategy@0.1.0` | M15 | H4, H1, M15 | disabled |
| `wti-trend-breakout-v0` | `WTI` | `TrendFilteredBreakoutStrategy@0.1.0` | M15 | H4, H1, M15 | disabled |

`MeanReversionStrategy` is not an initial V0 template. It remains a future challenger for an explicitly ranging regime.

Each template is materialized separately for each BrokerAccount. It never shares mutable state, evaluation keys, enabled state, Pair-to-Symbol mapping, session selection, or execution lifecycle with another account.

### 2.1 WTI identity

`WTI` is the canonical Pair. Broker symbols such as `USOIL`, `XTIUSD`, `WTI`, or `OIL` are account-scoped mappings, not domain identities.

WTI activation fails when the selected Symbol:

- cannot be identified as WTI/US crude;
- is not currently tradeable;
- lacks a valid volume minimum/step;
- lacks native StopLoss and TakeProfit support; or
- has unknown fixed-expiry/continuous-roll semantics that prevent the engine from determining whether a roll guard is active.

This is a minimum technical-safety check, not historical qualification.

## 3. Shared deterministic conventions

### 3.1 Evaluation clock and data alignment

A Strategy evaluates exactly once for each newly closed canonical M15 candle.

At trigger close time `t`:

1. Select the M15 candle whose `close_time = t`.
2. Select the latest closed H1 and H4 candles with `close_time <= t`.
3. Read only candles and IndicatorValues referenced by one COMPLETE MarketStateSnapshot.
4. DataEngine/FeatureEngine skip Strategy evaluation and audit a reason code when any required candle is open, missing, gapped, from a mismatched revision, or outside the same Pair and BrokerAccount scope. Strategy never receives an incomplete MarketState.
5. Strategy returns `None` only when a complete MarketState fails its technical rules.
6. Never use an intrabar touch as an Opportunity trigger.

LONG and SHORT rules are symmetric. A strict comparison is used unless this specification explicitly says inclusive. If both directions pass, return `None` and audit `CONFLICTING_SETUPS`.

The evaluation key follows Backend V0 exactly and consists only of `strategy_config_version_id + pair_id + trigger_time`. Fixtures represent those components as a structured object; persistence serialization remains owned by Backend V0.

### 3.2 Numeric definitions

All persisted parameters and calculations use Decimal semantics.

- EMA input: closed-candle `close`; initialize with the arithmetic mean of the first `period` closes, then apply `EMA_t = alpha × close_t + (1 - alpha) × EMA_(t-1)` with `alpha = 2 / (period + 1)`.
- ATR: a predecessor closed candle is required before the first observation. True Range is the maximum of `high-low`, `abs(high-previous_close)`, and `abs(low-previous_close)`. Initialize ATR with the arithmetic mean of the first `period` True Ranges after that predecessor, then apply `ATR_t = ((period-1) × ATR_(t-1) + TR_t) / period`.
- RSI: a predecessor closed candle is required before the first delta. Initialize average gain/loss with the arithmetic mean of the first `period` close deltas, then apply Wilder recurrence. If average loss is zero and gain is positive, RSI is 100; if both are zero, RSI is 50.
- ADX: for each observation after a required predecessor, let `up_move = high_t - high_(t-1)` and `down_move = low_(t-1) - low_t`; `+DM = up_move` only when `up_move > down_move` and `up_move > 0`, otherwise zero; `-DM = down_move` only when `down_move > up_move` and `down_move > 0`, otherwise zero. A tie makes both zero. Compute True Range from that same predecessor. Initialize Wilder-smoothed TR/+DM/-DM as sums of the first `period` such observations, then recur as `previous - previous/period + current`. If smoothed TR is zero, both DI values and DX are zero. Otherwise derive DI and set DX to zero when the DI denominator is zero. Initialize ADX as the arithmetic mean of the first `period` defined DX values, then apply `ADX_t = ((period-1) × ADX_(t-1) + DX_t) / period`.
- Candle body fraction: `abs(close - open) / (high - low)`; a zero-range candle has body fraction zero.
- Directional body: LONG requires `close > open`; SHORT requires `close < open`.
- Spread buffer: spread observed at evaluation, expressed in Pair price units.
- IndicatorDefinition `v1` stores the algorithm identity, implementation version, parameter schema, input semantics, initialization, and smoothing rules. Concrete periods live in StrategyConfig parameters; each IndicatorValue stores the canonical parameter hash required by Backend V0.
- Canonical parameter hashing uses UTF-8 RFC 8785 JSON of `{indicator_key, implementation_version, parameters}` and SHA-256.
- Indicator calculations use Decimal precision 38 with `ROUND_HALF_EVEN`, no intermediate quantization, and persist the final value at scale 18 using `ROUND_HALF_EVEN`.

Indicator warm-up uses the declared closed-bar minimum in section 8. Values computed from a shorter series are invalid rather than partially initialized.

### 3.3 Session selection

The UI requires the user to select an entry session policy before activation. If that choice differs from the selected immutable StrategyConfig version, the UI first creates a new immutable version through StrategyConfig update, then sends a separate activation command selecting that exact version. Activation itself never mutates or creates a StrategyConfig version.

The selected StrategyConfig version must contain one of:

- `ALL_BROKER_OPEN`;
- `ASIA`;
- `LONDON`;
- `NEW_YORK`;
- `LONDON_NEW_YORK_OVERLAP`; or
- `CUSTOM`, with one or more IANA-timezone windows.

The selection is stored before activation in the immutable StrategyConfig version. Preset definitions are versioned and DST-aware. Existing positions remain monitored outside their entry session. Session choice does not bypass event-risk, spread, volatility, quote-freshness, State, or RiskEngine gates.

WTI additionally blocks new entries during a known roll guard and for 30 minutes after the broker's daily reopen. These WTI Pair safety windows apply even when `ALL_BROKER_OPEN` is selected. The account scheduler owns them through Backend V0 `PairEntryGateSnapshot`; a blocked gate skips Strategy evaluation rather than becoming input to the pure Strategy.

### 3.4 Order entry

A valid Opportunity is enriched into a Signal. When mode/approval and final risk gates permit execution:

- create a Market Order at the first fresh quote available after the M15 trigger close;
- recompute entry-relative SL/targets and perform PRE_ORDER RiskAssessment;
- reject an expired Signal rather than refreshing or chasing it;
- use the Backend V0 account dispatch, idempotency, fencing, order-check, reconciliation, and native-protection contracts.

Pending Stop entry is not part of these V0 templates.

## 4. TrendPullbackContinuationStrategy

### 4.1 H4 directional filter

The common seed for XAUUSD, EURUSD, and USDJPY is:

```yaml
ema_fast:
  period: 34
  price: close
ema_slow:
  period: 150
  price: close
atr_h4:
  period: 14
adx_h4:
  period: 14
adx_min: 22
ema_separation_atr: 0.10
slope_bars: 5
```

LONG requires all conditions on the latest closed H4 candle:

1. `close > EMA150`.
2. `EMA34 > EMA150`.
3. `(EMA34 - EMA150) >= 0.10 × ATR14`.
4. `EMA150[t] > EMA150[t - 5]`.
5. `ADX14 >= 22`.

SHORT inverts comparisons 1–4 and uses the same ADX threshold.

Failure reason codes are `TREND_PRICE_FILTER_FAILED`, `EMA_ORDER_FAILED`, `EMA_SEPARATION_FAILED`, `EMA_SLOPE_FAILED`, and `ADX_FILTER_FAILED`.

### 4.2 H1 pullback and reclaim

Common seed:

```yaml
ema_pullback:
  period: 34
  price: close
atr_h1:
  period: 14
rsi_h1:
  period: 14
pullback_window_bars: 2
pullback_tolerance_atr: 0.50
reclaim_level_rsi: 50
min_body_fraction: 0.55
```

For LONG:

1. In either of the last two closed H1 candles, `low <= EMA34 + 0.50 × ATR14`.
2. Neither of those two candles closes below `EMA34 - 0.50 × ATR14`.
3. The latest H1 candle closes above EMA34, is bullish, and has body fraction `>= 0.55`.
4. RSI14 reclaims 50: latest `RSI >= 50` and preceding closed H1 `RSI < 50`.

SHORT is symmetric: a high reaches `EMA34 - 0.50 × ATR14`, neither close exceeds `EMA34 + 0.50 × ATR14`, the latest candle closes below EMA34 with a bearish body, and RSI crosses from above 50 to `<= 50`.

The setup becomes armed at the reclaim H1 candle's `close_time`. Freeze that candle's EMA34 and ATR14; LONG invalidation is `frozen_ema34 - 0.50 × frozen_atr14`, and SHORT invalidation is `frozen_ema34 + 0.50 × frozen_atr14`. A later H1 close `<=` the frozen LONG boundary or `>=` the frozen SHORT boundary invalidates the setup. The setup remains eligible until, but excluding, the close time of the second subsequent H1 candle; equality is expired. A reversal of the H4 filter invalidates it earlier.

### 4.3 M15 continuation trigger

Common mechanics:

1. The breakout channel excludes the trigger candle and contains the configured number of immediately preceding closed M15 candles.
2. LONG requires trigger close above channel high plus buffer; SHORT requires trigger close below channel low minus buffer.
3. The trigger candle must be directional and have body fraction `>= 0.55`.
4. Extension from the unbuffered channel boundary must be `<= max_extension_atr × ATR14(M15)`.
5. The first passing closed candle creates at most one Opportunity for its evaluation key.

Pair-specific exact seeds:

| Parameter | XAUUSD | EURUSD | USDJPY |
|---|---:|---:|---:|
| `channel_bars` | 12 | 12 | 12 |
| `atr_m15_period` | 14 | 14 | 14 |
| `buffer_atr` | 0.10 | 0.10 | 0.05 |
| `buffer_spread_multiple` | 1.50 | 1.50 | 1.50 |
| `min_body_fraction` | 0.55 | 0.55 | 0.55 |
| `max_extension_atr` | 1.00 | 1.00 | 1.00 |
| `signal_ttl_m15_bars` | 4 | 2 | 4 |

`trigger_buffer = max(buffer_atr × ATR14(M15), buffer_spread_multiple × spread_at_evaluation)`.

Signal TTLs are therefore:

- XAUUSD: 60 minutes;
- EURUSD: 30 minutes;
- USDJPY: 60 minutes.

A Signal is valid only while `execution_time < expires_at`. Equality is expired.

### 4.4 Opportunity output

A passing evaluation returns:

```yaml
direction: LONG | SHORT
confidence: 0.70
reason_codes:
  - H4_TREND_CONFIRMED
  - H1_PULLBACK_RECLAIMED
  - M15_CONTINUATION_CONFIRMED
```

The fixed confidence is a deterministic seed for V0, not a probability of profit. DecisionEngine scores and RiskAssessment remain separate.

## 5. TrendFilteredBreakoutStrategy for WTI

### 5.1 H4 directional filter

WTI uses the H4 seed and directional rules defined normatively in section 4.1. There is no second WTI-specific copy of those parameters.

### 5.2 H1 range and compression setup

```yaml
range_lookback_h1: 12
atr_h1_period: 14
atr_short_h1_period: 6
atr_long_h1_period: 30
compression_ratio_max: 0.65
range_width_atr_min: 1.00
range_width_atr_max: 3.00
```

The range uses the 12 immediately preceding closed H1 candles and excludes a currently forming candle.

- `range_high = max(high)`.
- `range_low = min(low)`.
- `range_width = range_high - range_low`.
- Setup requires `1.00 × ATR14 <= range_width <= 3.00 × ATR14`.
- Setup requires `ATR6 / ATR30 <= 0.65`.
- Only the direction allowed by the H4 filter is eligible.

Any data discontinuity, known roll transition, or H1 close outside the range before the M15 trigger invalidates the setup.

### 5.3 M15 breakout trigger

```yaml
atr_m15_period: 14
break_buffer_atr: 0.10
min_body_atr: 0.50
max_extension_atr: 1.25
signal_ttl_m15_bars: 2
false_breakout_cooldown_m15_bars: 8
reopen_cooldown_minutes: 30
roll_guard_before_expiry_broker_trading_days: 3
```

LONG requires:

1. M15 close `> range_high + 0.10 × ATR14(M15)`.
2. Bullish body size `abs(close - open) >= 0.50 × ATR14(M15)`.
3. `close - range_high <= 1.25 × ATR14(M15)`.

SHORT is symmetric below `range_low`.

The Signal TTL is 30 minutes. `FALSE_BREAKOUT` means the first closed M15 candle after a trigger closes back inside the triggering H1 range before an Order is created. `STOPPED_BEFORE_TP1` means a resulting Position is closed by its initial StopLoss before TP1 is confirmed. Reconciliation/Position monitoring appends either source event and does not project cooldown. The account scheduler is the sole cooldown projection owner: it consumes that event and advances a durable cooldown through eight subsequent closed canonical M15 candle times for that BrokerAccount, Pair, and strategy logical config. A new H1 range with a different final candle ID may start a new setup only after cooldown. The resulting `PairEntryGateSnapshot` is an upstream eligibility gate; roll/reopen/cooldown state is never passed to Strategy.

A broker trading day is a broker-local calendar date with at least one configured Symbol trading session. For a fixed-expiry Symbol, the roll guard starts at the first session open on the third broker trading day before `expiration_time` and remains active through expiry or confirmed mapping to the next contract, whichever is later. For a continuous Symbol, the broker-published roll window is authoritative; activation remains blocked when no auditable roll window exists. The account scheduler derives these facts from versioned broker-session and roll-window records and persists their references in `PairEntryGateSnapshot`.

The reopen cooldown interval is half-open: `[broker_reopen_time, broker_reopen_time + 30 minutes)`. Entry becomes eligible exactly at `broker_reopen_time + 30 minutes`, subject to all other gates.

WTI evaluation returns confidence `0.68` with reason codes `H4_TREND_CONFIRMED`, `H1_COMPRESSION_CONFIRMED`, and `M15_RANGE_BREAK_CONFIRMED`.

## 6. Entry protection and staged exit policy

All four templates share the following exit policy.

### 6.1 Initial StopLoss

`R` is the absolute distance from the fresh executable entry quote to the final quantized initial native StopLoss.

For TrendPullbackContinuationStrategy:

- LONG structure level is the lower of the two H1 setup lows and the lowest low of the last six closed M15 candles.
- SHORT structure level is the higher of the two H1 setup highs and the highest high of the last six closed M15 candles.
- Add an outward buffer of `0.25 × ATR14(H1)`.
- Enforce a minimum stop distance of `0.80 × ATR14(H1)` by moving the StopLoss farther from entry when necessary.
- Reject the Signal when required distance exceeds `2.00 × ATR14(H1)`.

For WTI TrendFilteredBreakoutStrategy:

- LONG structure level is the lowest low of the six closed M15 candles ending with the trigger candle; SHORT uses the highest high of the same six candles. Retest inference is not part of V0.
- Add an outward buffer of `0.25 × ATR14(M15)`.
- Enforce minimum distance `1.00 × ATR14(M15)`.
- Reject the Signal when required distance exceeds `2.00 × ATR14(H1)`.

The ExecutionEngine quantizes the price away from entry to the broker tick size, then validates stops/freeze levels. Native SL is submitted with the entry Order and is not considered active until broker reconciliation confirms it.

### 6.2 Target stages

At entry, submit one native safety TakeProfit at `4.00R` for the entire initial position.

Durable reduce-only PositionCommands implement staged exits:

1. **TP1 at `1.00R`:** close 40% of initial filled volume.
2. **TP2 at `2.00R`:** close 30% of initial filled volume.
3. **Runner:** trail the remaining volume, nominally 30%.

TP1 and TP2 volumes are rounded down to the broker volume step. The runner receives all residual volume, including rounding residuals. If either TP1 or TP2 would be below minimum volume, that Signal is ineligible with `UNSPLITTABLE_POSITION_SIZE`; the engine does not increase total volume to make the split fit.

Crossing a target price does not advance the stage. The stage advances only after the matching broker Fill is reconciled.

### 6.3 Trailing runner

Trailing starts only after TP2 Fill is confirmed.

- Indicator: ATR(14) on closed M15 candles.
- Distance: `2.00 × ATR14(M15)`.
- LONG candidate: `highest_high_of_closed_M15_since_entry - trailing_distance`.
- SHORT candidate: `lowest_low_of_closed_M15_since_entry + trailing_distance`.
- Recalculate once per newly closed M15 candle.
- Quantize to broker tick size and update only when the resulting native SL is strictly tighter than the confirmed SL and satisfies stops/freeze constraints.
- A rejected or UNKNOWN modification leaves the last confirmed native SL authoritative and enters reconciliation; it is never blindly retried.
- Native safety TP at 4R remains active for the runner.

When automated trailing is not capability-safe, the runner retains its last confirmed native SL and the 4R native safety TP. The account receives an audited degradation warning; the engine never approximates trailing with an opposite Order.

### 6.4 Strategy exit

A confirmed opposite H4 directional filter closes all remaining volume through a durable reduce-only PositionCommand. A merely neutral/failed H4 filter does not force an exit; native SL, target stages, and runner management continue.

Signal TTL never closes an existing Position.

## 7. StrategyConfig seed payloads

All templates use:

```yaml
minimum_score: 70
entry_order_type: MARKET
exit_policy:
  tp1_r: 1.00
  tp1_fraction: 0.40
  tp2_r: 2.00
  tp2_fraction: 0.30
  runner_fraction_nominal: 0.30
  native_safety_tp_r: 4.00
  trailing:
    starts_after: TP2_CONFIRMED
    timeframe: M15
    atr_period: 14
    atr_multiple: 2.00
    update_on: CLOSED_CANDLE
```

The three trend templates use the common section 4 parameters plus their pair-specific section 4.3 values. WTI uses section 5.

`minimum_score` is applied by DecisionEngine after enrichment. It does not change the pure Strategy pass/fail rules.

## 8. Minimum lookback

A MarketStateSnapshot is COMPLETE only when it has at least:

| Template | H4 closed bars | H1 closed bars | M15 closed bars |
|---|---:|---:|---:|
| XAUUSD trend-pullback | 450 | 150 | 60 |
| EURUSD trend-pullback | 450 | 150 | 60 |
| USDJPY trend-pullback | 450 | 150 | 60 |
| WTI trend-breakout | 450 | 300 | 500 |

The H4 requirement is three times EMA150. Trend H1/M15 requirements cover indicator warm-up plus setup/channel input. WTI provisions longer H1/M15 series for compression, discontinuity, session, and roll-aware baselines.

Exactly one missing required bar, a time gap, or a mismatched source revision makes the snapshot incomplete. DataEngine/FeatureEngine skip Strategy evaluation and emit the auditable reason; Strategy never receives that snapshot.

## 9. Activation behavior

- Templates are shipped disabled.
- Enabling selects one existing immutable StrategyConfig version for one BrokerAccount and Pair; activation never mutates it.
- Before activation, the UI requires an explicit entry-session choice. A changed choice is persisted by creating a new config version, followed by a separate activation command for that version.
- No demo-duration, historical-performance, trade-count, or broker-history calibration threshold blocks activation.
- Minimum technical safety checks may block activation as described in Backend V0 and section 2.1.
- Demo and live BrokerAccounts use the same deterministic Strategy rules. Activation on one account never activates another.
- Updating any parameter creates a new version; active Opportunities and Signals retain their original version references.

## 10. Deterministic scenario fixtures

The normative seed data and expected outputs are in [`fixtures/strategy-v0-cases.json`](fixtures/strategy-v0-cases.json). Its expansion contracts are executable requirements, not prose hints: they produce complete account-scoped Candle and IndicatorValue records, deterministic mirrored SHORT cases, and exact minimum-lookback prefixes. CI expands those contracts first, validates every resulting foreign key/account scope/timestamp/revision, then applies each mutation independently. The stdlib-only [`validate_strategy_v0.py`](fixtures/validate_strategy_v0.py) validator checks contract shape, coverage matrices, RFC 8785 hashes, chronology/account scope, and raw-bar indicator vectors. Expected outputs compare complete reason codes and values, not only whether an Opportunity exists.

### 10.1 Shared data and isolation fixtures

1. **Closed-candle alignment:** only the latest H1/H4 candles with `close_time <= M15.close_time` are read.
2. **Open candle rejected:** replacing one required input with an open candle makes the pipeline skip Strategy evaluation and audit `OPEN_CANDLE_INPUT`.
3. **Lookback short by one:** each timeframe tested one bar below section 8 makes the pipeline skip evaluation and audit `INSUFFICIENT_LOOKBACK`.
4. **Gap detected:** one missing canonical candle makes the pipeline skip evaluation and audit `GAP_DETECTED`.
5. **Revision mismatch:** mixed candle revisions make the pipeline skip evaluation and audit `REVISION_MISMATCH`.
6. **Account isolation:** changing only a case's BrokerAccount while retaining account-A records is rejected; a deep clone that creates a distinct Pair, Candles, IndicatorValues, MarketStateSnapshot, and PairEntryGateSnapshot for account B produces a separate evaluation and shares no execution state.
7. **Config version isolation:** identical market inputs under two immutable config versions produce distinct evaluation keys.
8. **Replay deduplication:** replaying the same snapshot/config version does not create a second Opportunity.
9. **Directional conflict:** simultaneous LONG and SHORT truth yields no Opportunity and `CONFLICTING_SETUPS`.
10. **Boundary determinism:** equality at every inclusive threshold passes; equality at every strict breakout comparison fails.

### 10.2 Trend-pullback fixtures for each Pair

For each of XAUUSD, EURUSD, and USDJPY, fixtures provide a concrete valid LONG, a fully materialized mirrored SHORT, and assertions for that Pair's exact channel, buffer, extension, and TTL parameters. Because H4/H1 mechanics are one shared plugin contract, their failure and equality boundaries are exercised once on the canonical EURUSD fixture rather than duplicated at unrelated price scales; the deterministic mirror contract separately proves comparison inversion for every Pair:

1. Valid LONG produces one Opportunity with confidence 0.70 and the three success reason codes.
2. Mirrored valid SHORT produces one Opportunity with identical confidence.
3. Price on wrong side of EMA150 fails.
4. EMA34/EMA150 order fails.
5. EMA separation at the configured `ema_separation_atr` threshold passes; one Decimal quantum below fails.
6. EMA150 slope equality over configured `slope_bars` fails; strictly directional slope passes.
7. ADX exactly at configured `adx_min` passes; one Decimal quantum below fails.
8. No EMA34 pullback in the last two H1 bars fails.
9. H1 close beyond the 0.50 ATR invalidation boundary fails.
10. H1 body fraction exactly 0.55 passes; one Decimal quantum below fails.
11. RSI cross from 49.99 to 50.00 passes LONG; an RSI that remains above 50 does not count as a new reclaim.
12. Intrabar M15 channel breach with close inside fails.
13. Close exactly at buffered channel boundary fails because breakout is strict.
14. Directional M15 close one Decimal quantum beyond the boundary passes.
15. Trigger body one Decimal quantum below 0.55 fails.
16. Extension exactly 1.00 ATR passes; above it fails with `TRIGGER_OVEREXTENDED`.
17. Signal execution one quantum before expiry passes the TTL gate; equality at expiry fails.
18. A second M15 evaluation for the same armed H1 setup may create a Signal only when it has a new evaluation key and no active Signal/Order already owns that setup.

### 10.3 WTI fixtures

1. Valid WTI mapping plus H4 trend, H1 compression, and M15 breakout produces confidence 0.68.
2. Ambiguous underlying mapping blocks activation.
3. Unknown roll semantics blocks activation.
4. Range width exactly 1.00 or 3.00 ATR passes; outside either bound fails.
5. Compression ratio exactly 0.65 passes; above it fails.
6. A forming H1 candle is excluded from the 12-bar Donchian range.
7. M15 close exactly at buffered range boundary fails; one quantum beyond passes.
8. Body exactly 0.50 ATR passes; below fails.
9. Extension exactly 1.25 ATR passes; above fails.
10. Known roll guard or broker daily reopen cooldown yields `SKIP_EVALUATION` from the Pair entry gate; data discontinuity yields `SKIP_EVALUATION` from MarketState completeness. Strategy is not called in any of these cases.
11. Signal execution at 30-minute expiry fails.
12. `FALSE_BREAKOUT` or `STOPPED_BEFORE_TP1`, as defined in section 5.3, starts the configured scheduler-owned cooldown; a new setup before completion is skipped before Strategy evaluation.
13. A new H1 range with a different final candle ID after cooldown is eligible.

### 10.4 Execution and exit fixtures

1. Market entry uses the first fresh quote after trigger and reruns PRE_ORDER risk assessment. The Opportunity/initial assessment retain `evaluation_pair_entry_gate_snapshot_id`; PRE_ORDER materializes and records a distinct, fresh `pre_order_pair_entry_gate_snapshot_id` and must not reuse the evaluation snapshot.
2. Native SL and native 4R safety TP are present in the entry request.
3. Broker-confirmed protection is required before exposure quarantine is released.
4. TP1 price crossing without a Fill does not change stage.
5. Confirmed TP1 Fill closes the rounded-down 40% target and advances to TP1 confirmed.
6. Confirmed TP2 Fill closes the rounded-down 30% target and starts runner trailing.
7. Rounding residual belongs to the runner; total requested reductions never exceed filled volume.
8. An unsplittable volume blocks that Signal with `UNSPLITTABLE_POSITION_SIZE`.
9. Trailing updates only after a new closed M15 candle and never loosens confirmed SL.
10. Stops/freeze-level violation skips the modification and records a reason.
11. UNKNOWN trailing modification enters reconciliation without blind retry.
12. Unsupported safe trailing preserves fixed native protection and emits a degradation warning.
13. Opposite H4 filter emits one idempotent reduce-only close command; neutral H4 does not.
14. Signal expiry does not close or mutate an existing Position.
15. MT5 disconnect leaves the confirmed native SL and 4R safety TP active.

### 10.5 Indicator vectors

The fixture file includes raw closed bars with an explicit predecessor and exact scale-18 EMA/ATR/RSI/ADX outputs. Each output includes its IndicatorDefinition ID, concrete parameters, and RFC 8785 parameter hash. These vectors are contract tests for `algorithm_spec`; injected terminal IndicatorValues in Strategy fixtures test Strategy behavior separately.

## 11. Acceptance criteria

The V0 strategy-template implementation is complete when:

1. Both Strategy plugins are pure, deterministic, versioned, and account-scoped.
2. All four disabled templates serialize to the exact parameters in this specification.
3. IndicatorDefinition versions make EMA/ATR/ADX/RSI outputs reproducible.
4. Every fixture in section 10 passes for netting and hedging BrokerAccount projections where applicable.
5. Every no-trade path records a stable reason code.
6. Signal TTL, idempotency, versioning, session selection, and conflict behavior match Backend V0.
7. Native protection, partial reductions, trailing, and degradation behavior pass connector contract tests without weakening Backend V0 safety fences.
8. UI copy identifies values as editable V0 seeds and does not describe them as optimized or profitable.
9. No historical calibration, paper engine, or future mean-reversion template is silently added to V0 scope.
