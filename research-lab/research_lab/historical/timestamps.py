"""Detect and normalize Binance Spot millisecond / microsecond timestamps."""

from __future__ import annotations

from datetime import datetime, timezone

from research_lab.historical.constants import TIMESTAMP_UNIT_SWITCH


def detect_unit(raw: int | float, *, hint_open: datetime | None = None) -> str:
    """Return 'ms' or 'us'. Prefer magnitude; hint uses known switch day when ambiguous."""
    value = int(raw)
    digits = len(str(abs(value)))
    if digits >= 16:
        return "us"
    if digits <= 13:
        return "ms"
    # 14–15 digits: rare; use calendar hint if provided.
    if hint_open is not None and hint_open >= TIMESTAMP_UNIT_SWITCH:
        return "us"
    if hint_open is not None and hint_open < TIMESTAMP_UNIT_SWITCH:
        return "ms"
    # Default: treat 14–15 as microseconds if >= 1e14.
    return "us" if value >= 10**14 else "ms"


def to_utc_datetime(raw: int | float, *, unit: str | None = None) -> datetime:
    value = int(raw)
    resolved = unit or detect_unit(value)
    if resolved == "us":
        seconds = value / 1_000_000
    elif resolved == "ms":
        seconds = value / 1_000
    else:
        raise ValueError(f"Unsupported timestamp unit: {resolved}")
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def expected_unit_for_day(day: datetime) -> str:
    day_utc = day.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return "us" if day_utc >= TIMESTAMP_UNIT_SWITCH else "ms"
