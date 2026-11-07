"""Isolated public BTCUSDT Spot quote-tape collector. No broker or orders."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import signal
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from dashboard_data import load_runtime_status
from market_data import (
    BINANCE_BOOK_TICKER_URL,
    binance_kline_stream_url,
)

logger = logging.getLogger(__name__)

SYMBOL = "BTCUSDT"
CANDLE_INTERVAL_SECONDS = 60
DEFAULT_OUTPUT_ROOT = Path("quote-tape") / "sessions"
FORBIDDEN_OUTPUT_NAMES = frozenset(
    {"paper_broker.jsonl", "runtime_status.json"}
)
PUBLIC_ENDPOINTS = (
    BINANCE_BOOK_TICKER_URL,
    binance_kline_stream_url(CANDLE_INTERVAL_SECONDS),
)
MAX_QUOTES_FILE_BYTES = 20_000_000
DIAGNOSTIC_RAW_LIMIT = 2048
STATUS_POLL_SECONDS = 5.0
SESSION_METADATA_INTERVAL_SECONDS = 5.0
SESSION_REPLACE_ATTEMPTS = 8
SESSION_REPLACE_BACKOFF_SECONDS = 0.05


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("Timestamp must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _aware_utc(value).isoformat()


def resolve_session_directory(path: str | Path) -> Path:
    """Refuse live paper journals; sessions live under a dedicated tree."""
    directory = Path(path).expanduser().resolve()
    if directory.name in FORBIDDEN_OUTPUT_NAMES:
        raise ValueError("Collector output must not use the live paper journal.")
    return directory


class JsonlSink:
    """Append-only JSONL with explicit flush; rotates oversized quote files."""

    def __init__(self, path: Path, *, max_bytes: int | None = None) -> None:
        self.base_path = path
        self.max_bytes = max_bytes
        self.part = 0
        self._handle = None
        self._bytes = 0
        self._unsynced = 0
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._open_current()

    def _open_current(self) -> None:
        if self._handle is not None:
            self._handle.close()
        self.path = (
            self.base_path
            if self.part == 0
            else self.base_path.with_name(
                f"{self.base_path.stem}.part{self.part}{self.base_path.suffix}"
            )
        )
        existing = self.path.stat().st_size if self.path.exists() else 0
        self._handle = self.path.open("a", encoding="utf-8")
        self._bytes = existing
        self._unsynced = 0

    def append(self, record: Mapping[str, object]) -> None:
        if self._handle is None:
            raise RuntimeError("JSONL sink is closed.")
        line = json.dumps(dict(record), allow_nan=False, separators=(",", ":"))
        encoded = line + "\n"
        size = len(encoded.encode("utf-8"))
        if (
            self.max_bytes is not None
            and self._bytes > 0
            and self._bytes + size > self.max_bytes
        ):
            self.flush()
            self.part += 1
            self._open_current()
        self._handle.write(encoded)
        self._handle.flush()
        self._bytes += size
        self._unsynced += 1
        if self._unsynced >= 25:
            os.fsync(self._handle.fileno())
            self._unsynced = 0

    def flush(self) -> None:
        if self._handle is None:
            return
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        if self._handle is None:
            return
        self.flush()
        self._handle.close()
        self._handle = None


def write_session_json(path: Path, payload: Mapping[str, object]) -> None:
    """Replace session.json atomically so readers never see a partial object."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(dict(payload), temporary, indent=2, allow_nan=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        last_error: Exception | None = None
        for attempt in range(1, SESSION_REPLACE_ATTEMPTS + 1):
            try:
                os.replace(temporary_name, destination)
                last_error = None
                break
            except PermissionError as error:
                last_error = error
                if attempt >= SESSION_REPLACE_ATTEMPTS:
                    break
                time.sleep(SESSION_REPLACE_BACKOFF_SECONDS * attempt)
        if last_error is not None:
            raise last_error
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _raw_preview(raw: object) -> str:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    if len(text) > DIAGNOSTIC_RAW_LIMIT:
        return text[:DIAGNOSTIC_RAW_LIMIT] + "..."
    return text


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_ms_timestamp(value: object) -> datetime | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _optional_positive_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or result <= 0:
        return None
    return result


def _optional_nonnegative_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or result < 0:
        return None
    return result


