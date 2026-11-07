"""Paths and UTC acquisition windows for Binance Spot historical archives."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from research_lab import LAB_ROOT

PROVENANCE = "BINANCE_SPOT_HISTORICAL"
LIVE_PROVENANCE = "BINANCE_SPOT_LIVE_CAPTURE"

VISION_BASE = "https://data.binance.vision"
SYMBOL = "BTCUSDT"
MARKET = "spot"
KLINE_INTERVAL = "1m"
KLINE_INTERVAL_SECONDS = 60

KLINES_START = datetime(2024, 11, 4, tzinfo=timezone.utc)
KLINES_END_EXCLUSIVE = datetime(2026, 11, 4, tzinfo=timezone.utc)
AGGTRADES_START = datetime(2026, 8, 4, tzinfo=timezone.utc)
AGGTRADES_END_EXCLUSIVE = datetime(2026, 11, 4, tzinfo=timezone.utc)

# Binance Spot open-time units switched from ms to µs on this UTC day.
TIMESTAMP_UNIT_SWITCH = datetime(2025, 1, 1, tzinfo=timezone.utc)

# Sealed holdout start — Stage 3.2.4 research must exclude this instant and later.
RESEARCH_CUTOFF = datetime(2026, 10, 31, 4, 20, 59, 999000, tzinfo=timezone.utc)
HISTORICAL_DATASET_ID = "binance_spot_btcusdt_vision_stage324"

DATASET_ROOT = LAB_ROOT / "data" / "binance_spot" / SYMBOL
RAW_KLINES = DATASET_ROOT / "raw" / "klines_1m"
RAW_AGGTRADES = DATASET_ROOT / "raw" / "aggTrades"
PROCESSED = DATASET_ROOT / "processed"
MANIFESTS = DATASET_ROOT / "manifests"
LOGS = DATASET_ROOT / "logs"
REPORTS = DATASET_ROOT / "reports"

DISK_RESERVE_BYTES = 20 * 1024 * 1024 * 1024  # keep 20 GiB free after download
MAX_WORKERS = 4
MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
HTTP_TIMEOUT_SECONDS = 120.0

KLINE_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
)

AGGTRADE_COLUMNS = (
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "timestamp",
    "buyer_is_maker",
    "best_price_match",
)


def ensure_layout() -> None:
    for path in (
        RAW_KLINES,
        RAW_AGGTRADES,
        PROCESSED,
        MANIFESTS,
        LOGS,
        REPORTS,
        PROCESSED / "klines_1m",
        PROCESSED / "aggTrades",
        PROCESSED / "derived",
    ):
        path.mkdir(parents=True, exist_ok=True)
