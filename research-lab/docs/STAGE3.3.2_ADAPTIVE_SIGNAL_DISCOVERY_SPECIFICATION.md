# Stage 3.3.2 — Adaptive Signal Discovery Architecture Specification

**Stage type:** Architecture and research planning only  
**Status:** Planning document — **not** an implementation authorization  
**Scope:** `research-lab` design for one adaptive paper-scalping system with four specialized engines  

This document does **not** authorize code, tests, historical research, scoring, optimization, live capture, or production promotion. It does **not** claim profitability or predictive accuracy.

---

## 1. Objective

Build **one** adaptive trading system composed of:

- Shared market data / quality layer  
- Market intelligence (Stage 3.3.1 characterization)  
- Opportunity detection  
- Four specialized strategy engines (S1–S4)  
- An adaptive strategy controller  
- Existing central risk + PaperBroker execution (unchanged)

The system must be able to select a primary engine, optionally use confirmation, or choose **NO TRADE**.

Frozen B0/H1 baselines remain untouched research references. S4 is a **new** research candidate and does not replace them.

---

## 2. Authorization boundaries (inherited)

| Constraint | Rule |
| --- | --- |
| Cost | Strict €0 unless separately approved |
| Mode | Paper-only; no live orders; no authenticated trading endpoints |
| Instruments | Spot BTC/USDT; no leverage / margin / futures |
| Frozen | No edits to B0/H1 rules/params, Stage 2 replay, PaperBroker economics, quote-tape sessions, sealed artifacts |
| Holdout | Never access/score/tune against sealed holdout (`2026-10-31T04:20:59.999Z`) |
| Evidence tracks | Historical candle research ≠ quote-tape research |
| Code location | Future work under `F:\crypto-ai-scalper\research-lab\` only |
| Forbidden peers | Never access `F:\crypto-ai-trade-v4` |
| This stage | Planning docs only — no implementation |

---

## 3. Full adaptive architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Layer 1 — Market Data & Quality                                 │
│  Validated OHLCV / timestamps / gaps / stale / no look-ahead    │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│ Layer 2 — Market Intelligence (Stage 3.3.1)                     │
│  PrimaryRegime + Volatility + Momentum + Strength + DQ status   │
│  Confirmed regime (persistence/hysteresis) + uncertainty notes  │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│ Layer 3 — Opportunity Detection                                 │
│  Gross proxy / costs / net edge / incomplete-cost fail-closed   │
│  (extends 3.3.1 opportunity; per-engine movement proxies later) │
└───────────────────────────────┬─────────────────────────────────┘
                                │
          ┌─────────────────────┼─────────────────────┐
          ▼                     ▼                     ▼
     ┌─────────┐          ┌─────────┐          ┌─────────┐
     │   S1    │          │   S2    │   ...    │   S4    │
     │ engines │          │ engines │          │ engines │
     └───┬─────┘          └───┬─────┘          └───┬─────┘
         │                    │                    │
         └────────────┬───────┴────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│ Layer 4 — Adaptive Strategy Controller (deterministic rules)    │
│  Eligibility → candidates → confidence evidence → costs →       │
│  primary/confirm/conflict → NO TRADE or StrategyRecommendation  │
└───────────────────────────────┬─────────────────────────────────┘
                                │ recommendation only
┌───────────────────────────────▼─────────────────────────────────┐
│ Existing Risk Engine + PaperBroker (UNCHANGED)                  │
│  Independent permit/deny; fees/spread/slip/risk limits frozen   │
└─────────────────────────────────────────────────────────────────┘
```

### Design principles

1. **One system, many engines** — shared interfaces and one risk path.  
2. **Regime ≠ opportunity ≠ signal ≠ risk approval** — never collapse these.  
3. **Overlapping secondary traits** stay independent (Stage 3.3.1 contract).  
4. **NO TRADE is a first-class outcome.**  
5. **Deterministic, rule-based controller initially** — no ML in this architecture stage.  
6. **Controller recommends; risk decides.**  
7. **Expandable** — new engines plug into the same candidate contract without redesigning risk.

---

## 4. Layer 1 — Market data and quality

### Responsibilities

- Provide closed-candle (and later tape-derived) observations with consistent UTC timestamps.  
- Reject invalid OHLC, non-positive prices, naive timestamps, duplicates, non-monotonic series.  
- Detect gaps / missing intervals / stale readiness.  
- Ensure all downstream layers see the **same** decision-time cutoff (no future bars).

### Existing foundations (do not invent unsupported capabilities)

