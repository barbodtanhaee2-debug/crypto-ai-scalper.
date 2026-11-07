"""UTC timestamp helpers for adaptive contracts."""

from __future__ import annotations

from datetime import datetime, timezone


def require_utc(value: datetime, *, field: str = "timestamp") -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def utc_iso(value: datetime) -> str:
    return require_utc(value).isoformat()


def parse_utc_iso(value: str, *, field: str = "timestamp") -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} is not valid ISO-8601") from error
    return require_utc(parsed, field=field)
