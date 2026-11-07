# Research hypotheses (not implemented in Stage 3.1)

These are questions for later stages. None is assumed profitable. Do not treat this file as a strategy list to optimize on the Stage 2.3 test window.

## A. Short-term momentum

Continuation after a close break of a short lookback high, with optional volume confirmation from captured kline base/quote volume.

## B. Mean reversion

Fade short-lived dislocations or post-spike extensions while estimated round-trip costs stay below expected reversion.

## C. Market microstructure

Use recorded bid/ask, sizes, and spread path. There is no full L2 book in the Stage 2.3 tape. Quote pressure is limited to bookTicker quantities.

## D. Volatility-aware scalping

Enter only when recent range/ATR exceeds cost; stand aside when costs dominate.

Stage 3.1 only proves the laboratory can evaluate such strategies fairly (same clocks, tape fills, costs, splits, sample-size gates).