- Stage 3.3.1: `CharacterizationCandle`, `validate_candle_series`, `DataQualityStatus`  
- Lab backtesting: chronological candle replay, cutoff helpers, synthetic quote labelling  
- Stage 2: quote-tape sessions remain a **separate** evidence track  

### Requirements

| Requirement | Behavior |
| --- | --- |
| Invalid data | Fail closed; no silent repair |
| Gaps | Propagate gap status; intelligence may reset confirmed regime (3.3.1) |
| Look-ahead | Only bars available at candle close / decision_time |
| Paid data | Forbidden without explicit approval |

### Limitation to document

Layer 1 does not yet define a unified “tape + candle” merger for adaptive control. Until separately designed, candle-path intelligence and tape-path execution research stay dual-track.

---

## 5. Layer 2 — Market intelligence engine

**Implementation dependency:** Stage 3.3.1 package `research_lab.characterization`.

### Consumed outputs (actual)

From `MarketCharacterization` / related enums:

| Field | Use in adaptive system |
| --- | --- |
| `primary.regime` / `confirmed_regime` | Eligibility gating |
| `primary.raw_regime`, `conflicting` | Uncertainty / transition handling |
| `secondary.volatility` | High/low/normal/unknown overlap |
| `secondary.momentum` | Directional bias / S4 affinity |
| `secondary.directional_strength` | Strength evidence |
| `data_quality.status` | Hard NO TRADE when INVALID / INSUFFICIENT_DATA (policy) |
| `features.*` | Engine-local structure (ATR, efficiency, range bounds) |
| `opportunity` | Baseline cost-adjusted filter (ATR proxy) |
| `persistence_count` | Switching / confirmation evidence |

### Primary regimes (controller vocabulary)

- `TRENDING`  
- `RANGING`  
- `TRANSITIONING`  
- `UNKNOWN`  

### Secondary (independent, overlapping)

Example legal joint state:  
`TRENDING + HIGH_VOLATILITY + STRONG_UP + STRONG directional_strength`.

Do **not** encode high/low volatility as mutually exclusive primary regimes.

### Gaps relative to this architecture (explicit)

Stage 3.3.1 does **not** yet expose:

- Named “market structure” object beyond rolling high/low / range width / EMA structure features  
- Explicit “classification uncertainty” scalar — uncertainty is represented via `UNKNOWN`, `TRANSITIONING`, `conflicting`, DQ status, and awaiting-persistence  

Future work may add structured uncertainty summaries **without** inventing probability-of-profit scores.

---

## 6. Layer 3 — Opportunity detection

### Distinction

| Concept | Meaning |
| --- | --- |
| Market conditions | Regime + secondary traits |
| Strategy eligibility | Engine allowed to propose in this condition |
| Signal validity | Engine-specific pattern confirmed / pending / invalid |
| Opportunity quality | Expected movement vs costs |
| Risk approval | Separate central engine (later) |

A preferred regime **does not** imply a tradable opportunity.

### Cost model (reuse only)

Frozen paper assumptions (Stage 3.3.1 / `BacktestSettings` defaults):

- `fee_rate_per_side = 0.001`  
- `spread_fraction = 0.0002`  
- `slippage_rate_per_side = 0.0005`  
- Round-trip ≈ `2×fee + spread + 2×slip`  

Incomplete cost information → opportunity status incomplete/unavailable → **cannot** become a positive selection.

### Gross movement

- Stage 3.3.1 baseline: `normalized_atr × multiplier` (proxy, not forecast, not MFE).  
- Per-engine research may define **engine-specific** gross proxies (pullback depth, sweep size, expansion range) but must:
  - Remain labelled as proxies  
  - Feed the same cost components  
  - Never treat historical MFE as guaranteed future move  

### Rejection duties

Reject when gross/net insufficient, costs incomplete, or proxy unavailable.

---

## 7. Layer 4 — Adaptive strategy controller

Central deterministic intelligence. **Rule-based initially. No ML.**

### Responsibilities

1. Ingest Layer 2 (+ Layer 3 baseline) outputs.  
2. Evaluate conditions.  
3. Determine engine eligibility (matrix + hard NO TRADE rules).  
4. Collect candidate objects from eligible engines.  
5. Evaluate confidence **evidence** (not win-probability %).  
6. Assess cost-adjusted opportunity per candidate.  
7. Select primary engine and optional confirmation role.  
8. Reject unsuitable / conflicting / correlated duplicates.  
9. Emit **NO TRADE** when warranted.  
10. Persist a complete decision record (inputs, eligibility, candidates, reasons).

