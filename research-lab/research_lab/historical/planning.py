"""Build monthly/daily archive plans for UTC half-open ranges."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterator, Literal

Kind = Literal["klines_1m", "aggTrades"]
Granularity = Literal["monthly", "daily"]


@dataclass(frozen=True)
class ArchiveSpec:
    kind: Kind
    granularity: Granularity
    period: str  # YYYY-MM or YYYY-MM-DD
    relative_path: str
    filename: str

    @property
    def checksum_filename(self) -> str:
        return f"{self.filename}.CHECKSUM"


def _month_start(value: date) -> date:
    return date(value.year, value.month, 1)


def _add_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _daterange(start: date, end_exclusive: date) -> Iterator[date]:
    cursor = start
    while cursor < end_exclusive:
        yield cursor
        cursor += timedelta(days=1)


def plan_archives(
    kind: Kind,
    start: datetime,
    end_exclusive: datetime,
) -> list[ArchiveSpec]:
    if start.tzinfo is None or end_exclusive.tzinfo is None:
        raise ValueError("Acquisition windows must be timezone-aware UTC.")
    start_d = start.astimezone(timezone.utc).date()
    end_d = end_exclusive.astimezone(timezone.utc).date()
    if end_d <= start_d:
        raise ValueError("end_exclusive must follow start.")

    specs: list[ArchiveSpec] = []
    month = _month_start(start_d)
    while month < end_d:
        next_month = _add_month(month)
        month_fully_inside = month >= start_d and next_month <= end_d
        if month_fully_inside:
            specs.append(_monthly_spec(kind, month))
        else:
            day_start = max(month, start_d)
            day_end = min(next_month, end_d)
            for day in _daterange(day_start, day_end):
                specs.append(_daily_spec(kind, day))
        month = next_month
    return specs


def _monthly_spec(kind: Kind, month: date) -> ArchiveSpec:
    period = f"{month.year:04d}-{month.month:02d}"
    if kind == "klines_1m":
        filename = f"BTCUSDT-1m-{period}.zip"
        relative = f"data/spot/monthly/klines/BTCUSDT/1m/{filename}"
    else:
        filename = f"BTCUSDT-aggTrades-{period}.zip"
        relative = f"data/spot/monthly/aggTrades/BTCUSDT/{filename}"
    return ArchiveSpec(kind, "monthly", period, relative, filename)


def _daily_spec(kind: Kind, day: date) -> ArchiveSpec:
    period = f"{day.year:04d}-{day.month:02d}-{day.day:02d}"
    if kind == "klines_1m":
        filename = f"BTCUSDT-1m-{period}.zip"
        relative = f"data/spot/daily/klines/BTCUSDT/1m/{filename}"
    else:
        filename = f"BTCUSDT-aggTrades-{period}.zip"
        relative = f"data/spot/daily/aggTrades/BTCUSDT/{filename}"
    return ArchiveSpec(kind, "daily", period, relative, filename)
