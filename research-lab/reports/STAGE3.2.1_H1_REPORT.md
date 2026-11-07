# Stage 3.2.1 — H1 next-bar close confirmation

Paper-only. Frozen `momentum_breakout` parameters were not changed. Production files were not modified. The chronological holdout (final 25% by candle close) was **excluded from replay** and was **not scored**.

This is **not** a profitability result.

## Dataset

- dataset_id: `stage23_extended_btcusdt_20261030T191728Z-4751556f`
- session: `20261030T191728Z-4751556f`
- manifest: `F:\crypto-ai-scalper\quote-tape\reports\STAGE2.3_EXTENDED_SHA256_MANIFEST.csv`
- session.json SHA-256: `c3995cb3483d53b648054fee495c8323a71ad81bbe5791614a29a8c8f8f1cf70`
- clock: `host_receive` for both arms
- Stage 2.4 kline_event trade counts were **not** used

## Windows

| Split | Candles |
| --- | ---: |
| Train | 362 |
| Validation | 181 |
| Holdout (unscored, not replayed) | 182 |
| Replay window (session start through validation end) | 543 |

Holdout starts at `2026-10-31T04:20:59.999000+00:00`.

## Independent host_receive baseline

Frozen `momentum_breakout` on recorded bid/ask, `host_receive` freshness.

| Metric | Value |
| --- | --- |
| Stale candle decisions | 1 |
| Completed trades in replay window | 2 |
| Completed trades scored (train+validation, both legs before holdout) | 2 |
| After-cost net P&L (scored only) | -0.7612047118075549 |
| Ranking allowed | False |

## Candidate (next-bar close confirmation)

| Metric | Value |
| --- | --- |
| Stale candle decisions | 1 |
| Completed trades in replay window | 1 |
| Completed trades scored | 1 |
| After-cost net P&L (scored only) | -0.2674905493152743 |
| Ranking allowed | False |

## Opportunity overlay (primary)

Baseline long **signals** (evaluate→entry), not occupancy-matched fills.

| Outcome | Count |
| --- | ---: |
| Baseline long signals | 2 |
| Confirmed (t+1 close held breakout) | 1 |
| Failed confirmation | 1 |
| Missing next-minute candle | 0 |
| Unresolved at window end | 0 |

CSV: `research-lab/reports/h1/opportunity_overlay.csv`

## Full-path replay (secondary)

Occupancy can make trade sets incomparable. Unmatched trades are **not** forced.

| Set | Count |
| --- | ---: |
| Matched (entry within 120 s, same direction) | 1 |
| Unmatched baseline | 1 |
| Unmatched candidate | 0 |

## Reproducibility

Two runs per arm. Journal fingerprints drop `account_initialized` wall-clock.

`{"baseline_journal_fingerprint_equal": true, "candidate_journal_fingerprint_equal": true, "baseline_trades_equal": true, "candidate_trades_equal": true, "unexplained_differences": []}`

## Tests

`python -m unittest discover -s tests` → **259 OK** (previous 249 plus 10 H1 tests). Two-run journal fingerprints matched on the live H1 arms.

## Economic classification

**inconclusive** — Either arm has fewer than 30 completed scored trades; H1 is not an economic ranking result.

Baseline scored trades: 2. Candidate scored trades: 1. Gate: 30.

Scored P&L figures above are descriptive bookkeeping only. They must not be read as evidence that confirmation helped or hurt.