### A. Regime adaptation

Eligibility follows confirmed market conditions (matrix below), not wall-clock schedules.

### B. Opportunity adaptation

Even if eligible, a candidate needs:

- Valid / confirmed signal status  
- Sufficient confidence evidence  
- Passing cost-adjusted opportunity check  

### C. Confidence assessment (evidence framework)

See §10. No arbitrary percentages. No probability-of-profit.

### D. Strategy switching safeguards

| Safeguard | Intent |
| --- | --- |
| Regime confirmation | Prefer `confirmed_regime` over raw flicker (3.3.1 persistence/hysteresis) |
| Controller hysteresis | Extra bars before changing **selected primary engine** |
| Switching cooldown | Minimum bars/time after a switch before another switch |
| Minimum evidence | Require persistence_count / confirmation status thresholds |
| Position-state awareness | Do not switch engines mid-open-position in ways that create duplicate exposure; exits follow existing risk/broker rules |
| Explicit transition policy | On `TRANSITIONING` / `UNKNOWN` → force NO TRADE (default hypothesis) |

### E. Multi-strategy coordination

- One **primary** candidate maximum per decision.  
- At most one **confirmation** engine when the matrix marks Secondary/Conditional and directions agree.  
- Confirmation **does not** increase size or open a second position.  
- Correlated near-duplicate setups (same direction, overlapping invalidation, same bar) count as **one** opportunity.  
- Conflicting directions or incompatible invalidations → reject both (NO TRADE) unless a documented priority rule applies (default: reject).  
- Risk remains centralized — confirmation never bypasses risk.

### F. Mandatory NO TRADE conditions

Controller must choose NO TRADE when any hold:

- `confirmed_regime` is `UNKNOWN` or `TRANSITIONING` (default policy)  
- Data quality `INVALID` or `INSUFFICIENT_DATA`  
- Signals conflict (direction / structure)  
- Expected movement does not justify costs / opportunity rejected  
- Confidence evidence insufficient  
- No eligible engine / all engines ineligible or no-signal  
- Risk constraints would prohibit entry (if risk pre-check is consulted; final deny still at risk engine)  
- Low-volatility compression with matrix “No” for all engines  

---

## 8. Four specialized strategy engines

All engines are **research designs**. None are implemented in this stage. None may modify frozen B0/H1.

### S1 — Trend Pullback Continuation

| Item | Spec |
| --- | --- |
| Purpose | Enter established trend after temporary retracement |
| Preferred environment | `TRENDING` |
| Research requirements | Trend qualification; pullback identification; continuation confirmation; entry invalidation; opportunity expiry; cost-adjusted movement |
| Side | Long-only (spot constraint) unless future approved otherwise |
| Notes | Uses intelligence trend + structure features; must invalidate if trend breaks |

### S2 — Liquidity Sweep Reversal

| Item | Spec |
| --- | --- |
| Purpose | Failed breaks of established boundaries (sweep then reject) |
| Preferred environment | `RANGING` |
| Research requirements | Recent highs/lows; temporary breach; rejection confirmation; range invalidation; distinguish rejection vs continued breakout |
| Side | **Long-side spot only** (initial restriction) |
| Notes | High risk of false “rejection”; confirmation mandatory before Confirmed status |

### S3 — Volatility Expansion Breakout

| Item | Spec |
| --- | --- |
| Purpose | Directional breakout after compression |
| Preferred environment | Confirmed volatility expansion (`HIGH_VOLATILITY` after prior compression evidence) |
| Research requirements | Compression detection; expansion confirmation; breakout validation; false-breakout rejection; expiry; cost-adjusted movement |
| Side | Long-only initially |
| Notes | Matrix: Primary only when expansion **confirmed**, not merely `HIGH_VOLATILITY` label |

### S4 — Momentum Confirmation

| Item | Spec |
| --- | --- |
| Purpose | Simple directional momentum candidate **and** optional confirmation engine |
| Preferred environment | `TRENDING` or confirmed directional expansion |
| Research requirements | Momentum qualification; directional + price-structure confirmation; weakening detection; invalidation; cost-adjusted assessment |
| Side | Long-only initially |
| Notes | **New research candidate** — must not modify/replace frozen B0/H1 |

---

## 9. Common strategy interface (contract only — not implemented)

### Design goal

Every engine returns a standardized **StrategyCandidate** so the controller can compare engines without engine-specific branches beyond eligibility.

### Proposed candidate fields

