# Stage 3.2.4 — Experiment design & validation plan

Paper-only. €0. Companion to `STAGE3.2.4_RESEARCH_AUDIT.md`.

**This document does not authorize strategy optimization or holdout scoring.**

Historical results must never be presented as proof of future profitability.

---

## 1. Research question

On a multi-year BTCUSDT Spot 1m Vision dataset (holdout-excluded), under **identical** costs, risk, and data rules:

1. How does frozen `momentum_breakout` behave after costs?
2. Does frozen H1 next-bar close confirmation change trade rate / after-cost metrics relative to that baseline (descriptive, gated)?
3. Do a **pre-registered**, small set of alternative confirmation / momentum / MA hypotheses clear the sample-size gate without parameter search?

Ranking is allowed only when `completed_scored_trades >= 30` **and** the candidate was pre-registered. Insufficient samples → `inconclusive`, never “winner”.

---

## 2. PHASE 2 — Historical replay design (lab-only)

### 2.1 New adapter (proposed; not implemented in this task)

Name (proposed): `research_lab.backtesting.historical_replay.HistoricalCandleReplay`

| Requirement | Spec |
| --- | --- |
| Input | Processed 1m CSVs under `data/binance_spot/BTCUSDT/processed/klines_1m/` |
| Optional aux | aggTrades streams for **labelled** microstructure studies only |
| Order | Strict ascending `open_time_utc` / `close_time_utc` |
| Decision time | Candle `close_time_utc` (bar available only after close) |
| Missing intervals | Detect gaps; do not invent bars; pending confirmations cancel if next minute absent (H1 rule reuse, unchanged) |
| Look-ahead | Evaluate on bars with `close_time <= decision_time` only |
| Determinism | Same inputs → identical journals/trades (double-run fingerprint) |
| Fills | PaperBroker via **synthetic** bid/ask: mid = close (or configurable), spread = `spread_fraction`; reason labelled synthetic |
| Costs | Frozen `BacktestSettings` defaults (fee 10 bps, slip 5 bps, spread 2 bps) |
| Risk | risk 0.5%, daily loss 2%, max DD 10%, long-only, equity 100 |
| Journals | `research-lab/experiments/runs/stage324/...` only |
| Separation | Must **not** modify `quote_tape_replay.py`, `historical_replay.py` (production), or Stage 2 engines |

**Assumption register** (must be written into every run artifact):

- No historical L2 / bookTicker in Vision klines.
- Synthetic quotes are not executable market truth.
- AggTrades are prints, not depth.
- Latency beyond assumed slip is unmodelled.
- Partial fills unmodelled (same as Stage 3.1).

### 2.2 Holdout exclusion at the data layer

```text
RESEARCH_END_EXCLUSIVE = 2026-10-31T04:20:59.999Z
```

Any loader used by Stage 3.2.4 experiments must drop:

- klines with `close_time >= RESEARCH_END_EXCLUSIVE`
- aggTrades with `timestamp_utc >= RESEARCH_END_EXCLUSIVE`

Assert after load: `max(close_time) < RESEARCH_END_EXCLUSIVE`.

Do not alter sealed tape holdout metadata or session files.

### 2.3 Provenance

| Tag | Use |
| --- | --- |
| `BINANCE_SPOT_HISTORICAL` | Vision candle / aggTrade research |
| `BINANCE_SPOT_LIVE_CAPTURE` | Quote-tape sessions only |

Never merge automatically. Dual-track reports only.

---

## 3. PHASE 3 — Fair comparison design

### 3.1 Shared experiment constants (frozen)

Copied from `configs/h1_next_bar_close.json` / `default_experiment.json` — **do not silently cheapen**:

| Constant | Value |
| --- | --- |
| symbol | BTCUSDT |
| long_only | true |
| initial_equity | 100 |
| fee_rate_per_side | 0.001 |
| slippage_rate_per_side | 0.0005 |
| spread_fraction (candle path) | 0.0002 |
| risk_fraction | 0.005 |
| daily_loss_limit_fraction | 0.02 |
| max_drawdown_fraction | 0.10 |
| min_closed_trades_to_rank | 30 |
| candle_interval_seconds | 60 |

### 3.2 Pre-registered candidates (no search)

