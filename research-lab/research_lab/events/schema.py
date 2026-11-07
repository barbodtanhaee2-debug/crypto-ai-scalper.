"""Versioned research event schema for Stage 3.2.8.

Events are research-lab diagnostics only. They do not alter strategy or broker
behavior and are not equivalent to live order-book execution records.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

SCHEMA_VERSION = "1.1.0"
SYMBOL_DEFAULT = "BTCUSDT"

EVENT_TYPES = frozenset(
    {
        "strategy_decision",
        "entry_signal",
        "entry_pending",
        "entry_compatibility_check",
        "entry_cancelled_incompatible",
        "entry_cancelled_missing_candle",
        "entry_rejected",
        "entry_filled",
        "position_exit_signal",
        "exit_rejected",
        "exit_cancelled_missing_candle",
        "position_closed",
        "risk_limit_rejected",
        "run_completed",
        "run_failed",
    }
)

# Events that must carry a non-null lifecycle_id when present in a complete stream.
LIFECYCLE_REQUIRED_TYPES = frozenset(
    {
        "entry_signal",
        "entry_pending",
        "entry_compatibility_check",
        "entry_cancelled_incompatible",
        "entry_cancelled_missing_candle",
        "entry_rejected",
        "entry_filled",
        "position_exit_signal",
        "exit_rejected",
        "exit_cancelled_missing_candle",
        "risk_limit_rejected",
    }
)

RISK_LIMIT_REASON_MARKERS = (
    "Maximum drawdown limit reached",
    "Daily-loss limit reached",
    "Position sizing exceeds the configured stop-and-cost risk budget",
)


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamps must be timezone-aware UTC.")
    return value.astimezone(timezone.utc).isoformat()


def make_event_id(*, run_id: str, arm: str, sequence: int, event_type: str) -> str:
    """Stable ID from run/arm/sequence/type (no wall-clock)."""

    raw = f"{run_id}|{arm}|{sequence:012d}|{event_type}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{arm}-{sequence:012d}-{digest}"


def is_risk_limit_reason(reason: str | None) -> bool:
    text = str(reason or "")
    return any(marker in text for marker in RISK_LIMIT_REASON_MARKERS)


@dataclass(frozen=True)
class ResearchEvent:
    schema_version: str
    run_id: str
    arm: str
    event_id: str
    sequence: int
    event_type: str
    event_time: str | None
    candle_open_time: str | None
    signal_time: str | None
    signal_price: float | None
    symbol: str
    provenance: str
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "arm": self.arm,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "event_time": self.event_time,
            "candle_open_time": self.candle_open_time,
            "signal_time": self.signal_time,
            "signal_price": self.signal_price,
            "symbol": self.symbol,
            "provenance": self.provenance,
            "payload": dict(self.payload),
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), allow_nan=False, sort_keys=True)

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "ResearchEvent":
        return ResearchEvent(
            schema_version=str(raw.get("schema_version")),
            run_id=str(raw.get("run_id")),
            arm=str(raw.get("arm")),
            event_id=str(raw.get("event_id")),
            sequence=int(raw["sequence"]),  # type: ignore[arg-type]
            event_type=str(raw.get("event_type")),
            event_time=raw.get("event_time"),  # type: ignore[arg-type]
            candle_open_time=raw.get("candle_open_time"),  # type: ignore[arg-type]
            signal_time=raw.get("signal_time"),  # type: ignore[arg-type]
            signal_price=(
                None
                if raw.get("signal_price") is None
                else float(raw["signal_price"])  # type: ignore[arg-type]
            ),
            symbol=str(raw.get("symbol")),
            provenance=str(raw.get("provenance")),
            payload=dict(raw.get("payload") or {}),
        )


def validate_event_dict(raw: Mapping[str, Any]) -> list[str]:
    """Return a list of schema problems (empty means structurally valid)."""

    errors: list[str] = []
    if raw.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"unsupported schema_version={raw.get('schema_version')!r}")
    for key in (
        "run_id",
        "arm",
        "event_id",
        "sequence",
        "event_type",
        "symbol",
        "provenance",
        "payload",
    ):
        if key not in raw:
            errors.append(f"missing field {key}")
    event_type = raw.get("event_type")
    if event_type not in EVENT_TYPES:
        errors.append(f"unknown event_type={event_type!r}")
    try:
        sequence = int(raw["sequence"])  # type: ignore[index]
        if sequence < 1:
            errors.append("sequence must be >= 1")
    except (KeyError, TypeError, ValueError):
        errors.append("sequence must be a positive integer")
    if not isinstance(raw.get("payload"), Mapping):
        errors.append("payload must be an object")
    for ts_key in ("event_time", "candle_open_time", "signal_time"):
        value = raw.get(ts_key)
        if value is None:
            continue
        if not isinstance(value, str):
            errors.append(f"{ts_key} must be ISO-8601 string or null")
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            errors.append(f"{ts_key} is not valid ISO-8601")
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            errors.append(f"{ts_key} must include timezone")
    return errors