def parse_book_ticker(
    raw_message: str | bytes,
    *,
    received_at: datetime,
) -> dict[str, object]:
    """Parse one public bookTicker payload without inventing missing fields."""
    received = _aware_utc(received_at)
    record: dict[str, object] = {
        "record_type": "quote",
        "received_at_utc": received.isoformat(),
        "symbol": None,
        "bid": None,
        "ask": None,
        "bid_quantity": None,
        "ask_quantity": None,
        "bid_raw": None,
        "ask_raw": None,
        "bid_quantity_raw": None,
        "ask_quantity_raw": None,
        "update_id": None,
        "exchange_event_timestamp": None,
        "spread": None,
        "spread_percent": None,
        "event_age_seconds": None,
        "accepted": False,
        "rejection": None,
    }
    try:
        message = json.loads(raw_message)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
        record["rejection"] = f"Malformed JSON message: {error}."
        record["raw_preview"] = _raw_preview(raw_message)
        return record
    if not isinstance(message, dict):
        record["rejection"] = "Message must be a JSON object."
        record["raw_preview"] = _raw_preview(raw_message)
        return record
    payload = message.get("data", message)
    if not isinstance(payload, dict):
        record["rejection"] = "Message data must be a JSON object."
        record["raw_preview"] = _raw_preview(raw_message)
        return record

    symbol = payload.get("s")
    record["symbol"] = symbol if isinstance(symbol, str) else None
    if symbol != SYMBOL:
        record["rejection"] = "Message symbol is not BTCUSDT."
        record["raw_preview"] = _raw_preview(raw_message)
        return record

    record["bid_raw"] = payload.get("b")
    record["ask_raw"] = payload.get("a")
    record["bid_quantity_raw"] = payload.get("B")
    record["ask_quantity_raw"] = payload.get("A")
    record["update_id"] = _optional_int(payload.get("u"))
    event_time = _optional_ms_timestamp(payload.get("E")) if "E" in payload else None
    if "E" in payload and event_time is None:
        record["rejection"] = "Exchange event timestamp is invalid."
        record["raw_preview"] = _raw_preview(raw_message)
        return record
    record["exchange_event_timestamp"] = _iso(event_time)
    if event_time is not None:
        record["event_age_seconds"] = (received - event_time).total_seconds()

    bid = _optional_positive_float(payload.get("b"))
    ask = _optional_positive_float(payload.get("a"))
    record["bid"] = bid
    record["ask"] = ask
    record["bid_quantity"] = _optional_nonnegative_float(payload.get("B"))
    record["ask_quantity"] = _optional_nonnegative_float(payload.get("A"))
    if bid is None or ask is None:
        record["rejection"] = "Bid or ask is missing, zero, negative, or non-numeric."
        record["raw_preview"] = _raw_preview(raw_message)
        return record
    if ask <= bid:
        record["rejection"] = "Ask must be greater than bid."
        record["raw_preview"] = _raw_preview(raw_message)
        return record
    spread = ask - bid
    mid = (bid + ask) / 2
    record["spread"] = spread
    record["spread_percent"] = (spread / mid) * 100 if mid > 0 else None
    record["accepted"] = True
    return record


