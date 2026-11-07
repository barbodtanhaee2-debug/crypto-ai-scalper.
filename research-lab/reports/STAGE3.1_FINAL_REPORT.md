# Stage 3.1 — Isolated scalping strategy research laboratory

Paper-only research infrastructure. This is **not** a profitability result. Production trading, frozen baselines, and original capture files were not modified. `F:\crypto-ai-trade-v4` was not accessed.

## Dataset identity

| Field | Value |
| --- | --- |
| Catalog id | `stage23_extended_btcusdt_20261030T191728Z-4751556f` |
| Session | `20261030T191728Z-4751556f` |
| Status / ended_at | `stopped` / `2026-10-31T07:22:59.755340+00:00` (from Stage 2.3 bind) |
| Quotes / candles | 2,787,240 / 725 |
| Manifest | `quote-tape/reports/STAGE2.3_EXTENDED_SHA256_MANIFEST.csv` |
| session.json SHA-256 | `c3995cb3483d53b648054fee495c8323a71ad81bbe5791614a29a8c8f8f1cf70` |
| Validation report | `quote-tape/reports/STAGE2.3_EXTENDED_VALIDATION.md` |
| Missing 1m intervals | none detected (>90 s between closes) |
| Reconnect gaps | 1 (preserved, not interpolated) |

Original JSONL was not copied into `research-lab/datasets/`. Catalog stores pointers only. Bind refuses a mutated `session.json` hash.

## Implemented vs proposed

**Implemented in Stage 3.1**

- Isolated `research-lab/` package `research_lab`
- Immutable dataset catalog and chronological train/validation/test splits
- Research strategy protocol, production adapter, demonstration strategy
- Lab quote replay with pluggable clocks (`kline_event`, `host_receive`)
- After-cost metrics with an insufficient-sample gate (default 30 closed trades)
- Stale-decision investigation and lab-only host-clock correction
- Isolated experiment runner (ids, config snapshot, journals under `experiments/runs/`)
- Unit tests plus inclusion in the project suite

**Not implemented (proposed later)**

- Hypothesis strategies A–D (momentum variants, mean reversion, microstructure, volatility-aware)
- Parameter search or ranking of `momentum_breakout`
- A second synthetic candle engine for research-only strategies (`Backtester` still type-locks production classes)
- Partial fills (existing PaperBroker does not model them)
- Additional public tape beyond Stage 2.3

## Architecture

See `STAGE3.1_ARCHITECTURE.md`. Production modules are imported, never edited. Stage 2 `QuoteTapeReplay` remains the frozen comparison engine. The lab fork only adds a decision-clock policy.

## File and module inventory

| Path | Role |
| --- | --- |
| `research-lab/README.md` | Operator overview |
| `research-lab/reports/STAGE3.1_ARCHITECTURE.md` | Pre-implementation gap assessment |
| `research-lab/reports/STAGE3.1_STALE_DECISIONS.md` | Stale-clock root cause |
| `research-lab/reports/stale_decision_analysis.json` | Per-bar stale classifications |
| `research-lab/reports/stage31_validation.json` | Bind + replay comparison |
| `research-lab/datasets/catalog.json` | Immutable session pointers |
| `research-lab/configs/default_experiment.json` | Frozen cost/split defaults |
| `research-lab/configs/research_hypotheses.md` | Future hypotheses, not code |
| `research-lab/research_lab/datasets/catalog.py` | Bind, hash check, in-memory slices |
| `research-lab/research_lab/backtesting/clocks.py` | Clock policies |
| `research-lab/research_lab/backtesting/engine.py` | `ResearchQuoteReplay` |
| `research-lab/research_lab/backtesting/splits.py` | Chronological splits |
| `research-lab/research_lab/strategies/protocol.py` | `StrategySpec` / protocol |
| `research-lab/research_lab/strategies/demo.py` | `demo_close_follow` (demo only) |
| `research-lab/research_lab/strategies/adapters.py` | Production wrap |
| `research-lab/research_lab/metrics/evaluate.py` | After-cost metrics |
| `research-lab/research_lab/experiments/stale_investigation.py` | Clock audit |
| `research-lab/research_lab/experiments/runner.py` | Isolated runs |
| `research-lab/research_lab/isolation.py` | Capture/live-journal/v4 guards |
| `research-lab/tests/` | Laboratory unit tests |
| `research-lab/run_stage3_1.py` | Validation driver |

## Stale-decision investigation

