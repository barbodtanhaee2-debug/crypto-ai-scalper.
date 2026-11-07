# Stage 3.3.1 — Implementation Report

**Status:** PASS  
**Date:** 2026-11-06  
**Scope:** `research-lab` only · €0 · paper-only · offline · synthetic fixtures only  

## Confirmation of authorization boundaries

- No access to `F:\crypto-ai-trade-v4`.
- No historical candle replays, quote-tape experiments, scoring, optimization, or benchmarking.
- No sealed-holdout access; no Stage 3.2.5 artifacts used or modified.
- No changes to B0/H1 strategies, production modules, Stage 2 replay, PaperBroker economics, or event-capture enabling.
- No Stage 3.2.9 / 3.3.2 work.
- No machine learning; no dependency changes.
- New documentation filenames only (no overwrite of prior stage reports).

## Files created

| Path | Role |
| --- | --- |
| `research_lab/characterization/__init__.py` | Public exports |
| `research_lab/characterization/candles.py` | OHLC candle type |
| `research_lab/characterization/config.py` | Provisional config + cost defaults |
| `research_lab/characterization/models.py` | Result enums/dataclasses |
| `research_lab/characterization/data_quality.py` | Validation (no repair) |
| `research_lab/characterization/features.py` | Feature formulas |
| `research_lab/characterization/primary.py` | Primary regime raw classifier |
| `research_lab/characterization/secondary.py` | Secondary overlapping labels |
| `research_lab/characterization/stability.py` | Persistence / hysteresis / gap reset |
| `research_lab/characterization/opportunity.py` | Cost-adjusted opportunity filter |
| `research_lab/characterization/pipeline.py` | Sequential no-look-ahead engine |
| `tests/test_market_characterization.py` | Synthetic-fixture unit tests |
| `docs/STAGE3.3.1_MARKET_CHARACTERIZATION_SPECIFICATION.md` | Specification |
| `reports/STAGE3.3.1_IMPLEMENTATION_REPORT.md` | This report |

## Files modified

None outside the new characterization package / its tests / new docs.  
Existing research-lab modules (events, historical replay, strategies, etc.) were not edited for this stage.

## Architecture decisions

1. **Three-way separation:** primary regime ≠ secondary characteristics ≠ opportunity filter.  
2. **R3/R4 as secondary volatility only**, so high/low volatility can coexist with TRENDING or RANGING.  
3. **Features isolated from classification;** primary isolated from secondary; stability is a forward-only state machine.  
4. **Fail closed** on invalid/missing/non-finite data → UNKNOWN / UNAVAILABLE.  
5. **Provisional thresholds** are explicit config fields, not hidden adaptive logic.  
6. **Opportunity layer reuses** `BacktestSettings` default fee/spread/slippage; does not invent a second fee model or emit orders.

## Feature formulas and configuration defaults

See `docs/STAGE3.3.1_MARKET_CHARACTERIZATION_SPECIFICATION.md` and `config.py`.

| Feature | Default lookback |
| --- | --- |
| ATR / NATR | 14 |
| Realized vol | 20 |
| Directional efficiency | 20 |
| EMA fast / slow | 8 / 21 |
| Rolling range | 20 |
| Volume deviation | off |

Warm-up bars = `max(lookbacks) + 1` (typically 22).

## Classification rules (summary)

- **TRENDING:** high efficiency + EMA separation + sufficient range span.  
- **RANGING:** low efficiency + bounded range width.  
- **Conflict / intermediate:** TRANSITIONING (conflict flagged when both trend and range fire).  
- **Confirmation:** 3-bar persistence; +2 hysteresis to switch; gap ≥ 2 intervals resets.  
- **Secondary vol:** NATR primary; RVOL confirms; hard NATR↔RVOL contradiction → UNKNOWN vol.

## Cost assumptions reused

| Parameter | Value | Source |
| --- | --- | --- |
| `fee_rate_per_side` | 0.001 | `BacktestSettings` default |
| `spread_fraction` | 0.0002 | `BacktestSettings` default |
| `slippage_rate_per_side` | 0.0005 | `BacktestSettings` default |
| Round-trip fraction | `2*fee + spread + 2*slip` = 0.0032 | Documented paper assumption |

Gross move = `normalized_atr * atr_move_multiplier` (proxy only; not MFE/forecast).

## Tests executed and exact results

Targeted:

```
python -m unittest tests.test_market_characterization -v
Ran 24 tests in ~0.06s
OK
```

Full research-lab suite:

```
python -m unittest discover -s tests -v
Ran 134 tests in ~1.50s
OK
```

Exact counts: **134 tests, 0 failures, 0 errors**.

Coverage includes all Stage 3.3.1 required synthetic cases (trend up/down, sideways, high-vol trend/range, compression, transitioning, conflicting evidence, warm-up, missing/duplicate/invalid OHLC, non-monotonic timestamps, non-finite inputs, hysteresis, persistence, gap reset, cost rejection, incomplete costs, determinism, no-look-ahead, no retrospective changes, primary/secondary independence).

## Existing tests affected

None failed. Prior suite grew from 110 → 134 by addition of the 24 new characterization tests only.

## Known limitations

- Provisional thresholds are not historically calibrated.  
- ATR gross-move proxy is a research filter, not expected PnL.  
- Volume path unused unless enabled.  
- No historical validation in this stage (intentionally).  
- Does not claim profitability, predictive accuracy, or strategy superiority.

## STOP

Stage 3.3.1 complete. Waiting for explicit approval before Stage 3.3.2 or any historical experiment.
