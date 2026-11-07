# Stage 3.1 — Architecture and gap assessment

Paper-only research infrastructure. This document was written **before** implementation. It is not a profitability claim.

## Boundary

- Work only in `F:\crypto-ai-scalper`.
- Never read or write `F:\crypto-ai-trade-v4`.
- Do not modify production trading modules: `runtime.py`, `paper_broker.py`, `strategy.py`, `backtester.py`, `config.py`, `market_data.py`, `historical_replay.py`.
- Do not modify frozen baselines, original quote-tape captures, or Stage 2.3 manifests.
- Isolated Stage 2 replay (`quote_tape_replay.py`, `quote_tape_dataset.py`) stays behaviour-frozen. Lab corrections live under `research-lab/` only.

## What already exists (reuse)

| Component | Role in research | Constraint |
| --- | --- | --- |
| `quote_tape_dataset.py` | Immutable session load, reconnect gaps as empty, no invented bookTicker `E` | Use as-is |
| `quote_tape_replay.py` | Host-order merge of quotes + candles; PaperBroker fills; 3 s freshness | Canonical Stage 2.4 engine |
| `PaperBroker` | Long-only paper fills, fees, slippage, pending exits | Import only |
| `_Strategy.evaluate` / `StrategyDecision` | Frozen production strategy API | Wrap, do not change |
| `BacktestSettings` | Frozen fee 10 bps, slip 5 bps, spread 2 bps | Stress copies only |
| `Backtester` | Synthetic candle execution | Label synthetic; type-locked to production strategies |
| Stage 2.3 session `20261030T191728Z-4751556f` | Validated ~12 h BTCUSDT public tape | Pointers only; SHA-256 manifest |
| Stage 2.4 reports | Execution comparison and 69 stale candle decisions | Evidence, not a tuning target |

## Gaps that affect research validity

1. **Mixed clocks (Stage 2.4).** Candle *decision time* uses kline `E`. Quote freshness is `(E − last_quote.received_at)`. bookTicker has no `E`. Negative ages fail closed. This produced **69 stale candle decisions**. The 3 s broker threshold is not proven wrong; the clocks are incomparable.
2. **Possible look-ahead labelling.** Event order is host `received_at` (correct: the bar is not evaluated before the host has it). Labelling the decision with `E` that can precede host receive mixes “exchange event” with “information available here”.
3. **No research strategy interface.** `make_strategy` / `Backtester` only accept four production classes. New hypotheses cannot be added without touching production or forking Backtester.
4. **No chronological research splits, experiment IDs, or metric pack** for ranking after costs.
5. **Synthetic vs tape.** Candle close ± spread is not executable. Screening may use it only if labelled.
6. **Partial fills** are not modelled. Gaps are not interpolated (must stay that way).
7. **Sample size.** Stage 2.4 had six closed longs. Ranking on that window is invalid.

## Laboratory design

Python package `research_lab` lives under `research-lab/` (hyphenated folder, underscored import). Runtime journals go under `research-lab/experiments/runs/` via `resolve_replay_journal` (never `paper_broker.jsonl`).

```
research-lab/
  research_lab/     importable package
  datasets/         catalog pointers (no tape copies)
  configs/          frozen experiment JSON + hypothesis notes
  experiments/      run outputs
  reports/          Stage 3.1 reports
  tests/
```

### Dataset layer

- Catalog records session id, paths, manifest SHA of `session.json`, validation report path.
- Load through `load_quote_tape_session` only.
- Chronological slices are in-memory views (filtered tuples), not rewritten JSONL.
- Preserve host receive times; never apply kline median offset to quotes.

### Clocks (lab only)

| Policy | Decision timestamp | Freshness | Use |
| --- | --- | --- | --- |
| `kline_event` | kline `E` if present | mixed `E` vs quote receive | Reproduce Stage 2.4 |
| `host_receive` | candle `received_at` | same-clock host times | Lab correction (default for new research) |

Do **not** raise the 3 s threshold to hide mixed-clock staleness.
Do **not** discard stale decisions; count and persist them.

### Strategy layer

`ResearchStrategy` protocol: spec (inputs, warmup, signals, entry/exit, constraints, holding, indicators, parameters) + deterministic `evaluate(...)`.
Production strategies attach via adapter (call existing `evaluate`, no source edits).
Demo strategy implements the protocol independently to prove the engine does not require production classes.

### Execution

Quote path: recorded bid/ask through PaperBroker (existing).
Candle path: existing Backtester, labelled synthetic; production strategies only until a lab synthetic screen exists — Stage 3.1 does not add a second synthetic engine.

### Integrity

Experiment id, dataset identity + manifest reference, frozen config snapshot, chronological train/validation/test, in-sample vs out-of-sample report folders, reproducibility double-run, insufficient-sample flag (default: fewer than 30 closed trades is not rankable).

## Stage 3.1 implementation vs later

**This stage:** catalog, clocks, research replay fork, protocol + demo + production adapter, metrics, splits, stale investigation, tests, reports.

**Not this stage:** hypothesis strategies A–D, parameter search, ranking momentum_breakout, production changes, live data.