def parse_kline(
    raw_message: str | bytes,
    *,
    received_at: datetime,
    interval_seconds: int = CANDLE_INTERVAL_SECONDS,
) -> dict[str, object]:
    """Parse one public kline; completed candles only when x is true."""
    received = _aware_utc(received_at)
    record: dict[str, object] = {
        "record_type": "candle",
        "received_at_utc": received.isoformat(),
        "symbol": None,
        "interval_seconds": interval_seconds,
        "open_time_utc": None,
        "close_time_utc": None,
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "base_volume": None,
        "quote_volume": None,
        "is_closed": None,
        "kline_open_time_ms": None,
        "kline_close_time_ms": None,
        "exchange_event_timestamp": None,
        "event_age_seconds": None,
        "accepted": False,
        "rejection": None,
    }
    try:
        message = json.loads(raw_message)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
        record["rejection"] = f"Malformed JSON message: {error}."
        record["raw_preview"] = _raw_preview(raw_message)
        return record
    if not isinstance(message, dict):
        record["rejection"] = "Message must be a JSON object."
        record["raw_preview"] = _raw_preview(raw_message)
        return record
    payload = message.get("data", message)
    if not isinstance(payload, dict):
        record["rejection"] = "Message data must be a JSON object."
        record["raw_preview"] = _raw_preview(raw_message)
        return record
    kline = payload.get("k")
    if not isinstance(kline, dict):
        record["rejection"] = "Message does not contain a kline."
        record["raw_preview"] = _raw_preview(raw_message)
        return record
    payload_symbol = payload.get("s") if isinstance(payload.get("s"), str) else None
    kline_symbol = kline.get("s") if isinstance(kline.get("s"), str) else None
    if (
        payload_symbol is not None
        and kline_symbol is not None
        and payload_symbol != kline_symbol
    ):
        record["symbol"] = kline_symbol
        record["rejection"] = "Kline symbol does not match message symbol."
        record["raw_preview"] = _raw_preview(raw_message)
        return record
    symbol = kline_symbol or payload_symbol
    record["symbol"] = symbol
    if symbol != SYMBOL:
        record["rejection"] = "Message symbol is not BTCUSDT."
        record["raw_preview"] = _raw_preview(raw_message)
        return record
    expected_label = f"{interval_seconds // 60}m"
    if kline.get("i") != expected_label:
        record["rejection"] = "Kline interval is unexpected."
        record["raw_preview"] = _raw_preview(raw_message)
        return record

    open_time = _optional_ms_timestamp(kline.get("t"))
    close_time = _optional_ms_timestamp(kline.get("T"))
    record["open_time_utc"] = _iso(open_time)
    record["close_time_utc"] = _iso(close_time)
    is_closed = kline.get("x")
    record["is_closed"] = is_closed if isinstance(is_closed, bool) else None
    record["open"] = _optional_positive_float(kline.get("o"))
    record["high"] = _optional_positive_float(kline.get("h"))
    record["low"] = _optional_positive_float(kline.get("l"))
    record["close"] = _optional_positive_float(kline.get("c"))
    record["base_volume"] = _optional_nonnegative_float(kline.get("v"))
    record["quote_volume"] = _optional_nonnegative_float(kline.get("q"))

    if record["is_closed"] is not True:
        if record["is_closed"] is False:
            record["rejection"] = "Candle is still in progress."
        else:
            record["rejection"] = "Kline closed flag is invalid."
            record["raw_preview"] = _raw_preview(raw_message)
        return record

    interval_ms = interval_seconds * 1000
    open_ms = kline.get("t")
    close_ms = kline.get("T")
    if (
        not isinstance(open_ms, int)
        or isinstance(open_ms, bool)
        or not isinstance(close_ms, int)
        or isinstance(close_ms, bool)
        or open_ms % interval_ms != 0
        or close_ms != open_ms + interval_ms - 1
        or open_time is None
        or close_time is None
    ):
        record["rejection"] = "Kline timestamps do not match the interval."
        record["raw_preview"] = _raw_preview(raw_message)
        return record

    open_price = record["open"]
    high = record["high"]
    low = record["low"]
    close = record["close"]
    if None in (open_price, high, low, close):
        record["rejection"] = "Kline OHLC values are invalid."
        record["raw_preview"] = _raw_preview(raw_message)
        return record
    if low > min(open_price, close) or high < max(open_price, close) or high < low:
        record["rejection"] = "Kline OHLC values are inconsistent."
        record["raw_preview"] = _raw_preview(raw_message)
        return record

    record["kline_open_time_ms"] = open_ms
    record["kline_close_time_ms"] = close_ms
    if "E" in payload:
        event_time = _optional_ms_timestamp(payload.get("E"))
        if event_time is None:
            record["rejection"] = "Exchange event timestamp is invalid."
            record["raw_preview"] = _raw_preview(raw_message)
            return record
        record["exchange_event_timestamp"] = _iso(event_time)
        record["event_age_seconds"] = (received - event_time).total_seconds()
        record["event_minus_close_seconds"] = (
            event_time - close_time
        ).total_seconds()
    record["close_age_seconds"] = (received - close_time).total_seconds()
    record["accepted"] = True
    return record


@dataclass
class CollectorCounts:
    quotes_received: int = 0
    quotes_accepted: int = 0
    quotes_rejected: int = 0
    quotes_duplicate: int = 0
    quotes_out_of_order: int = 0
    candles_received: int = 0
    candles_accepted: int = 0
    candles_in_progress: int = 0
    candles_rejected: int = 0
    candles_duplicate: int = 0
    candles_missing_interval: int = 0
    reconnects: int = 0
    session_gaps: int = 0
    session_metadata_write_failures: int = 0


