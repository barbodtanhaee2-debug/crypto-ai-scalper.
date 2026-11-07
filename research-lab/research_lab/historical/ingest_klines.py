"""Stream-parse verified kline ZIPs into normalized research CSV + validation."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from research_lab.historical.constants import (
    KLINE_COLUMNS,
    KLINE_INTERVAL_SECONDS,
    PROCESSED,
    PROVENANCE,
)
from research_lab.historical.timestamps import detect_unit, to_utc_datetime
from research_lab.isolation import assert_isolated_path


@dataclass
class KlineValidation:
    archive: str
    rows_read: int = 0
    rows_written: int = 0
    rejected: list[dict[str, object]] = field(default_factory=list)
    duplicate_open_times: int = 0
    out_of_order: int = 0
    missing_intervals: list[str] = field(default_factory=list)
    open_unit_counts: dict[str, int] = field(default_factory=dict)
    first_open_utc: str | None = None
    last_open_utc: str | None = None


def _looks_like_header(row: list[str]) -> bool:
    if not row:
        return False
    first = row[0].strip().lower()
    return first in {"open_time", "open time"} or first.startswith("open_time")


def iter_kline_rows_from_zip(archive: Path) -> Iterator[list[str]]:
    with zipfile.ZipFile(archive) as bundle:
        names = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
        if not names:
            raise ValueError(f"No CSV member in {archive.name}.")
        with bundle.open(names[0]) as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8", newline="")
            reader = csv.reader(text)
            for row in reader:
                if not row or all(not cell.strip() for cell in row):
                    continue
                if _looks_like_header(row):
                    continue
                yield row


def _finite_nonneg(value: str, name: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise ValueError(f"{name} is not numeric.") from error
    if not (number == number) or number < 0:  # NaN check
        raise ValueError(f"{name} must be finite and nonnegative.")
    return number


def _finite_positive(value: str, name: str) -> float:
    number = _finite_nonneg(value, name)
    if number <= 0:
        raise ValueError(f"{name} must be positive.")
    return number


def parse_kline_row(row: list[str]) -> dict[str, object]:
    if len(row) < 12:
        raise ValueError(f"Kline row has {len(row)} fields; expected 12.")
    open_raw = int(row[0])
    close_raw = int(row[6])
    unit = detect_unit(open_raw)
    open_dt = to_utc_datetime(open_raw, unit=unit)
    close_dt = to_utc_datetime(close_raw, unit=detect_unit(close_raw, hint_open=open_dt))
    o = _finite_positive(row[1], "open")
    h = _finite_positive(row[2], "high")
    low = _finite_positive(row[3], "low")
    c = _finite_positive(row[4], "close")
    volume = _finite_nonneg(row[5], "volume")
    quote_volume = _finite_nonneg(row[7], "quote_volume")
    trades = int(row[8])
    if trades < 0:
        raise ValueError("trades must be nonnegative.")
    if low > min(o, c) or h < max(o, c) or low > h:
        raise ValueError("OHLC values are inconsistent.")
    expected_close = open_dt + timedelta(seconds=KLINE_INTERVAL_SECONDS) - timedelta(milliseconds=1)
    # Allow ms or us representation of inclusive close; compare to second precision floor.
    if abs((close_dt - open_dt).total_seconds() - (KLINE_INTERVAL_SECONDS - 0.001)) > 1.0:
        # Soft check: close must be after open and before open+60s.
        if not (open_dt < close_dt <= open_dt + timedelta(seconds=KLINE_INTERVAL_SECONDS)):
            raise ValueError("close_time is outside the expected one-minute window.")
    return {
        "open_time_raw": open_raw,
        "close_time_raw": close_raw,
        "timestamp_unit": unit,
        "open_time_utc": open_dt.isoformat(),
        "close_time_utc": close_dt.isoformat(),
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "volume": volume,
        "quote_volume": quote_volume,
        "trades": trades,
        "taker_buy_base_volume": _finite_nonneg(row[9], "taker_buy_base_volume"),
        "taker_buy_quote_volume": _finite_nonneg(row[10], "taker_buy_quote_volume"),
        "ignore": row[11],
        "provenance": PROVENANCE,
        "source_columns": list(KLINE_COLUMNS),
    }


def ingest_kline_archive(archive: Path, *, output_csv: Path | None = None) -> KlineValidation:
    archive = Path(archive)
    report = KlineValidation(archive=archive.name)
    destination = assert_isolated_path(
        output_csv
        or (PROCESSED / "klines_1m" / f"{archive.stem}.csv")
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    previous: datetime | None = None
    fieldnames = [
        "open_time_utc",
        "close_time_utc",
        "open_time_raw",
        "close_time_raw",
        "timestamp_unit",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trades",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
        "ignore",
        "provenance",
    ]
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in iter_kline_rows_from_zip(archive):
            report.rows_read += 1
            try:
                parsed = parse_kline_row(row)
            except (ValueError, TypeError, OverflowError) as error:
                report.rejected.append({"row": row[:12], "error": str(error)})
                continue
            unit = str(parsed["timestamp_unit"])
            report.open_unit_counts[unit] = report.open_unit_counts.get(unit, 0) + 1
            open_key = str(parsed["open_time_utc"])
            open_dt = datetime.fromisoformat(open_key)
            if open_key in seen:
                report.duplicate_open_times += 1
                report.rejected.append({"row": row[:12], "error": "duplicate open_time"})
                continue
            seen.add(open_key)
            if previous is not None:
                if open_dt < previous:
                    report.out_of_order += 1
                    report.rejected.append({"row": row[:12], "error": "out_of-order open_time"})
                    continue
                gap = (open_dt - previous).total_seconds()
                if gap > KLINE_INTERVAL_SECONDS + 0.5:
                    steps = int(round(gap / KLINE_INTERVAL_SECONDS))
                    for index in range(1, steps):
                        missing = previous + timedelta(seconds=KLINE_INTERVAL_SECONDS * index)
                        report.missing_intervals.append(missing.isoformat())
            previous = open_dt
            if report.first_open_utc is None:
                report.first_open_utc = open_key
            report.last_open_utc = open_key
            writer.writerow(parsed)
            report.rows_written += 1
    meta_path = destination.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")
    return report