| ID | Arm | Source | Notes |
| --- | --- | --- | --- |
| B0 | Baseline momentum breakout | Production `MomentumBreakoutStrategy` via adapter | Frozen params; **do not invent a new baseline** |
| H1 | Next-bar close confirmation | Existing `ConfirmedMomentumBreakoutStrategy` | **Rules frozen**; lab reference |
| C1 | Alt confirmation A | Lab-only wrapper (future) | Pre-register **one** rule before coding (e.g. require t+1 close **and** t+1 low ≥ signal prior high − buffer). Spec locked in config before first run |
| C2 | Alt confirmation B | Lab-only wrapper (future) | Optional second rule only if still ≤ total budget of variants; otherwise skip |
| M1 | Momentum volume filter | Lab wrapper around frozen breakout | Optional binary volume filter with **one** pre-set threshold from a non-tuned heuristic (e.g. volume ≥ SMA(20) of volume); threshold frozen before first fold |
| A1 | MA crossover | Production `MovingAverageCrossoverStrategy` via adapter | **Production defaults only**; no grid |

Hard cap: **≤6 arms** including B0 and H1. Prefer ≤5 if C2 is unnecessary.

Forbidden in Stage 3.2.4 implementation phase:

- Grid / random / Bayesian search over lookbacks, buffers, MA lengths
- Selecting winners using the sealed holdout
- Editing H1 or production strategy modules
- Promoting any arm to production

### 3.3 Evaluation periods (historical track)

All arms share the same calendar cuts (UTC):

| Period | Definition |
| --- | --- |
| Research start | `2024-11-04T00:00:00Z` |
| Research end exclusive | `2026-10-31T04:20:59.999Z` (sealed holdout start) |
| Sealed holdout | `≥ 2026-10-31T04:20:59.999Z` — **excluded entirely** |
| Warmup | Max strategy warmup bars from research start (decisions suppressed until warmup complete); identical across arms that share lookback family; for MA vs momentum, each uses its own frozen warmup but **shared calendar** availability |

### 3.4 Walk-forward methodology

**Expanding or rolling chronological folds** on `[research_start, research_end)`:

Proposed default (locked before coding):

1. Split research window into **N=4** contiguous chronological folds by candle count (not by shuffling).
2. For fold `k=1..N-1`:
   - **Train** = folds `1..k` (used only for descriptive diagnostics / optional non-parametric checks — **not** for parameter fitting in 3.2.4 because params are frozen).
   - **Validation** = fold `k+1` (scored after-cost metrics).
3. Aggregate validation metrics across folds with equal fold weight **and** report pooled trades with `insufficient_sample` if pooled scored closed trades < 30.
4. Because parameters are pre-frozen, “train” is primarily for **stability charts** and leakage tests, not fitting. If a future stage allows fitting, fitting may use train only; validation remains untouched; holdout remains sealed.

Alternative (also acceptable if documented before run): single chronological 50/25 split of the research window for train/validation, with the final 25% of the **research** window as an **internal** lab validation slice — still ending before sealed holdout. Prefer walk-forward if compute allows.

### 3.5 Identical data availability rules

- Same 1m series for every arm.
- Same gap handling (no fill).
- Same synthetic quote construction.
- If an arm optionally uses volume, volume comes from the same kline row available at that bar’s close — not from future bars.
- AggTrade-enhanced arms (if ever approved) must still be runnable in a **candle-only** ablation for fair comparison against B0/H1.

### 3.6 What “fair” means for H1 vs baseline on candles

On tape, H1 used recorded bid/ask + `host_receive`. On Vision candles, both B0 and H1 use synthetic quotes. Therefore:

- Historical B0 vs H1 comparison is valid **within** the synthetic-candle track.
- Do **not** claim historical H1 confirms or refutes Stage 3.2.1 tape H1 economically.
- Stage 3.2.1 remains the tape-track reference (inconclusive).

---

## 4. PHASE 4 — Validation plan

### 4.1 Metrics (after costs)

Reuse `experiment_metrics`:

- net P&L, net return %, completed trades
- expectancy/trade, profit factor, max drawdown
- fees, estimated spread, estimated slippage
- rejected entries, unresolved exits
- `ranking_allowed` iff scored closed trades ≥ 30

Primary scientific outputs: trade count, expectancy, drawdown, ranking gate — not win rate vanity.

### 4.2 Sample-size / trade-count requirements

| Gate | Rule |
| --- | --- |
| Economic ranking | ≥30 completed **scored** trades per arm on the reporting window |
| Fold reporting | Publish fold metrics even if <30; mark `insufficient_sample` |
| Opportunity overlay (H1-style) | Count baseline signals vs confirmed/failed/missing next bar; separate from occupancy-matched fills |

### 4.3 Reject a candidate when

