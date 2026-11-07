# Stage 3.2.4 — Implementation report (infrastructure phase)

Paper-only. €0. Authorized Steps 1–5 only. No full historical scoring, walk-forward, optimization, ranking, holdout inspection, aggTrade fill simulator, or live capture control.

This is **not** a profitability result.

## Verdict

Isolated historical research infrastructure is in place and covered by offline tests plus a short-fixture B0/H1 smoke. Production, H1 rules, frozen baseline parameters, quote-tape sessions, and the sealed holdout were not modified or scored.

## Files created

| Path | Role |
| --- | --- |
| `research_lab/datasets/historical_catalog.py` | Vision registration, manifest verify, holdout filter |
| `research_lab/backtesting/historical_replay.py` | `HistoricalCandleReplay` next-candle-open fills |
| `datasets/historical_catalog.json` | Historical catalog (tape `catalog.json` untouched) |
| `configs/stage324_experiment.json` | Frozen Stage 3.2.4 experiment lockfile |
| `tests/test_historical_candle_replay.py` | 17 offline tests |
| `run_stage324_smoke.py` | Short-fixture B0/H1 smoke runner |
| `reports/stage324_smoke_results.json` | Smoke output |
| `reports/STAGE3.2.4_IMPLEMENTATION_REPORT.md` | This report |

## Files modified (research-lab only)

| Path | Change |
| --- | --- |
| `research_lab/historical/constants.py` | `RESEARCH_CUTOFF`, `HISTORICAL_DATASET_ID` |
| `research_lab/datasets/__init__.py` | Export historical bind helpers |
| `research_lab/backtesting/__init__.py` | Export `HistoricalCandleReplay` |

No production modules, H1 implementation, baseline parameters, or quote-tape session files were changed.

## Exact execution timing

Convention: `next_available_candle_open`

1. After candle **N** closes, the strategy may observe N (and earlier) only.
2. Observation synthetic quote mid = **N close** (labelled synthetic; not bookTicker).
3. Entry/exit decisions are **deferred** — no fill at the already-observed close.
4. Earliest fill: candle **N+1 open** as mid, then frozen spread + slippage via PaperBroker.
5. Missing N+1 or gap → cancel pending (no fabrication).
6. If N+1 open ask falls outside signal stop/target → cancel pending (stops not rewritten).
7. Decision/position timestamps aligned to fill instant for PaperBroker validation only.

Documented in run `assumptions` / `execution_notes` and `configs/stage324_experiment.json`.

## Holdout boundary verification

Cutoff: `2026-10-31T04:20:59.999000+00:00`

- `load_klines` / bind filters `close_time >= cutoff` (and open_time).
- `assert_before_cutoff` raises on equality and later.
- Replay refuses any loaded candle at/after cutoff.
- AggTrade helper `filter_aggtrade_timestamp` uses the same exclusive rule.
- Tests cover exact boundary equality.

Sealed holdout was not scored or inspected for performance.

## Isolation verification

- Journals must resolve under `research-lab/experiments/runs/` (or temp `.../experiments/runs/` in tests).
- `assert_isolated_path` / `resolve_replay_journal` refuse live `paper_broker.jsonl`.
- Historical catalog is separate from quote-tape `datasets/catalog.json`.
- Raw Vision archives remain immutable; binder only reads.

## Test results

| Suite | Result |
| --- | --- |
| `python -m unittest discover -s research-lab/tests` | **58 OK** (prior 41 + 17 new) |
| New module | `tests.test_historical_candle_replay` **17 OK** |
| Smoke | `python research-lab/run_stage324_smoke.py` → exit 0 |

Smoke checks (infrastructure only):

- Identical observation timestamps/bases for B0 and H1
- Same execution model
- Deterministic double-run fingerprints for each arm
- Manifest SHA locked into config
- Synthetic quotes only

## Frozen configuration

`research-lab/configs/stage324_experiment.json`

- Research window start / exclusive cutoff
- Provenance `BINANCE_SPOT_HISTORICAL`
- Manifest path + SHA-256 (`73ba52180fdc77fbd51e04ce881dcd44b623924ea478b99c9b563e99d3d91be8`)
- Arms **B0** + **H1** only (C1/C2/M1/A1 explicitly excluded pending approval)
- Frozen costs/risk, next-open execution, missing-data policy
- `ranking_allowed_this_phase: false`
- `full_historical_scoring_authorized: false`

## Remaining blockers (for later authorization)

1. Full two-year B0/H1 scoring not authorized.
2. Walk-forward comparison not authorized.
3. C1/C2/M1/A1 definitions not approved.
4. AggTrade fill simulator not authorized.
5. Sealed holdout remains permanently out of discovery/scoring.
6. Tape vs candle tracks remain dual-track (do not blend rankings).

## Stop condition

Steps 1–5 complete. No full historical scoring or optimization was started.