| Field | Description |
| --- | --- |
| `strategy_id` | `S1` / `S2` / `S3` / `S4` |
| `signal_id` | Stable id within run (deterministic from inputs) |
| `timestamp` | Decision / signal time (UTC aware) |
| `direction` | `long` (initially); future short only if spot policy changes |
| `eligibility_status` | Whether engine may run given intelligence |
| `signal_status` | See vocabulary below |
| `confirmation_status` | Internal engine confirmation state |
| `confidence_evidence` | Structured evidence bag (not a fake probability) |
| `entry_reference` | Price/level/basis used for hypothetical entry research |
| `invalidation_reference` | Level/condition that voids the idea |
| `opportunity_expiry` | Time/bar index after which signal is expired |
| `expected_movement_estimate` | Optional gross proxy fraction/price |
| `estimated_costs` | Fee/spread/slip components or incomplete marker |
| `estimated_net_opportunity` | Gross − costs when both available |
| `rejection_reason` | Human-readable machine key |
| `required_market_characteristics` | Declared needs (regime/vol/momentum) |

### Status vocabulary (explicit differences)

| Status | Meaning |
| --- | --- |
| **Ineligible** | Controller/matrix forbids engine under current intelligence |
| **No signal** | Eligible, but pattern not present |
| **Pending confirmation** | Pattern started; confirmation rules not yet met |
| **Confirmed signal** | Engine rules satisfied at this close |
| **Invalidated signal** | Was active; structure broke invalidation |
| **Expired signal** | Passed `opportunity_expiry` without actionable confirmation |
| **Rejected opportunity** | Signal may exist but economics/quality fail |

These statuses are **orthogonal** to risk approval.

### Relationship to existing `ResearchStrategy`

Current `ResearchStrategy` returns production-shaped `StrategyDecision` for lab engines. The adaptive candidate contract is a **research overlay** for discovery/selection. Future integration may adapt candidates into decisions **without** editing production `strategy.py`. That adapter is a later stage.

---

## 10. Adaptive confidence framework

### Four separated pillars

| Pillar | Question |
| --- | --- |
| 1. Strategy eligibility | Is the engine allowed to speak? |
| 2. Signal confidence | How strong is the **evidence** for this pattern? |
| 3. Opportunity quality | Does proxied move clear costs? |
| 4. Risk approval | Does central risk permit size/entry? |

**Do not** collapse into one “score” or “probability of profit.”

### Signal confidence — evidence dimensions (qualitative / countable)

Measurable evidence examples (no arbitrary %):

- Regime suitability match (confirmed vs preferred)  
- Signal strength metrics (pullback depth vs ATR, sweep size, expansion multiple)  
- Confirmation quality (bars confirming, secondary engine agree/disagree)  
- Market structure integrity (range bounds, EMA alignment)  
- Data quality OK  
- Cost-adjusted opportunity pass/fail  
- Signal persistence (consecutive confirming bars)  

Controller stores an **evidence record** (pass/fail/unknown per dimension + numeric measurements where available).

### Future calibration (not now)

Calibration of evidence weights may only use **independent out-of-sample** research windows under separate approval, never the sealed holdout, never small recent win/loss samples for auto-tuning. Any change to selection rules requires documented validation + explicit approval.

---

## 11. Strategy eligibility matrix (starting hypothesis)

Not immutable. Default research hypothesis:

| Market condition | S1 | S2 | S3 | S4 |
| --- | --- | --- | --- | --- |
| Trending | Primary | No | Conditional | Secondary |
| Ranging | No | Primary | No | Secondary |
| High volatility | Conditional | Conditional | Primary **if expansion confirmed** | Conditional |
| Low volatility | No | No | No | No |
| Transitioning | No | No | No | No |
| Unknown | No | No | No | No |

### Interpretation notes

- **Primary:** preferred proposer when condition holds and other gates pass.  
- **Secondary:** may confirm a primary; may propose alone only under stricter evidence (default: confirmation-only when a Primary exists).  
- **Conditional:** allowed only with extra explicit checks (e.g., S3 needs compression→expansion sequence, not only `HIGH_VOLATILITY`).  
- **High volatility** row overlaps primary regimes; apply **after** primary regime eligibility (intersection of gates).  
- **Low volatility:** all No — avoid forcing trades in compression.

### Compatibility and conflicts

| Rule | Behavior |
| --- | --- |
| Compatible confirmation | Same direction; confirmation engine Secondary/Conditional; does not widen risk |
| Conflict | Opposite directions or incompatible invalidations → NO TRADE |
| Correlation | Same-bar overlapping setup → treat as one opportunity |
| Dual Primary claim | Prefer matrix Primary for current regime; if tie/conflict → NO TRADE |
| No eligible Primary | NO TRADE (do not promote Secondary alone by default) |