Stage 2.4 counted **69** stale candle decisions. The lab investigation reproduces **69** on the `kline_event` policy.

**Cause:** freshness is `(kline E − last quote host received_at)`. bookTicker has no exchange `E`. `fresh` requires `0 ≤ age ≤ 3`.

| Class | Count |
| --- | ---: |
| Negative age (E before last quote host time) | **68** |
| Age greater than 3 s on kline E | **1** |
| Stale on E but fresh on host receive | **68** |
| Still stale on host receive | **1** |
| Candles with E before host receive (all candles) | 138 |

Median kline `E − received_at` is **+0.053 s**. That offset was **not** applied to quotes.

This is mixed-clock replay synchronization, not evidence that quotes were idle for 3 s, and **not** a reason to widen production `max_market_data_age_seconds`.

**Lab correction:** default research clock `host_receive` (candle availability and quote age on the host clock). Stale decisions are still counted. Event order remains host receive. Residual **1** host-clock stale bar is treated as a true gap/delay (reconnect window class).

`ResearchQuoteReplay` with `kline_event` matched frozen `QuoteTapeReplay`: 69 stale, 6 trades, equity 98.169.

`host_receive` on the same frozen `momentum_breakout` parameters is a **different executable path** (3 trades). Previously fail-closed mixed-clock bars can now be healthy, so the single long-only slot is occupied at different times. That is a clock-policy effect, not parameter tuning. Do not rank either path.

## Demonstration strategy

`demo_close_follow` implements the research protocol without subclassing production `_Strategy`. On this tape it completed 18 trades, all after costs, **insufficient_sample=true** (need 30 to rank). It is a wiring proof, not a candidate.

## Metrics and integrity

Every experiment reports net P&L, net return, trades, win rate, averages, profit factor, expectancy, drawdown, holding time, fees, spread, slippage, rejects, unresolved exits, exposure, stale counts. Ranking is disabled below 30 closed trades. Primary score is risk-adjusted after costs, not gross profit or win rate.

Splits are chronological by candle close. Experiment ids freeze dataset hash, clock, and `BacktestSettings` costs (fee 10 bps, slip 5 bps, synthetic spread 2 bps). Random seed is reserved; strategies are deterministic.

## Tests

| Suite | Result |
| --- | --- |
| `python -m unittest discover -s research-lab/tests` | 12 OK |
| `python -m unittest discover -s tests` | **249 OK** (previous 237 plus 12 lab tests) |

Covered: dataset load/slice/missing intervals, isolation, event-time clocks, mixed-clock stale classification, lab vs frozen kline_event parity, look-ahead (no future bars), missing quotes not fabricated, fees on quote fills, two-run reproducibility, insufficient-sample flag.

Existing production tests continued to pass. No production code was changed to make them pass.

## Known limitations

- One ~12 h public session; n is too small to claim an edge.
- Host vs kline clocks remain uncalibrated for bookTicker (no quote `E`).
- Changing decision clock changes which frozen-strategy trades exist.
- Synthetic candle screening for *new* research strategies is not in this stage.
- No partial fills; no L2 book (bookTicker sizes only).
- Demo strategy is intentionally naive.
- Mark-to-market path for lab metrics uses closed-trade nets plus final equity, not the Stage 2.4 journal mark series (acceptable for this infrastructure stage; refine in 3.2 if ranking starts).

## Proposed Stage 3.2 research plan

1. Freeze `host_receive` as the research default; keep `kline_event` only for Stage 2.4 reproduction.
2. Add **one** hypothesis strategy at a time (start with volume-tagged momentum **or** cost-aware mean reversion), with a frozen config and chronological splits.
3. Require `ranking_allowed` (sample gate) before any comparison; this window will not qualify until more tape exists.
4. Collect additional public quote-tape sessions under the Stage 2.3 collector rules.
5. Do not tune `momentum_breakout`. Do not optimize against the Stage 2.3 test hours repeatedly.
6. Candle-only screens, if used, must stay labelled synthetic.

## Conclusion

Stage 3.1 delivered an isolated laboratory that can evaluate new strategies on recorded bid/ask data without touching production behaviour. The 69 Stage 2.4 stale decisions are explained: **68 mixed-clock negative ages**, **1** genuine &gt;3 s host gap. The 3 s threshold was not increased.

The laboratory is ready for careful Stage 3.2 hypothesis tests. It is **not** ready to declare any strategy profitable.
