from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_LAB = Path(__file__).resolve().parents[1]
_PROJECT = _LAB.parent
for _path in (_LAB, _PROJECT):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)

from quote_tape_collector import write_session_json


def write_session(root: Path, quotes, candles, *, name="session"):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    write_session_json(
        directory / "session.json",
        {
            "session_id": name,
            "status": "stopped",
            "symbol": "BTCUSDT",
            "started_at_utc": quotes[0]["received_at_utc"] if quotes else None,
            "ended_at_utc": quotes[-1]["received_at_utc"] if quotes else None,
        },
    )
    (directory / "quotes.jsonl").write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in quotes),
        encoding="utf-8",
    )
    (directory / "candles.jsonl").write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in candles),
        encoding="utf-8",
    )
    (directory / "events.jsonl").write_text("", encoding="utf-8")
    return directory


def quote(start: datetime, index: int, *, bid=100.0, ask=100.02, update_id=None):
    received = start + timedelta(seconds=index)
    return {
        "record_type": "quote",
        "received_at_utc": received.isoformat(),
        "symbol": "BTCUSDT",
        "bid": bid,
        "ask": ask,
        "spread": ask - bid,
        "update_id": index if update_id is None else update_id,
        "bid_quantity": 1.0,
        "ask_quantity": 1.0,
        "bid_raw": str(bid),
        "ask_raw": str(ask),
        "accepted": True,
        "exchange_event_timestamp": None,
    }


def candle(
    start: datetime,
    minute: int,
    *,
    close=100.0,
    high=None,
    low=None,
    e_lag_ms=15,
    receive_lead_ms=50,
):
    open_time = start + timedelta(minutes=minute)
    close_time = open_time + timedelta(milliseconds=59_999)
    received = close_time - timedelta(milliseconds=receive_lead_ms)
    event = close_time + timedelta(milliseconds=e_lag_ms)
    high = close + 0.2 if high is None else high
    low = close - 0.2 if low is None else low
    return {
        "record_type": "candle",
        "received_at_utc": received.isoformat(),
        "symbol": "BTCUSDT",
        "open_time_utc": open_time.isoformat(),
        "close_time_utc": close_time.isoformat(),
        "exchange_event_timestamp": event.isoformat(),
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "base_volume": 1.0,
        "quote_volume": 100.0,
        "is_closed": True,
        "accepted": True,
    }
