"""Public Binance Spot historical kline download and validation.

Live runtime kline intervals are unchanged. This module is for offline
research datasets only and never uses authenticated or order endpoints.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backtester import load_candles_csv, validate_candles
from market_data import BINANCE_KLINE_REST_URL, _price
from strategy import Bar

HISTORICAL_KLINE_INTERVALS = {3600: "1h"}
BINANCE_KLINE_LIMIT = 1000
EXPECTED_BTCUSDT_1H_2025_COUNT = 8760
DEFAULT_START = datetime(2025, 1, 1, tzinfo=timezone.utc)
DEFAULT_END_EXCLUSIVE = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class RawKline:
    open_time_ms: int
    close_time_ms: int
    bar: Bar
    volume: float
    quote_volume: float
    trade_count: int


def _aware_utc(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware UTC timestamp.")
    return value.astimezone(timezone.utc)


def historical_kline_interval_label(interval_seconds: int) -> str:
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int):
        raise ValueError("Historical kline interval must be an integer.")
    try:
        return HISTORICAL_KLINE_INTERVALS[interval_seconds]
    except KeyError as error:
        raise ValueError(
            "Unsupported historical kline interval. This downloader currently "
            "supports 1h (3600 seconds) only and does not change live runtime "
            "intervals."
        ) from error


def _volume(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{name} must be numeric.")
    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be numeric.") from error
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and positive.")
    return result


def parse_binance_kline_row(row: object, *, interval_seconds: int) -> RawKline:
    if not isinstance(row, list) or len(row) < 9:
        raise ValueError("Binance kline row is invalid.")
    interval_ms = interval_seconds * 1000
    open_time_ms, close_time_ms = row[0], row[6]
    if (
        isinstance(open_time_ms, bool)
        or not isinstance(open_time_ms, int)
        or isinstance(close_time_ms, bool)
        or not isinstance(close_time_ms, int)
        or open_time_ms % interval_ms != 0
        or close_time_ms != open_time_ms + interval_ms - 1
    ):
        raise ValueError("Binance kline row has invalid interval boundaries.")
    prices = tuple(
        _price(row[offset], name)
        for offset, name in zip((1, 2, 3, 4), ("open", "high", "low", "close"))
    )
    if prices[2] > min(prices[0], prices[3]) or prices[1] < max(prices[0], prices[3]):
        raise ValueError("Binance kline row has inconsistent OHLC values.")
    trade_count = row[8]
    if (
        isinstance(trade_count, bool)
        or not isinstance(trade_count, int)
        or trade_count < 1
    ):
        raise ValueError("Binance kline row has invalid trade count.")
    close_time = datetime.fromtimestamp(close_time_ms / 1000, tz=timezone.utc)
    return RawKline(
        open_time_ms=open_time_ms,
        close_time_ms=close_time_ms,
        bar=Bar(close_time, *prices),
        volume=_volume(row[5], "volume"),
        quote_volume=_volume(row[7], "quote_volume"),
        trade_count=trade_count,
    )


def validate_historical_klines(
    rows: Sequence[RawKline],
    *,
    interval_seconds: int,
    start: datetime,
    end_exclusive: datetime,
    expected_count: int | None = None,
) -> tuple[RawKline, ...]:
    start_utc = _aware_utc(start, "start")
    end_utc = _aware_utc(end_exclusive, "end_exclusive")
    if start_utc >= end_utc:
        raise ValueError("start must precede exclusive end.")
    if not rows:
        raise ValueError("Historical kline dataset is empty.")
    start_open_ms = int(start_utc.timestamp() * 1000)
    end_open_ms = int(end_utc.timestamp() * 1000)
    interval_ms = interval_seconds * 1000
    previous_open = None
    validated: list[RawKline] = []
    for index, row in enumerate(rows):
        if not isinstance(row, RawKline):
            raise ValueError(f"Kline {index} is not a RawKline.")
        if row.open_time_ms < start_open_ms or row.open_time_ms >= end_open_ms:
            raise ValueError(f"Kline {index} is outside the requested range.")
        if previous_open is not None:
            gap = row.open_time_ms - previous_open
            if gap <= 0:
                raise ValueError("Kline timestamps must be unique and chronological.")
            if gap != interval_ms:
                raise ValueError(
                    f"Missing or extra kline before {row.bar.timestamp.isoformat()}."
                )
        elif row.open_time_ms != start_open_ms:
            raise ValueError("Dataset does not start on the requested open time.")
        validated.append(row)
        previous_open = row.open_time_ms
    last_open = end_open_ms - interval_ms
    if validated[-1].open_time_ms != last_open:
        raise ValueError("Dataset does not end on the last requested open time.")
    bars = validate_candles(
        [row.bar for row in validated],
        interval_seconds=interval_seconds,
    )
    if len(bars) != len(validated):
        raise ValueError("Validated bars do not match raw klines.")
    if expected_count is not None and len(validated) != expected_count:
        raise ValueError(
            f"Expected {expected_count} candles, received {len(validated)}."
        )
    return tuple(validated)


def _request_klines(query: dict[str, object], *, timeout: float) -> list:
    url = f"{BINANCE_KLINE_REST_URL}?{urlencode(query)}"
    request = Request(url, headers={"User-Agent": "crypto-ai-scalper-research/paper"})
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        raise ValueError("Binance kline history response must be a list.")
    return payload


def download_binance_spot_klines(
    *,
    start: datetime,
    end_exclusive: datetime,
    interval_seconds: int = 3600,
    timeout: float = 10.0,
    pause_seconds: float = 0.2,
    requester=_request_klines,
) -> tuple[RawKline, ...]:
    """Download public Spot klines. Uses /api/v3/klines only."""
    interval_label = historical_kline_interval_label(interval_seconds)
    start_utc = _aware_utc(start, "start")
    end_utc = _aware_utc(end_exclusive, "end_exclusive")
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000)
    cursor = start_ms
    collected: list[RawKline] = []
    seen_opens: set[int] = set()
    while cursor < end_ms:
        payload = requester(
            {
                "symbol": "BTCUSDT",
                "interval": interval_label,
                "startTime": cursor,
                "endTime": end_ms - 1,
                "limit": BINANCE_KLINE_LIMIT,
            },
            timeout=timeout,
        )
        if not payload:
            break
        last_open = None
        for row in payload:
            parsed = parse_binance_kline_row(row, interval_seconds=interval_seconds)
            last_open = parsed.open_time_ms
            if parsed.open_time_ms < start_ms or parsed.open_time_ms >= end_ms:
                continue
            if parsed.open_time_ms in seen_opens:
                continue
            seen_opens.add(parsed.open_time_ms)
            collected.append(parsed)
        if last_open is None:
            break
        nxt = last_open + interval_seconds * 1000
        if nxt <= cursor:
            raise ValueError("Kline pagination did not advance.")
        cursor = nxt
        if pause_seconds > 0:
            time.sleep(pause_seconds)
    collected.sort(key=lambda row: row.open_time_ms)
    return tuple(collected)


def write_dataset(
    rows: Sequence[RawKline],
    output_dir: str | Path,
    *,
    interval_seconds: int,
    start: datetime,
    end_exclusive: datetime,
    expected_count: int | None,
    retrieved_at: datetime,
    source_url: str,
    valid: bool,
    error: str | None = None,
) -> Path:
    destination = Path(output_dir)
    if destination.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing dataset directory: {destination}"
        )
    destination.mkdir(parents=True, exist_ok=False)
    raw_path = destination / "raw_klines.jsonl"
    with raw_path.open("w", encoding="utf-8", newline="\n") as target:
        for row in rows:
            json.dump(
                {
                    "open_time_ms": row.open_time_ms,
                    "close_time_ms": row.close_time_ms,
                    "open": row.bar.open,
                    "high": row.bar.high,
                    "low": row.bar.low,
                    "close": row.bar.close,
                    "volume": row.volume,
                    "quote_volume": row.quote_volume,
                    "trade_count": row.trade_count,
                    "timestamp": row.bar.timestamp.isoformat(),
                },
                target,
            )
            target.write("\n")
    csv_path = destination / "candles.csv"
    if valid:
        with csv_path.open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(
                target,
                fieldnames=("timestamp", "open", "high", "low", "close"),
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "timestamp": row.bar.timestamp.isoformat(),
                        "open": row.bar.open,
                        "high": row.bar.high,
                        "low": row.bar.low,
                        "close": row.bar.close,
                    }
                )
    manifest = {
        "symbol": "BTCUSDT",
        "market": "Binance Spot",
        "interval": historical_kline_interval_label(interval_seconds),
        "interval_seconds": interval_seconds,
        "start_inclusive_open_utc": _aware_utc(start, "start").isoformat(),
        "end_exclusive_open_utc": _aware_utc(end_exclusive, "end_exclusive").isoformat(),
        "timestamp_convention": (
            "CSV timestamps are timezone-aware candle close times, matching "
            "the existing backtester schema."
        ),
        "source_endpoint": source_url,
        "authenticated": False,
        "api_keys_used": False,
        "retrieved_at_utc": retrieved_at.astimezone(timezone.utc).isoformat(),
        "row_count": len(rows),
        "expected_count": expected_count,
        "valid": valid,
        "error": error,
        "files": {
            "raw_klines": raw_path.name,
            "backtester_csv": csv_path.name if valid else None,
        },
    }
    manifest_path = destination / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as target:
        json.dump(manifest, target, indent=2, allow_nan=False)
        target.write("\n")
    return destination


def new_dataset_directory(root: str | Path, *, clock: datetime | None = None) -> Path:
    stamp = (clock or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return Path(root) / f"btcusdt_1h_2025_{stamp}"


def load_and_validate_backtester_csv(
    path: str | Path,
    *,
    interval_seconds: int,
    expected_count: int | None = None,
) -> tuple[Bar, ...]:
    candles = load_candles_csv(path, interval_seconds=interval_seconds)
    if expected_count is not None and len(candles) != expected_count:
        raise ValueError(
            f"CSV has {len(candles)} candles; expected {expected_count}."
        )
    return candles


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", default="data")
    parser.add_argument("--interval-seconds", type=int, default=3600)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_BTCUSDT_1H_2025_COUNT)
    args = parser.parse_args()
    start = DEFAULT_START
    end_exclusive = DEFAULT_END_EXCLUSIVE
    retrieved_at = datetime.now(timezone.utc)
    destination = new_dataset_directory(args.output_root, clock=retrieved_at)
    error = None
    valid = False
    rows: tuple[RawKline, ...] = ()
    try:
        rows = download_binance_spot_klines(
            start=start,
            end_exclusive=end_exclusive,
            interval_seconds=args.interval_seconds,
        )
        rows = validate_historical_klines(
            rows,
            interval_seconds=args.interval_seconds,
            start=start,
            end_exclusive=end_exclusive,
            expected_count=args.expected_count,
        )
        valid = True
    except (TypeError, ValueError, OSError) as exc:
        error = str(exc)
        valid = False
    write_dataset(
        rows,
        destination,
        interval_seconds=args.interval_seconds,
        start=start,
        end_exclusive=end_exclusive,
        expected_count=args.expected_count,
        retrieved_at=retrieved_at,
        source_url=BINANCE_KLINE_REST_URL,
        valid=valid,
        error=error,
    )
    if not valid:
        raise SystemExit(
            f"Dataset invalid; preserved raw download under {destination}: {error}"
        )
    print(f"Wrote validated dataset to {destination} ({len(rows)} candles).")


if __name__ == "__main__":
    main()