@dataclass
class QuoteTapeCollector:
    output_dir: Path
    session_id: str
    clock: Callable[[], datetime] = field(default=_utc_now)
    runtime_status_path: Path | None = None
    max_reconnect_attempts: int = 1000
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    quotes_max_bytes: int = MAX_QUOTES_FILE_BYTES
    session_metadata_interval_seconds: float = SESSION_METADATA_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        self.output_dir = resolve_session_directory(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.counts = CollectorCounts()
        self._quotes = JsonlSink(
            self.output_dir / "quotes.jsonl",
            max_bytes=self.quotes_max_bytes,
        )
        self._candles = JsonlSink(self.output_dir / "candles.jsonl")
        self._events = JsonlSink(self.output_dir / "events.jsonl")
        self._diagnostics = JsonlSink(self.output_dir / "diagnostics.jsonl")
        self._positions = JsonlSink(self.output_dir / "position_snapshots.jsonl")
        self._last_update_id: int | None = None
        self._last_candle_open_ms: int | None = None
        self._quote_gap_started: datetime | None = None
        self._kline_gap_started: datetime | None = None
        self._closed = False
        self._session_status = "starting"
        self._last_session_write_at: datetime | None = None
        self._write_session_metadata(status="starting", force=True)

    def _write_session_metadata(self, *, status: str, force: bool = False, **extra: object) -> None:
        now = self.clock()
        if not force and self._last_session_write_at is not None:
            elapsed = (now - self._last_session_write_at).total_seconds()
            interval = self.session_metadata_interval_seconds
            if interval > 0 and elapsed < interval:
                return
        self._session_status = status
        payload = {
            "session_id": self.session_id,
            "status": status,
            "symbol": SYMBOL,
            "market": "Binance Spot",
            "authenticated": False,
            "credentials_used": False,
            "order_endpoints": [],
            "public_endpoints": list(PUBLIC_ENDPOINTS),
            "candle_interval_seconds": CANDLE_INTERVAL_SECONDS,
            "output_dir": str(self.output_dir),
            "written_at_utc": _iso(now),
            "quotes_written": self.counts.quotes_accepted,
            "candles_written": self.counts.candles_accepted,
            "counts": dict(self.counts.__dict__),
            "retention": {
                "quotes_rotate_after_bytes": self.quotes_max_bytes,
                "policy": (
                    "quotes.jsonl rotates to quotes.partN.jsonl after "
                    f"{self.quotes_max_bytes} bytes. Sessions are not deleted "
                    "automatically; operators should archive or remove old "
                    "session directories."
                ),
            },
            **extra,
        }
        try:
            write_session_json(self.output_dir / "session.json", payload)
            self._last_session_write_at = now
        except OSError as error:
            self.counts.session_metadata_write_failures += 1
            self._diagnostic(
                "session_metadata_write_failed",
                {
                    "accepted": False,
                    "rejection": f"{type(error).__name__}: {error}",
                    "status_requested": status,
                    "quotes_accepted": self.counts.quotes_accepted,
                    "candles_accepted": self.counts.candles_accepted,
                    "forced": force,
                },
            )
            logger.warning(
                "Session metadata write failed without disconnecting streams: %s",
                error,
            )

    def _event(self, name: str, **details: object) -> None:
        self._events.append(
            {
                "record_type": "event",
                "event": name,
                "session_id": self.session_id,
                "timestamp_utc": _iso(self.clock()),
                **details,
            }
        )

    def _diagnostic(self, kind: str, record: Mapping[str, object]) -> None:
        self._diagnostics.append(
            {
                "record_type": "diagnostic",
                "kind": kind,
                "session_id": self.session_id,
                "timestamp_utc": _iso(self.clock()),
                **dict(record),
            }
        )

    def ingest_quote(self, raw_message: str | bytes) -> dict[str, object]:
        self.counts.quotes_received += 1
        parsed = parse_book_ticker(raw_message, received_at=self.clock())
        parsed["session_id"] = self.session_id
        if not parsed["accepted"]:
            self.counts.quotes_rejected += 1
            self._diagnostic("rejected_quote", parsed)
            return parsed
        update_id = parsed.get("update_id")
        if isinstance(update_id, int) and self._last_update_id is not None:
            if update_id == self._last_update_id:
                self.counts.quotes_duplicate += 1
                parsed["ordering"] = "duplicate"
                self._diagnostic("duplicate_quote", parsed)
                return parsed
            if update_id < self._last_update_id:
                self.counts.quotes_out_of_order += 1
                parsed["ordering"] = "out_of_order"
                self._diagnostic("out_of_order_quote", parsed)
        if isinstance(update_id, int):
            self._last_update_id = update_id
        self.counts.quotes_accepted += 1
        self._quotes.append(parsed)
        self._write_session_metadata(status="running")
        return parsed

    def ingest_kline(self, raw_message: str | bytes) -> dict[str, object]:
        self.counts.candles_received += 1
        parsed = parse_kline(raw_message, received_at=self.clock())
        parsed["session_id"] = self.session_id
        if parsed.get("is_closed") is False:
            self.counts.candles_in_progress += 1
            return parsed
        if not parsed["accepted"]:
            self.counts.candles_rejected += 1
            self._diagnostic("rejected_candle", parsed)
            return parsed
        open_ms = None
        try:
            open_dt = datetime.fromisoformat(str(parsed["open_time_utc"]))
            open_ms = int(open_dt.timestamp() * 1000)
        except (TypeError, ValueError, OverflowError):
            open_ms = None
        if open_ms is not None and self._last_candle_open_ms is not None:
            expected = self._last_candle_open_ms + CANDLE_INTERVAL_SECONDS * 1000
            if open_ms == self._last_candle_open_ms:
                self.counts.candles_duplicate += 1
                parsed["ordering"] = "duplicate"
                self._diagnostic("duplicate_candle", parsed)
                return parsed
            if open_ms != expected:
                self.counts.candles_missing_interval += 1
                parsed["ordering"] = "missing_interval"
                parsed["expected_open_time_utc"] = datetime.fromtimestamp(
                    expected / 1000, tz=timezone.utc
                ).isoformat()
                self._diagnostic("missing_candle_interval", parsed)
        if open_ms is not None:
            self._last_candle_open_ms = open_ms
        self.counts.candles_accepted += 1
        self._candles.append(parsed)
        self._candles.flush()
        self._write_session_metadata(status="running", force=True)
        return parsed

    def observe_runtime_status(self) -> dict[str, object] | None:
        """Read-only snapshot of paper status; never infers a position."""
        if self.runtime_status_path is None:
            return None
        status = load_runtime_status(self.runtime_status_path, now=self.clock())
        snapshot = {
            "record_type": "position_snapshot",
            "session_id": self.session_id,
            "observed_at_utc": _iso(self.clock()),
            "source_path": str(self.runtime_status_path),
            "status_exists": status.exists,
            "process_running": status.process_running,
            "heartbeat_age_seconds": status.heartbeat_age_seconds,
            "open_position": None,
            "position_state_available": False,
        }
        if status.process_running is True and isinstance(status.data, dict):
            flag = status.data.get("open_position")
            if isinstance(flag, bool):
                snapshot["open_position"] = flag
                snapshot["position_state_available"] = True
        self._positions.append(snapshot)
        return snapshot

    def note_disconnect(self, stream: str, reason: str) -> None:
        started = self.clock()
        if stream == "quote":
            self._quote_gap_started = started
        else:
            self._kline_gap_started = started
        self._event("disconnect", stream=stream, reason=reason)

    def note_reconnect(self, stream: str) -> None:
        now = self.clock()
        started = (
            self._quote_gap_started if stream == "quote" else self._kline_gap_started
        )
        gap_seconds = (now - started).total_seconds() if started is not None else None
        self.counts.reconnects += 1
        if gap_seconds is not None:
            self.counts.session_gaps += 1
        self._event(
            "reconnect",
            stream=stream,
            gap_seconds=gap_seconds,
            gap_started_utc=_iso(started),
            recovered_at_utc=_iso(now),
        )
        if stream == "quote":
            self._quote_gap_started = None
        else:
            self._kline_gap_started = None

    def flush(self) -> None:
        for sink in (
            self._quotes,
            self._candles,
            self._events,
            self._diagnostics,
            self._positions,
        ):
            sink.flush()
        self._write_session_metadata(status=self._session_status, force=True)

    def close(self) -> None:
        if self._closed:
            return
        self._event("shutdown")
        self.flush()
        ended = _iso(self.clock())
        self._write_session_metadata(
            status="stopped",
            force=True,
            ended_at_utc=ended,
        )
        session_path = self.output_dir / "session.json"
        if session_path.exists():
            try:
                payload = json.loads(session_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if payload.get("status") != "stopped":
                for _attempt in range(3):
                    try:
                        write_session_json(
                            session_path,
                            {
                                **payload,
                                "status": "stopped",
                                "ended_at_utc": ended,
                                "written_at_utc": ended,
                            },
                        )
                        break
                    except OSError as error:
                        logger.warning(
                            "Retrying stopped session.json write: %s", error
                        )
                        time.sleep(SESSION_REPLACE_BACKOFF_SECONDS)
        for sink in (
            self._quotes,
            self._candles,
            self._events,
            self._diagnostics,
            self._positions,
        ):
            sink.close()
        self._closed = True

    async def _run_stream(
        self,
        *,
        name: str,
        url: str,
        ingest: Callable[[str | bytes], object],
        connector,
        sleep: Callable[[float], object],
        stop_event: asyncio.Event,
    ) -> None:
        try:
            from websockets.exceptions import WebSocketException
        except ImportError:
            WebSocketException = OSError

        reconnects = 0
        backoff = self.base_backoff_seconds
        connected_once = False
        while not stop_event.is_set():
            try:
                async with connector(
                    url,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                ) as websocket:
                    if connected_once:
                        self.note_reconnect(name)
                    connected_once = True
                    self._event("connected", stream=name, url=url)
                    async for raw_message in websocket:
                        if stop_event.is_set():
                            break
                        ingest(raw_message)
                        if self.counts.quotes_accepted % 200 == 0:
                            self._quotes.flush()
                    if stop_event.is_set():
                        break
                    raise ConnectionError(f"{name} WebSocket closed.")
            except asyncio.CancelledError:
                self.note_disconnect(name, "Feed task cancelled.")
                raise
            except (OSError, TimeoutError, ConnectionError, WebSocketException) as error:
                self.note_disconnect(
                    name, f"{type(error).__name__}: {error}"
                )
                if stop_event.is_set():
                    break
                if reconnects >= self.max_reconnect_attempts:
                    self._event("reconnect_exhausted", stream=name)
                    break
                reconnects += 1
                await sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff_seconds)

    async def run(
        self,
        *,
        quote_connector=None,
        kline_connector=None,
        sleep: Callable[[float], object] = asyncio.sleep,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        stop_event = stop_event or asyncio.Event()
        if quote_connector is None or kline_connector is None:
            try:
                from websockets.asyncio.client import connect
            except ImportError as error:
                raise RuntimeError(
                    "Install project dependencies to collect public Binance streams."
                ) from error
            quote_connector = quote_connector or connect
            kline_connector = kline_connector or connect

        self._event("session_start", public_endpoints=list(PUBLIC_ENDPOINTS))
        self.flush()

        async def poll_status() -> None:
            if self.runtime_status_path is None:
                return
            while not stop_event.is_set():
                try:
                    self.observe_runtime_status()
                except (OSError, ValueError) as error:
                    self._diagnostic(
                        "status_read_error",
                        {"rejection": str(error), "accepted": False},
                    )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=STATUS_POLL_SECONDS)
                except asyncio.TimeoutError:
                    continue

        tasks = [
            asyncio.create_task(
                self._run_stream(
                    name="quote",
                    url=BINANCE_BOOK_TICKER_URL,
                    ingest=self.ingest_quote,
                    connector=quote_connector,
                    sleep=sleep,
                    stop_event=stop_event,
                ),
                name="quote-tape-bookTicker",
            ),
            asyncio.create_task(
                self._run_stream(
                    name="kline",
                    url=binance_kline_stream_url(CANDLE_INTERVAL_SECONDS),
                    ingest=self.ingest_kline,
                    connector=kline_connector,
                    sleep=sleep,
                    stop_event=stop_event,
                ),
                name="quote-tape-kline",
            ),
            asyncio.create_task(poll_status(), name="quote-tape-status"),
        ]
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            stop_event.set()
            raise
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.close()


async def _run_from_cli(args: argparse.Namespace) -> None:
    stop_event = asyncio.Event()
    previous_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda *_: stop_event.set())
        except (ValueError, OSError):
            logger.warning("Could not register signal handler for %s.", sig)

    started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session_id = f"{started}-{uuid.uuid4().hex[:8]}"
    output = Path(args.output_root) / session_id
    collector = QuoteTapeCollector(
        output_dir=output,
        session_id=session_id,
        runtime_status_path=(
            Path(args.runtime_status) if args.runtime_status else None
        ),
        max_reconnect_attempts=args.max_reconnect_attempts,
    )
    try:
        await collector.run(stop_event=stop_event)
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="Directory that will contain a new session subdirectory.",
    )
    parser.add_argument(
        "--runtime-status",
        default="",
        help="Optional read-only runtime_status.json path. Not written.",
    )
    parser.add_argument("--max-reconnect-attempts", type=int, default=1000)
    args = parser.parse_args()
    if not args.runtime_status:
        args.runtime_status = None
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_run_from_cli(args))
    except KeyboardInterrupt:
        logger.info("Quote-tape collector stopped by keyboard interrupt.")


if __name__ == "__main__":
    main()