---

## 12. Cost-adjusted opportunity handling

1. Each candidate supplies gross proxy (or inherits Layer 3 ATR proxy).  
2. Apply shared fee/spread/slip components.  
3. If any cost component missing → `INCOMPLETE_COST_INFORMATION` → cannot select.  
4. If net < minimum edge (provisional, aligned with 3.3.1 `minimum_net_edge_fraction`) → reject.  
5. Controller records assumptions on every decision.

No second fee model. No silent changes to PaperBroker economics.

---

## 13. Risk and execution separation

| Actor | May do | Must not do |
| --- | --- | --- |
| Adaptive controller | Recommend candidate / NO TRADE | Size positions, bypass risk, alter fees/slip/spread |
| Strategy engines | Emit candidates | Call broker, open positions |
| Existing risk + PaperBroker | Permit/deny, fill paper orders | Be replaced by a parallel risk engine |

Unchanged: risk limits, fees, spread, slippage, position-sizing rules, PaperBroker economics.

---

## 14. Feedback and validation methodology

### Research observations (logging / offline analysis)

- Signal quality and confirmation rates  
- Selection accuracy vs later labelled regimes (research labels only)  
- Missed vs rejected opportunities  
- Switching frequency / cooldown effectiveness  
- Execution cost realization (paper)  
- Cross-engine correlation / overlap  

### Insufficient for automatic rule changes

- Small samples  
- Recent win/loss streaks  
- In-sample fits without holdout-safe protocol  

### Evidence class for changing selection rules

Requires: documented experiment design, separate candle vs tape tracks, sufficient sample policy (lab existing ≥30 closed-trades rankability ethos where applicable), out-of-sample window **excluding sealed holdout**, explicit human approval.

Controller must **not** auto-modify parameters from live feedback loops in this architecture.

---

## 15. Research methodology for engines (evaluation plan — not execution)

Each engine evaluated **independently** before integration, using consistent criteria:

- Signal frequency  
- Confirmation rate  
- MFE / MAE (descriptive, not guarantees)  
- Time to favorable movement  
- Gross vs estimated net after costs  
- Performance stratified by regime / secondary traits  
- Signal overlap with other engines  
- Sensitivity to reasonable provisional parameter changes  

### Dual-track evidence

| Track | Role |
| --- | --- |
| Historical candle research | Structure/regime/signal statistics on synthetic quotes path |
| Quote-tape research | Execution realism / freshness / fill path |

Never merge tracks into a single “truth” metric without explicit methodology.  
**Never use sealed holdout.**

This stage does **not** run these evaluations.

---

## 16. Known limitations

- Planning only; interfaces not implemented.  
- Stage 3.3.1 opportunity proxy is ATR-based, not engine-specific.  
- No unified tape+candle adaptive loop yet.  
- Eligibility matrix is a hypothesis.  
- Long-only spot bias; S2 long-only sweep is asymmetric by design.  
- Confidence framework is evidence-structured, not calibrated.  
- Existing `ResearchStrategy` ≠ candidate contract (adapter needed later).  
- No claim of edge, accuracy, or readiness for live trading.

---

## 17. Future implementation stages (proposal only — not authorization)

1. Shared strategy candidate interfaces & decision-record schema  
2. S4 Momentum Confirmation (simplest new engine; also confirmation role)  
3. S1 Trend Pullback Continuation  
4. S3 Volatility Expansion Breakout  
5. S2 Liquidity Sweep Reversal (hardest structure / false-break logic)  
6. Adaptive Controller (eligibility + NO TRADE + logging)  
7. Strategy coordination (confirmation / conflict / correlation rules)  
8. Integrated synthetic-fixture testing  
9. **Separate approval** — historical candle research (still no holdout)  
10. **Separate approval** — paper-trading integration behind existing risk/broker  

### Why this order

- Interface first prevents four divergent APIs.  
- S4 first: simplest directional logic + dual role (candidate/confirm).  
- S1 next: leverages TRENDING intelligence already in 3.3.1.  
- S3 next: needs compression→expansion state beyond a single vol label.  
- S2 last among engines: highest false-positive structural risk.  
- Controller after ≥1–2 engines exist to avoid vacuum design.  
- Coordination after controller baseline.  
- Synthetic tests before any historical authorization.  
- Historical and paper integration remain gated approvals.

---

## 18. Stop

This specification ends Stage 3.3.2 planning content. Implementation requires a **new explicit authorization**.
