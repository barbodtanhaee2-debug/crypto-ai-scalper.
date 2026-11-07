# Stage 3.2.4.2 — Historical replay reliability patch

Paper-only. €0. Narrow reliability fixes only. **No** full historical scoring, walk-forward, optimization, ranking, holdout scoring, aggTrade fills, or live capture control.

## Bugs fixed

1. **Incompatible next-open `continue`** in `HistoricalCandleReplay.run`: cancelling a pending fill no longer skips evaluation of that candle; chronological observation continues.
2. **Formal scoring defaults**: new `run_formal_historical_replay` rejects `force_close_at_end=True` and requires `CutoffValidatedCandles`.
3. **Loader bypass hardening**: `iter_klines_from_csv` now applies the same open/close cutoff asserts; `candles_from_bars` refuses holdout timestamps by default; `load_formal_research_candles` wraps `load_klines`.

## Files created

| Path | Role |
| --- | --- |
| `reports/STAGE3.2.4.2_EXECUTION_MODEL.md` | Execution-model limitations |
| `reports/STAGE3.2.4.2_RELIABILITY_PATCH_REPORT.md` | This report |

## Files modified (research-lab only)

| Path | Change |
| --- | --- |
| `research_lab/backtesting/historical_replay.py` | Fix cancel path; docs; `run_formal_historical_replay` |
| `research_lab/datasets/historical_catalog.py` | `CutoffValidatedCandles`, `load_formal_research_candles`, stronger CSV/bar helpers |
| `research_lab/datasets/__init__.py` | Exports |
| `research_lab/backtesting/__init__.py` | Export formal entrypoint |
| `tests/test_historical_candle_replay.py` | Reliability regression tests |
| `configs/stage324_experiment.json` | Formal defaults + limitation flags |

**Untouched:** production strategy/PaperBroker/Stage 2 replay, H1 confirmation rules, B0 frozen parameters, quote-tape sessions, Vision raw archives, `F:\crypto-ai-trade-v4`.

## Tests added (offline fixtures)

- Incompatible next-open cancel + still evaluate + decision event + no fabricated fill
- Missing-next cancel event
- `force_close_at_end=False` unresolved position
- Deferred exit at next open
- Hand-calculated fee / slip / half-spread
- `iter_klines_from_csv` cutoff equality exclusion
- No future OHLC in decisions
- Formal entrypoint rejects force-close and raw lists
- `candles_from_bars` cutoff guard
- `load_formal_research_candles` wrapper

## Test results

| Suite | Result |
| --- | --- |
| `python -m unittest discover -s research-lab/tests` | **68 OK** |
| `python research-lab/run_stage324_smoke.py` | **exit 0** (infrastructure smoke only) |

## Cutoff enforcement

- Primary loaders and formal bundle still exclude `close_time/open_time >= 2026-10-31T04:20:59.999Z`.
- CSV equality regression covered.
- No holdout scoring or inspection performed.

## Unresolved-position handling

- Formal path forces `force_close_at_end=False`.
- Result exposes `completed_scored_trades` vs `unresolved_positions` separately; no invented end-of-sample fill price.

## Confirmations

- B0/H1 implementations and production files not edited in this patch.
- No historical B0/H1 experiment run.
- No sealed-holdout scoring.

## Remaining blockers (for a later authorized score)

1. Explicit human authorization for first holdout-excluded historical B0/H1 score.
2. Accept dual-track timing (historical next-open ≠ tape fill timing).
3. Accept absence of intra-bar protective stops on the candle path (documented; not implemented here).
4. Prefer `run_formal_historical_replay` + `load_formal_research_candles` only.

**Stopped after this patch.**
