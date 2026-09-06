# EURUSD SND/AO/QM Strategy — V0 Spec (Override B)

**Status:** ACCEPTED (decision B). Implements the user-selected QM + AO + supply/demand creator framework as the EURUSD V0 strategy.
**Supersedes (EURUSD only):** the EURUSD entry in `strategy-templates-v0.md`.
**Unaffected:** XAUUSD + USDJPY keep `TrendPullbackContinuationStrategy`; WTI keeps `TrendFilteredBreakoutStrategy`.
**Decision ticket:** issue #12 (initial V0 strategies) and issue #14 (EURUSD research).
**Backend contract:** `docs/spec/backend-v0.md`; domain language: `CONTEXT.md`.
**Calibration stance:** every numeric parameter in §8 is a **PROPOSED TEST SEED**, not a validated or profitable value. Authorized by env `TECHNICAL_DEFAULTS_AUTHORIZATION=PROPOSE_TEST_AND_CONSOLIDATED_REVIEW`. No activation until the readiness/validation gate passes.

> Why this override: the accepted EURUSD research (`docs/research/eurusd-strategy-v0.md`, issue #14) selected a trend-pullback/channel-continuation design and never examined the QM + AO + supply/demand creator framework the user supplied. The user has chosen that creator framework as the EURUSD V0 source of truth (decision B). This spec captures that design deterministically.

---

## 0. Strategy identity

- **Canonical Pair:** `EURUSD`
- **Plugin key:** `SupplyDemandAOQMStrategy@0.1.0`
- **Template key:** `eurusd-snd-ao-qm-bidirectional-v0`
- **Directions:** LONG and SHORT (mirrored, bidirectional)
- **Trigger timeframe:** M30 (confirmation). Trend and zone identification use H4.
- **Entry order type:** **Pending Limit** (preplaced at retest), NOT Market. Supported by backend `main` §6.18 and `CONTEXT.md` (Buy/Sell Limit canonical). No backend change needed for Limit itself.
- **Required timeframes:** H4 (trend, supply/demand zones), M30 (AO divergence, structure break, QM confirmation, entry).
- **Default state:** disabled until activation gate passes.

Evaluation semantics follow Backend V0: strategy evaluates once per newly **closed** canonical M30 candle; reads only complete, closed H4/M30 candles referenced by one `MarketStateSnapshot`; returns `None` on incomplete/conflicting state; never uses intrabar touch as a trigger; LONG/SHORT rules mirrored; simultaneous LONG and SHORT truth returns `None` with `CONFLICTING_SETUPS`.

**Opportunity output:** a passing evaluation returns a single Opportunity:

```yaml
direction: LONG | SHORT
confidence: 0.72
reason_codes:
  - H4_REGIME_CONFIRMED
  - H4_ZONE_VALID
  - M30_AO_DIVERGENCE
  - M30_QM_CONFIRMED
  - RETEST_LIMIT_ARMED
```

The fixed confidence `0.72` is a **deterministic seed for V0**, not a probability of profit (DecisionEngine scores and RiskAssessment remain separate). Reason-code composition is the deterministic contract checked by fixtures: all five must be present on a passing evaluation.

**Minimum lookback (MarketStateSnapshot COMPLETE only with at least):**

| Timeframe | Closed bars | Rationale |
|---|---:|---|
| H4 | 200 | swing identification + zone base/departure history + ATR14 warm-up |
| M30 | 60 | AO 5/34 warm-up, divergence pivot history, QM A→B→C window |

Exactly one missing required bar, a time gap, or a mismatched source revision makes the snapshot incomplete and evaluation is skipped with an auditable reason; Strategy never receives that snapshot. The M30 trigger candle plus the latest closed H4 candles with `close_time <= trigger_time` are read; never use an open or future H4 candle.

---

## 1. H4 trend regime (structure-led)

Design approval: `EURUSD_TREND_MODEL=STRUCTURE_LED_REGIME_V0`, trend TF H4.

1. Identify volatility-scaled major H4 swings. A swing is a local pivot whose reversal leg exceeds a **volatility threshold** (§8); confirm only on a completed H4 candle, never early with a future candle. Confirmed swing records are immutable and carry `(pivot_time, confirmation_time)`.
2. Regime is one of: `BEARISH_IMPULSIVE`, `BEARISH_PULLBACK`, `BULLISH_IMPULSIVE`, `BULLISH_PULLBACK`, `TRANSITION`, `RANGE`, `UNKNOWN`.
3. A **structural break** is a completed H4 close beyond the preceding confirmed swing extreme plus a volatility-scaled buffer, accepted by an acceptance rule (§8). After an invalidated structure the regime enters `TRANSITION` before re-resolving.
4. Track the significant swing preceding a confirmed break as the **trend invalidation reference**. Mirror bullish/bearish.
5. Pullbacks into matching H4 zones may arm M30 setups. Impulses are **not chased**.
6. `TRANSITION`, `RANGE`, `UNKNOWN` **block** new trend-following entries.
7. ADX/DMI are supplemental strength/direction **diagnostics only**; they never override the regime decision.

**Entry eligibility:**
- LONG requires `BULLISH_IMPULSIVE`/`BULLISH_PULLBACK` and price in/near a valid H4 demand zone.
- SHORT requires `BEARISH_IMPULSIVE`/`BEARISH_PULLBACK` and price in/near a valid H4 supply zone.
- Any other regime: no new trend-following entry.

---

## 2. H4 supply/demand zones

Design approval: `EURUSD_ZONE_MODEL=COMPRESSION_DEPARTURE_V0`, zone TF H4, boundary `FULL_BASE_WICK_RANGE`.

- **Supply** = drop-base-drop. **Demand** = rally-base-rally.
- Identify a **base** as a consolidation on H4 whose compression, candle overlap, body size, and duration meet thresholds (§8).
- Measure base compression against H4 volatility. No mandatory candle color or named pattern.
- Require a **decisive departure** — a closed candle leaving the base — as confirmation. Measure distance, speed, close location, opposing wicks (§8).
- A wider structural break is **supporting evidence only**, not mandatory for every continuation zone.
- Both zone types use the **lowest base low** and **highest base high** as boundaries.
- Freeze boundaries at confirmation. Retain base start/end, confirmation time, subsequent visit count, penetration depth, invalidation status.
- A zone becomes usable **only after departure confirmation**; never backdate.
- Freshness advantage is a **testable hypothesis**, not an established fact.
- M30 rejection/engulfing candles are **supporting evidence only**; they cannot bypass AO divergence + structure break + QM confirmation.
- H4 zone boundaries do **not** automatically define M30 entry or stop prices.

---

## 3. M30 AO divergence (arms; never enters alone)

Design approval: `EURUSD_AO_DIVERGENCE_DESIGN_STATUS=APPROVED`, fast 5, slow 34, price source median, pivot matching same-price-pivot candle, M30 closed.

- `AO = SMA(median,5) − SMA(median,34)` over closed M30, median = `(high+low)/2`.
- **Bearish divergence (SHORT):** two confirmed M30 price highs in the **same pullback**; second high higher; AO at second pivot lower than at first; second price high inside a valid H4 supply zone. Require meaningful price and AO differences (§8); record zero-line position; no mandatory zero-crossing or twin-peaks rule.
- **Bullish divergence (LONG):** mirror — two M30 lows, second lower, AO reading higher, inside valid H4 demand.
- Use confirmation timestamps; never backdate pivot availability.
- **Divergence only arms observation.** Structure break + QM confirmation + qualifying retest still required before entry eligible. Divergence alone produces **no** entry.
- **Cancel** warning on invalid H4 context/zone, superseding price extreme, or expiry. New extreme requires fresh divergence evaluation.

---

## 4. M30 QM (Quasimodo) confirmation + structure break

Design approval: `EURUSD_QM_DESIGN_STATUS=APPROVED`, entry preplaced limit at near edge, break `M30_CLOSE_BEYOND_B_WITH_BUFFER`, no rejection-close wait.

**SHORT (QM sell):** confirmed M30 high **A** (left shoulder) → intervening **low B** → **higher high C** (head) inside valid H4 supply with bearish AO divergence A-vs-C → completed M30 close below B plus calibrated buffer.
**LONG (QM buy):** mirrored low A → high B → lower low C inside valid H4 demand with bullish divergence → completed M30 close above B plus buffer.

**Matching rule:** match the exact A→B→C sequence, not arbitrary swings. Skip candidates without a meaningful wick (§8).

**Entry area:**
- SHORT: upper body edge through high of A candle.
- LONG: low of A candle through lower body edge.

**Order placement:**
- After break confirmation + risk checks, **preplace** a Sell Limit at the lower edge of the SHORT area (SHORT) / Buy Limit at the upper edge of the LONG area (LONG).
- **No additional M30 rejection-close wait.** Once the limit is in place, broker execution applies on a retest fill. No retest = no trade; **never chase** with Market.
- Pending orders reserve risk capacity and require native protective stops.
- Cancel on: expiry, invalid head/context/zone, or entry blackout. Reconcile fills that race cancellation.

---

## 5. Stop-loss

Design approval: `EURUSD_STOP_REFERENCE=QM_HEAD_C`, broker-native hard stop, buffer `VOLATILITY_AND_BROKER_DISTANCE`.

- SHORT: native StopLoss **above head C**. LONG: native StopLoss **below head C**.
- Add volatility buffer (§8) and satisfy current broker distance/tick/stops-level rules (connector handshake).
- Hard-stop execution does **not** wait for a candle close; fill price not guaranteed.
- Native SL submitted with the entry order; not considered active until broker reconciliation confirms it.

---

## 6. Take-profit (full exit, no extension)

Design approval: `EURUSD_TAKE_PROFIT_REFERENCE=NEAREST_VALID_OPPOSING_H4_ZONE_NEAR_EDGE`, `FULL_POSITION`, extension `DISABLED`.

- SHORT target: just above the near (upper) edge of the nearest valid H4 demand below entry. LONG target: just below the near (lower) edge of the nearest valid H4 supply above entry.
- Small execution buffer before the zone (§8) to avoid resting on the boundary.
- **Full-position exit** at target. **No target extension** in V0.

---

## 7. Reward-to-risk and session policy

Design approval: `EURUSD_MIN_REWARD_TO_RISK=2.00`, cost basis after costs, session `ANY_BROKER_TRADING_SESSION`.

- Require `planned_net_reward / planned_loss >= 2.00` before placing the entry order.
- Include estimated trading costs consistently, without double-counting spread.
- If the opposing zone is too close, **skip** — never extend the target to qualify.
- Session: **no** clock filter for EURUSD entries. Entry still requires broker market open, valid quotes/setup, news-blackout compliance, all risk/execution guards.

---

## 8. Proposed test seeds (CALIBRATION PENDING — consolidated review passed)

Proposed starting values, each `CALIBRATION_PENDING`. Must be validated on unseen EURUSD data (incl. costs and pivot-confirmation delays) before any activation.

| Parameter | Proposed seed | Notes |
|---|---|---|
| Volatility estimator | ATR(14) on H4 | swing/break/zone-compression scale |
| Swing threshold (H4) | reversal leg ≥ 1.0 × ATR14(H4) | minor-impulse distinction (§1) |
| Structural-break buffer | 0.25 × ATR14(H4) | completed close beyond swing extreme (§1) |
| Zone compression ratio | ATR6/ATR30 on H4 ≤ 0.65 | vs baseline |
| Zone base duration | ≥ 3 closed H4 candles | §2 |
| Zone departure | closed H4 close beyond base + 0.25 × ATR14(H4) | decisive departure (§2) |
| AO min price diff | 0.30 × ATR14(M30) | meaningful price difference (§3) |
| AO min AO diff | 0.30 × ATR14(M30) | meaningful AO difference (§3) |
| M30 break buffer (B) | 0.10 × ATR14(M30) | QM break confirmation (§4) |
| Minimum QM wick | 0.30 × ATR14(M30) | skip candidates w/o meaningful wick (§4) |
| SL volatility buffer | 0.25 × ATR14(M30) outward | §5 |
| TP pre-zone buffer | 0.10 × ATR14(M30) | §6 |
| Signal TTL | 30 minutes | must satisfy retest window |
| Pending-order expiry | 120 minutes after placement | no infinite resting order |
| Entry session | ANY | §7 |

Seeds deliberately reuse conventions in `strategy-templates-v0.md` (0.25 ATR buffers, ATR6/ATR30 compression 0.65) to minimize novel untested parameters.

---

## 9. Open items requiring resolution before activation

1. **Pending-order native SL/TP** — confirm the connector can attach protective SL/TP at pending-order creation, not only at fill. If fill-only, the pending order is naked in the interim; adjust risk accounting accordingly.
2. **XAUUSD scope** — XAUUSD stays `INACTIVE_PENDING_RESEARCH_AND_VALIDATION`. QM/AO is EURUSD only; XAUUSD uses TrendPullback or a separately validated framework.
3. **Order lifecycle for pending-limit** — expiry, duplicate/re-entry controls, disconnect recovery, risk-reset accounting on fill/cancel. Backend supports Pending lifecycle but fixtures must exercise Limit fill/cancel reconciliation.
4. Whether QM/AO is the **only** EURUSD strategy or coexists as a challenger beside the non-EURUSD TrendPullback plugin.

---

## 10. Documents produced/changed by this spec

- **NEW** (this): `docs/spec/strategy-eurusd-snd-ao-qm-v0.md`.
- **EDIT** `docs/spec/strategy-templates-v0.md`: remove EURUSD from TrendPullback matrix; keep XAUUSD + USDJPY; note EURUSD served by QM/AO.
- **EDIT** `docs/research/eurusd-strategy-v0.md`: record that the QM/AO creator framework was evaluated and selected over trend-pullback for EURUSD V0.
- **NEW** fixtures + validator updates for EURUSD QM/AO deterministic cases (Backend V0 contract).
- **NO change** to `backend-v0.md` core (Limit already supported). Templates-branch backend additions (`PairEntryGateSnapshot`, `algorithm_spec`, session/roll records) still need a separate merge decision.

**Status:** ACCEPTED as design; not activated. Awaiting resolution of §9 open items and validation before activation gate.