- Look-ahead or holdout leakage detected in tests or asserts
- Non-deterministic double-run fingerprints
- Uses non-frozen parameters or edits production/H1 code
- Relies on fabricated candles or unlabelled synthetic fills presented as tape
- Fails isolation (writes outside research-lab / touches v4 / live journals)
- Claims profitability or production readiness

### 4.4 Conditions for independent forward validation (later stage)

Only if **all** hold:

1. Ranking gate passed on pre-registered validation folds.
2. Reproducibility fingerprints OK.
3. Costs/assumptions unchanged and disclosed.
4. New **out-of-sample** data collected **after** candidate freeze (e.g. future public tape), not the sealed holdout.
5. Explicit human authorization for a new stage.
6. Still no live orders / no production promotion without a separate decision.

Sealed holdout is **not** that forward set.

### 4.5 Reproducibility checks

- Two identical runs per arm → equal trades + journal fingerprint (drop wall-clock init events as in H1).
- Config snapshot JSON copied into run directory.
- Dataset manifest SHA + holdout cut recorded in run metadata.

---

## 5. Proposed implementation plan (after design approval)

Ordered; each step stays inside `research-lab/`.

| Step | Work | Exit criterion |
| --- | --- | --- |
| 0 | Design review (this doc + audit) | Human OK to implement adapters/tests only |
| 1 | `historical_catalog` bind + holdout filter | Unit tests: no candle ≥ holdout; provenance tag |
| 2 | `HistoricalCandleReplay` skeleton | Chronological, deterministic, synthetic labels |
| 3 | Offline tests (Phase 5 list) | All new tests green; existing lab suite green |
| 4 | Wire B0 + H1 on a **short fixture** CSV | Overlay + metrics smoke (not full 2y yet) |
| 5 | Config `stage324_experiment.json` locking arms/windows/costs | No free parameters left unspecified |
| 6 | Full research-window smoke for B0 only | Manifested run artifact; sample gate reported |
| 7 | Add remaining pre-registered arms | Fair table; dual-track notes |
| 8 | Walk-forward runner + report `STAGE3.2.4_COMPARISON_REPORT.md` | Explicit inconclusive/rank flags |

**Not in this authorization:** steps 6–8 full scoring at scale, optimization, holdout scoring, H1 edits, live capture control.

---

## 6. PHASE 5 — Test and validation requirements

New isolated tests under `research-lab/tests/` (no Binance network):

| Test theme | Assertions |
| --- | --- |
| Chronological replay | Bars processed in UTC close order |
| Timestamp handling | ms/µs fixtures normalize; aware UTC required |
| No look-ahead | Decision at t cannot see bar with close > t; mutating future fixture must not change past decisions |
| Deterministic replay | Two runs → identical trade list / fingerprint |
| Missing data | Gap skips; no interpolated OHLC; H1 pending cancels on missing next minute |
| Fee / slippage | Known fill → fee and slip match `BacktestSettings` formulas |
| Trade accounting | Long-only; shorts rejected; net = gross − fees − slip − spread estimates |
| Holdout exclusion | Loader/filter drops ≥ `2026-10-31T04:20:59.999Z`; scoring assert raises on leak |
| Reproducible experiment | Config snapshot + metrics stable on fixture |

Do **not** weaken existing H1, engine, catalog, or historical acquisition tests.

---

## 7. Explicit blockers

| ID | Blocker | Unblocks |
| --- | --- | --- |
| B1 | No historical candle replay adapter yet | Step 2 implementation |
| B2 | Historical dataset not in lab catalog with holdout cut | Step 1 |
| B3 | Design approval pending for C1/C2/M1 exact rule text | Config lock before coding those arms |
| B4 | Tape vs candle tracks incomparable for blended ranking | Dual-track reporting discipline |
| B5 | AggTrade fill model unspecified | Keep candle-only until approved micro-spec |
| B6 | Authorization required before full multi-year scoring runs | Human go-ahead after this review |
| B7 | Safety: no production/H1/holdout/live-capture changes | Remains permanent for this stage |

---

## 8. Deliverables of this design task

1. `STAGE3.2.4_RESEARCH_AUDIT.md` — done  
2. `STAGE3.2.4_EXPERIMENT_DESIGN.md` — this file  
3. Proposed implementation plan — §5  
4. Test and validation requirements — §4–§6  
5. Blockers — §7  

**Stopped for review.** No strategy implementation, optimization, H1 modification, baseline modification, sealed-holdout scoring, production edits, or new live capture was performed.
