# Stage 3.2.3 — Historical Binance Vision acquisition plan

Paper-only research. Everything lives under `research-lab/`. Production, H1 rules, quote-tape sessions, and `F:\crypto-ai-trade-v4` are untouched.

## Existing architecture (reuse, do not modify)

| Component | Role | Constraint |
| --- | --- | --- |
| `research_lab.datasets.catalog` | Quote-tape session pointers | Keep separate from historical archives |
| `research_lab.isolation` | Refuse v4 / live journals / capture writes | Extend usage only; do not relax |
| Production `historical_data.py` | REST 1h klines for frozen 2025 baseline | **Do not modify or call for this job** |
| Live quote-tape sessions | `BINANCE_SPOT_LIVE_CAPTURE` provenance | Do not merge with historical |

## Proposed package

```
research-lab/
  data/binance_spot/BTCUSDT/
    raw/klines_1m/          verified ZIP + CHECKSUM (immutable)
    raw/aggTrades/
    processed/              normalized CSV / JSONL + sidecar meta
    manifests/
    logs/
    reports/
  research_lab/historical/  downloader, timestamps, ingest, derive, manifest
  tests/test_historical_binance_vision.py
  run_historical_acquisition.py
```

Provenance label: `BINANCE_SPOT_HISTORICAL`. Never described as order-book snapshots.

## Source

Base: `https://data.binance.vision/`

- Monthly/daily Spot klines `BTCUSDT` `1m`
- Monthly/daily Spot `aggTrades` `BTCUSDT`

UTC ranges:

- Klines: `[2024-11-04, 2026-11-04)`
- AggTrades: `[2026-08-04, 2026-11-04)`

Partial boundary months use daily archives; interior full months use monthly archives.

## Safety

1. Disk free check + HEAD/content-length plan before bulk download.
2. SHA-256 via `.CHECKSUM` before accepting a ZIP.
3. `.part` temp files; atomic rename after verify.
4. Bounded retries/concurrency; resume incomplete `.part` files.
5. Stream ZIP CSV members for aggTrades (no full RAM load).
6. Stop if estimated size exceeds free space minus a reserve.

## Timestamp policy

Detect ms vs µs (Binance Spot changed 2025-01-01). Preserve raw integers; normalize processed UTC. Report missing 1m intervals; never invent candles.

## Out of scope

H1 scoring, quote-tape merge, multi-interval archive downloads, production changes.
