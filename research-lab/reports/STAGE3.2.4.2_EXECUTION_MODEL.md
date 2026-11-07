# Stage 3.2.4.2 — Historical candle execution model

Paper-only research assumptions. Not live trading.

## Model

- Signal after candle **N** closes (bar available only then).
- Earliest simulated fill: candle **N+1** **open** mid ± frozen synthetic spread, then frozen slippage and fees via PaperBroker.
- Missing N+1 / gap: cancel pending fill; never fabricate candles or fills.
- Incompatible N+1 open vs signal stop/target: cancel pending **without** rewriting stops/targets; **still evaluate** candle N+1.

## What this is not

- Not historical L2 / order-book depth.
- Not bookTicker tape replay (`BINANCE_SPOT_LIVE_CAPTURE`).
- Not intra-bar protective stop simulation (no high/low stop path on this adapter).
- Not equivalent to live Spot execution.

## Formal scoring defaults

Use `run_formal_historical_replay` with `CutoffValidatedCandles` from `load_formal_research_candles` (or bind with `load_candles=True`).

- `force_close_at_end=True` is **rejected**.
- Unresolved positions are reported separately from completed scored trades.
- Sealed holdout cutoff `2026-10-31T04:20:59.999Z` remains excluded.
