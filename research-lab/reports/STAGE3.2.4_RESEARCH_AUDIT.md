# Stage 3.2.4 — Research audit (design only)

Paper-only. €0. No strategy implementation, optimization, holdout scoring, production edits, or new live capture in this stage slice.

This audit precedes any historical comparison code. It is not a profitability claim.

## 1. Sources inspected

| Source | Role |
| --- | --- |
| `data/binance_spot/BTCUSDT/reports/STAGE3.2.3_ACQUISITION_REPORT.md` | Historical acquisition verdict |
| `manifests/dataset_manifest_20261104T184815Z.json` + `INDEPENDENT_VERIFICATION.json` | Provenance, hashes, counts |
| `reports/STAGE3.1_ARCHITECTURE.md` | Lab design and gaps |
| `reports/STAGE3.2.1_H1_REPORT.md` | H1 inconclusive result |
| `reports/STAGE3.2.2_CAPTURE_READINESS.md` | Capture / holdout rules |
| `research_lab/backtesting/*`, `metrics/evaluate.py`, `strategies/*`, `experiments/h1_*` | Reusable lab surfaces |
| `configs/h1_next_bar_close.json`, `default_experiment.json`, `research_hypotheses.md` | Frozen experiment assumptions |
| `datasets/catalog.json` | Registered tape datasets only |

Production `strategy.py`, `backtester.py`, `paper_broker.py`, `quote_tape_replay.py`, and `historical_replay.py` were inspected for **import/contract** context only and must remain unmodified.

## 2. Available datasets and coverage

### 2.1 Binance Vision historical (Stage 3.2.3)

| Field | Value |
| --- | --- |
| Root | `research-lab/data/binance_spot/BTCUSDT` |
| Provenance | `BINANCE_SPOT_HISTORICAL` |
| Source | `https://data.binance.vision/` (public, no keys) |
| Symbol / market | BTCUSDT Spot |
| 1m klines | `[2024-11-04T00:00:00Z, 2026-11-04T00:00:00Z)` → **1,051,200** rows, **0** missing minutes |
| aggTrades | `[2026-08-04T00:00:00Z, 2026-11-04T00:00:00Z)` → **77,077,494** rows |
| Archives | 86/86 downloaded; independent SHA-256 re-verify OK |
| Timestamp units | Klines ms→µs at 2025-01-01; aggTrades all µs |
| Order book | **Not present.** Do not treat as depth snapshots. |

### 2.2 Live quote-tape (Stage 2.3 / H1)

| Field | Value |
| --- | --- |
| dataset_id | `stage23_extended_btcusdt_20261030T191728Z-4751556f` |
| Provenance (logical) | `BINANCE_SPOT_LIVE_CAPTURE` |
| Content | bookTicker + 1m klines (~12 h) |
| Catalog | Only entry in `research-lab/datasets/catalog.json` |
| H1 status | **inconclusive** (<30 scored trades/arm) |

### 2.3 Sealed holdout overlap (critical)

Sealed holdout begins at:

`2026-10-31T04:20:59.999Z`

The historical kline and aggTrade windows **overlap** this cutoff. For Stage 3.2.4 research experiments:

- Exclude every historical candle with `close_time >= 2026-10-31T04:20:59.999Z`.
- Exclude every aggTrade with `timestamp_utc >=` that same instant.
- Do **not** inspect, score, tune, or report performance on the excluded slice.
- Do **not** alter existing holdout files or tape session metadata.

Effective historical research ceiling (exclusive): **2026-10-31T04:20:59.999Z**.

Approx. usable 1m bars before holdout: from 2024-11-04 through ~2026-10-31 04:19 → on the order of **~1.05M − ~5.5k ≈ 1.045M** minutes (exact count deferred to implementation with a unit-tested filter).

Usable aggTrades: 2026-08-04 → holdout start only (~2.9 months).

## 3. Existing reusable components

