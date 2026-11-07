# Stage 3.2.5 — Full historical B0 vs H1 comparison

Paper-only. €0. User-authorized single offline experiment.
Historical candle track only (`BINANCE_SPOT_HISTORICAL`). Separate from live quote-tape.

## Conclusion: **PASS**

Both arms have ≥30 completed scored trades (B0=41, H1=40). After-cost metrics below are descriptive only. Not a profitability claim; not production promotion; not equivalent to tape H1 or live execution.

## Dataset and manifest verification

- Provenance: `BINANCE_SPOT_HISTORICAL`
- Manifest SHA-256: `73ba52180fdc77fbd51e04ce881dcd44b623924ea478b99c9b563e99d3d91be8`
- Manifest path: `F:\crypto-ai-scalper\research-lab\data\binance_spot\BTCUSDT\manifests\dataset_manifest_20261104T184815Z.json`
- Research start: `2024-11-04T00:00:00+00:00`
- Research end exclusive (sealed holdout): `2026-10-31T04:20:59.999000+00:00`
- Formal candles loaded: **1045700**
- Excluded at/after cutoff: **5500**
- Max close in research set: `2026-10-31T04:19:59.999999+00:00`

## Exact run configuration

- Run ID: `20261106T063406Z`
- Entrypoint: `load_formal_research_candles` + `run_formal_historical_replay`
- `force_close_at_end`: `False`
- Arms: B0=`momentum_breakout_logged`, H1=`h1_confirmed_momentum_breakout`
- Costs: fee=0.001, slip=0.0005, spread=0.0002, equity=100.0
- Risk: risk_fraction=0.005, daily_loss=0.02, max_dd=0.1
- Execution model: `next_available_candle_open`

## Trade counts

| Arm | Completed scored trades | Unresolved positions | Entries accepted | Entries rejected |
| --- | ---: | ---: | ---: | ---: |
| B0 | 41 | 0 | 41 | 18211 |
| H1 | 40 | 0 | 40 | 10694 |

## After-cost metrics (completed scored trades only)

| Metric | B0 | H1 |
| --- | ---: | ---: |
| Net P&L | -10.163937885744927 | -10.047782821887592 |
| Net return % | -10.163937885744955 | -10.04778282188752 |
| Win rate | 0.17073170731707318 | 0.15 |
| Expectancy / trade | -0.2479009240425592 | -0.2511945705471898 |
| Profit factor | 0.1116386798840133 | 0.10326961571327194 |
| Max drawdown (frac) | 0.10163937885744957 | 0.10047782821887609 |
| Fees paid | 7.4757578829069 | 7.284500504558901 |
| Est. spread costs | 0.7475758587484007 | 0.7284501104784805 |
| Est. slippage | 3.7378792038782502 | 3.642250472021489 |
| Ranking allowed | True | True |
| Insufficient sample | False | False |

## Cancellations and rejects

| Arm | Missing-next cancels | Incompatible-open cancels | Missing intervals |
| --- | ---: | ---: | ---: |
| B0 | 0 | 18421 | 0 |
| H1 | 0 | 18421 | 0 |

## Deterministic fingerprints

- B0 replica match: `True`
- H1 replica match: `True`
- B0 fingerprints: `['c0118119ae1e0ce10543181704b024a7cfcf7a0539f4aaabf707cd8e27df34bb', 'c0118119ae1e0ce10543181704b024a7cfcf7a0539f4aaabf707cd8e27df34bb']`
- H1 fingerprints: `['4a448f4730ade9bd414262fd9d593bb25ab8e1ce5602b59b803c35be84ea7d7d', '4a448f4730ade9bd414262fd9d593bb25ab8e1ce5602b59b803c35be84ea7d7d']`

## Limitations

- Synthetic candle-derived quotes (mid ± spread); **not** historical L2 / bookTicker.
- **No** intra-bar protective stop simulation on this path.
- Historical next-open fill timing differs from Stage 3.2.1 tape H1.
- Unresolved end-of-window positions are **not** force-closed.
- Results are not proof of future profitability and must not be promoted to production from this report alone.

Raw JSON: `F:\crypto-ai-scalper\research-lab\reports\stage325_results_20261106T063406Z.json`

