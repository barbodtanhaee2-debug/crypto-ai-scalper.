"""Derive coarser OHLCV intervals from complete 1-minute candles only."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from research_lab.historical.constants import PROCESSED, PROVENANCE
from research_lab.isolation import assert_isolated_path

SUPPORTED_MINUTES = (5, 15, 60)


@dataclass
class DerivedBar:
    open_time_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    complete: bool
    source_1m_count: int


@dataclass
class DeriveReport:
    interval_minutes: int
    bars_written: int = 0
    incomplete_bars: int = 0
    skipped_incomplete: int = 0
    missing_source_minutes: list[str] = field(default_factory=list)


def floor_utc(moment: datetime, minutes: int) -> datetime:
    moment = moment.astimezone(timezone.utc)
    if minutes not in SUPPORTED_MINUTES:
        raise ValueError(f"Unsupported interval minutes: {minutes}")
    total_minutes = moment.hour * 60 + moment.minute
    floored = total_minutes - (total_minutes % minutes)
    return moment.replace(
        hour=floored // 60,
        minute=floored % 60,
        second=0,
        microsecond=0,
    )


def _read_1m_rows(path: Path) -> Iterator[dict[str, str]]:
    with Path(path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            yield row


def aggregate_1m_to_interval(
    rows: Iterator[dict[str, str]],
    *,
    interval_minutes: int,
    require_complete: bool = True,
) -> tuple[list[DerivedBar], DeriveReport]:
    if interval_minutes not in SUPPORTED_MINUTES:
        raise ValueError(f"interval_minutes must be one of {SUPPORTED_MINUTES}")
    report = DeriveReport(interval_minutes=interval_minutes)
    expected = interval_minutes
    buckets: dict[datetime, list[dict[str, str]]] = {}
    order: list[datetime] = []
    previous_open: datetime | None = None
    for row in rows:
        open_dt = datetime.fromisoformat(row["open_time_utc"])
        if open_dt.tzinfo is None:
            open_dt = open_dt.replace(tzinfo=timezone.utc)
        if previous_open is not None:
            gap = (open_dt - previous_open).total_seconds()
            if gap > 60.5:
                steps = int(round(gap / 60.0))
                for index in range(1, steps):
                    missing = previous_open + timedelta(minutes=index)
                    report.missing_source_minutes.append(missing.isoformat())
        previous_open = open_dt
        key = floor_utc(open_dt, interval_minutes)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(row)

    bars: list[DerivedBar] = []
    for key in order:
        members = buckets[key]
        complete = len(members) == expected
        if require_complete and not complete:
            report.skipped_incomplete += 1
            report.incomplete_bars += 1
            continue
        if not complete:
            report.incomplete_bars += 1
        opens = [float(item["open"]) for item in members]
        highs = [float(item["high"]) for item in members]
        lows = [float(item["low"]) for item in members]
        closes = [float(item["close"]) for item in members]
        volumes = [float(item["volume"]) for item in members]
        quotes = [float(item["quote_volume"]) for item in members]
        trades = [int(item["trades"]) for item in members]
        bars.append(
            DerivedBar(
                open_time_utc=key,
                open=opens[0],
                high=max(highs),
                low=min(lows),
                close=closes[-1],
                volume=sum(volumes),
                quote_volume=sum(quotes),
                trades=sum(trades),
                complete=complete,
                source_1m_count=len(members),
            )
        )
        report.bars_written += 1
    return bars, report


def write_derived_csv(
    bars: list[DerivedBar],
    destination: Path,
    *,
    interval_minutes: int,
) -> Path:
    destination = assert_isolated_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "open_time_utc",
        "interval_minutes",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trades",
        "complete",
        "source_1m_count",
        "provenance",
        "derived_from",
    ]
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for bar in bars:
            writer.writerow(
                {
                    "open_time_utc": bar.open_time_utc.isoformat(),
                    "interval_minutes": interval_minutes,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "quote_volume": bar.quote_volume,
                    "trades": bar.trades,
                    "complete": bar.complete,
                    "source_1m_count": bar.source_1m_count,
                    "provenance": PROVENANCE,
                    "derived_from": "klines_1m",
                }
            )
    return destination


def derive_from_processed_1m(
    source_csv: Path,
    *,
    interval_minutes: int,
    output_csv: Path | None = None,
    require_complete: bool = True,
) -> DeriveReport:
    source_csv = Path(source_csv)
    bars, report = aggregate_1m_to_interval(
        _read_1m_rows(source_csv),
        interval_minutes=interval_minutes,
        require_complete=require_complete,
    )
    destination = assert_isolated_path(
        output_csv
        or (
            PROCESSED
            / "derived"
            / f"{source_csv.stem}_{interval_minutes}m.csv"
        )
    )
    write_derived_csv(bars, destination, interval_minutes=interval_minutes)
    return report