| Component | Path | Reuse rule |
| --- | --- | --- |
| Isolation | `research_lab/isolation.py` | Keep; refuse v4 / live journals / capture writes |
| Research strategy protocol | `strategies/protocol.py` | Implement new candidates behind this only |
| Production adapter | `strategies/adapters.py` | Wrap frozen production strategies; **do not edit** production params |
| Registry | `strategies/registry.py` | Extend carefully for new lab names; leave H1 names intact |
| H1 confirmation | `strategies/confirmed_momentum.py` | **Do not modify rules** |
| Quote replay fork | `backtesting/engine.py` `ResearchQuoteReplay` | Tape path only; keep separate from candle historical path |
| Clocks | `backtesting/clocks.py` | Prefer `host_receive` for tape; historical needs its own labelled clock |
| Splits | `backtesting/splits.py` | Chronological only; generalize for candle streams |
| Holdout helpers | `backtesting/holdout.py` | Pattern for exclusion + scored_trades; extend for historical timestamps |
| Metrics | `metrics/evaluate.py` | After-cost metrics; `MIN_CLOSED_TRADES_TO_RANK = 30` |
| H1 runner / overlay | `experiments/h1_runner.py`, `h1_comparison.py` | Reference for fair dual-arm protocol; do not change H1 |
| Historical I/O | `historical/*` | Load verified archives; derive intervals; manifests |
| PaperBroker / BacktestSettings | production imports | Fee 10 bps, slip 5 bps, spread 2 bps (synthetic), risk 0.5%, daily loss 2%, max DD 10%, equity 100 |

Frozen baseline production strategies (via adapter / `baseline.py` contracts, parameters unchanged):

- `MomentumBreakoutStrategy` (lookback 20, risk 0.5%) — **Stage 3.2.4 baseline**
- Also frozen elsewhere: range mean-reversion, MA crossover, volatility trend — available as **pre-registered** alternatives only with production defaults (no retuning)

## 4. Missing capabilities (block historical comparison today)

1. **No candle-stream historical replay adapter** under `research-lab/` that consumes Vision 1m CSVs chronologically with labelled synthetic execution.
2. **Historical dataset not catalogued** — `catalog.json` only knows the quote-tape session; no `BINANCE_SPOT_HISTORICAL` binder with holdout filter.
3. **No unified experiment harness** for multi-candidate comparison on the same historical window with identical costs/splits.
4. **No walk-forward runner** for multi-year chronological folds.
5. **AggTrade fill model absent** — and must never be mislabelled as L2 depth.
6. **Derived intervals** (`derive.py`) exist but are not batch-materialized; 1m remains canonical.
7. **Tape vs candle execution parity is not defined** — H1 tape results cannot be numerically merged with synthetic-candle historical results without an explicit dual-track report.

## 5. Proposed architecture (research-lab only)

```
research-lab/
  research_lab/
    historical/          # existing acquisition (immutable raw)
    datasets/
      historical_catalog.py   # NEW: bind Vision dataset + holdout cut
    backtesting/
      historical_replay.py    # NEW: candle chronological replay (lab-only)
      holdout.py              # extend filters; do not weaken tape helpers
      splits.py               # time-based splits for long series
    experiments/
      stage324_runner.py      # NEW later: fair multi-arm comparison
  configs/
    stage324_experiment.json  # NEW: frozen windows, costs, candidate list
  reports/
    STAGE3.2.4_*.md
  tests/
    test_historical_replay.py # NEW later
```

Principles:

- Separate **tape replay** (`ResearchQuoteReplay`) from **historical candle replay**.
- Decisions use only bars with `close_time <= decision_time`.
- Missing minutes: skip / cancel pending logic; never interpolate.
- Synthetic bid/ask from mid ± `spread_fraction` must carry reason label `synthetic_historical_candle_quote`.
- Journals under `research-lab/experiments/runs/` only.
- Provenance tags on every run: `BINANCE_SPOT_HISTORICAL` vs `BINANCE_SPOT_LIVE_CAPTURE`.

## 6. Data integrity concerns

| Concern | Status / mitigation |
| --- | --- |
| Checksums / counts | Stage 3.2.3 verified; re-check on bind |
| ms/µs mix | Normalized UTC in processed CSVs; preserve raw |
| Headerless schema | Confirmed; parsers already skip headers if present |
| Holdout leakage | Hard filter at load + assert in runner |
| Mixing tape + historical | Forbidden without dual-track labelling |
| Fabricating quotes | Only labelled synthetic mid±spread for candle path |
| AggTrades as depth | Forbidden; optional trade-print VWAP/impact studies only |
| Incomplete day | Acquisition already exclusive of incomplete fabrication |

## 7. Look-ahead bias risks

| Risk | Required control |
| --- | --- |
| Using bar N+1 features at bar N close | Engine only appends a bar after its close; evaluate on prefix |
| H1 “confirmation” using same-bar close twice | Existing H1 waits for next minute; do not change |
| Parameter search on full sample then reporting OOS | Pre-register candidates; no grid search in 3.2.4 |
| Holdout peeking | Exclude ≥ holdout start from all discovery/validation scoring |
| Walk-forward using future fold stats to pick winners mid-stream | Freeze candidate set before first fold; report fold metrics separately |
| AggTrade path using prints after decision | Decision time must precede any print used for that fill |
| Derived higher TFs built with future 1m bars | Aggregate only completed 1m bars inside the interval |

## 8. Execution-cost modelling requirements

Reuse frozen `BacktestSettings` / H1 config (do not invent cheaper costs):

| Assumption | Value | Label |
| --- | --- | --- |
| Initial equity | 100 USDT | paper |
| Fee per side | 0.001 (10 bps) | assumed Spot taker-like |
| Slippage per side | 0.0005 (5 bps) | assumed |
| Synthetic spread | 0.0002 (2 bps) | candle path only |
| Risk fraction | 0.005 | frozen |
| Daily loss limit | 0.02 | frozen |
| Max drawdown | 0.10 | frozen |
| Long-only | true | Spot paper |
| Ranking gate | ≥30 closed scored trades | `experiment_metrics` |

Reports must state: costs are **assumptions**, not measured venue fees; candle fills are **not** recorded bookTicker fills; historical results are **not** proof of future profit.

## 9. Existing evaluation metrics and frozen baselines

Primary metrics (`experiment_metrics`): after-cost net P&L, net return %, completed trades, expectancy/trade, profit factor, max drawdown, fees/spread/slippage totals, stale counts (tape), `ranking_allowed` / `insufficient_sample`.

**Do not rank on gross profit or win rate alone.**

Frozen economic baseline for comparison:

1. Production `momentum_breakout` via `ProductionStrategyAdapter` / `DecisionLoggingAdapter` with **unchanged** parameters.
2. H1 `h1_confirmed_momentum_breakout` with **unchanged** confirmation rules (reference arm; not retuned).

Stage 3.2.1 tape result remains inconclusive and must not be rewritten as historical evidence.

## 10. Recommended experiment design (summary)

See companion `STAGE3.2.4_EXPERIMENT_DESIGN.md` for full plan.

Headline:

- Pre-register a **small** candidate set (baseline, H1, 1–2 confirmation variants, 1 momentum variant, 1 MA variant with frozen production defaults).
- Single shared research window ending at sealed holdout start.
- Chronological walk-forward on train/validation folds **before** holdout.
- Identical costs, equity, risk, data availability rules.
- Separate reporting tracks: **historical synthetic-candle** vs **tape bookTicker** (no blended ranking).
- No sealed-holdout scoring; no production promotion.

## 11. Explicit blockers (before implementation / runs)

1. Historical candle replay adapter not built yet (this audit forbids implementing strategies until design docs are reviewed).
2. Historical catalog + holdout cut binder missing.
3. Fair multi-arm Stage 3.2.4 runner missing.
4. Cannot claim execution parity between Vision candles and Stage 2.3 bookTicker without a labelled dual-track protocol.
5. AggTrade-enhanced fills are optional and blocked until a no-look-ahead fill spec is approved.
6. H1 remains inconclusive on tape; historical sample may clear the 30-trade gate — still not proof of edge.
7. Live continuous capture (if still running) is out of scope; do not stop/start it for this stage slice.
8. Budget / safety: no paid APIs, no auth, no live orders, no v4 access.

## 12. Stop condition for this task

Audit and experiment-design documents are the deliverables. Strategy code, optimization, H1 edits, baseline edits, holdout scoring, and production changes are **out of scope** until a later authorized implementation phase.
