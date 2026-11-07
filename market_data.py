"""Public Binance Spot BTC/USDT best-bid/ask feed with fail-closed health."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlencode
from urllib.request import urlopen

from strategy import Bar

BINANCE_BOOK_TICKER_URL = (
    "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"
)
BINANCE_KLINE_STREAM_URL = (
    "wss://stream.binance.com:9443/ws/btcusdt@kline_"
)
BINANCE_KLINE_URL = (
    "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"
)
BINANCE_KLINE_REST_URL = "https://api.binance.com/api/v3/klines"
BINANCE_SERVER_TIME_URL = "https://api.binance.com/api/v3/time"
SUPPORTED_KLINE_INTERVALS = {
    60: "1m",
    180: "3m",
    300: "5m",
    900: "15m",
    1800: "30m",
}
logger = logging.getLogger(__name__)
MAX_BINANCE_CLOCK_OFFSET_SECONDS = 5.0
MAX_BINANCE_CLOCK_SAMPLE_ROUND_TRIP_SECONDS = 2.0
MAX_BINANCE_CLOCK_SAMPLE_AGE_SECONDS = 120.0
BINANCE_CLOCK_RESYNC_INTERVAL_SECONDS = 30.0
MAX_BINANCE_CLOCK_WALL_JUMP_SECONDS = 0.5


def binance_kline_interval_label(interval_seconds: int) -> str:
    """Return a supported Binance Spot kline label for a duration in seconds."""
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int):
        raise ValueError("Kline interval must be an integer number of seconds.")
    try:
        return SUPPORTED_KLINE_INTERVALS[interval_seconds]
    except KeyError as error:
        supported = ", ".join(SUPPORTED_KLINE_INTERVALS.values())
        raise ValueError(
            f"Unsupported Binance kline interval: {interval_seconds} seconds. "
            f"Supported intervals: {supported}."
        ) from error


def binance_kline_stream_url(interval_seconds: int) -> str:
    return BINANCE_KLINE_STREAM_URL + binance_kline_interval_label(interval_seconds)


@dataclass(frozen=True)
class MarketDataSettings:
    symbol: str = "BTCUSDT"
    stale_after_seconds: float = 3.0
    max_reconnect_attempts: int = 5
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.symbol != "BTCUSDT":
            raise ValueError("This feed is restricted to BTCUSDT.")
        if (
            not math.isfinite(self.stale_after_seconds)
            or self.stale_after_seconds <= 0
        ):
            raise ValueError("stale_after_seconds must be positive.")
        if self.max_reconnect_attempts < 0:
            raise ValueError("max_reconnect_attempts cannot be negative.")
        if (
            not math.isfinite(self.base_backoff_seconds)
            or self.base_backoff_seconds <= 0
        ):
            raise ValueError("base_backoff_seconds must be positive.")
        if (
            not math.isfinite(self.max_backoff_seconds)
            or self.max_backoff_seconds < self.base_backoff_seconds
        ):
            raise ValueError(
                "max_backoff_seconds must be at least the base backoff."
            )


def _utc_datetime(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("Timestamp must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _price(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{name} must be a numeric price.")
    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be a numeric price.") from error
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and positive.")
    return result


def fetch_binance_server_time(*, timeout: float = 5.0) -> datetime:
    """Fetch Binance's public server clock without credentials."""
    with urlopen(BINANCE_SERVER_TIME_URL, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("Binance server-time response must be an object.")
    timestamp_ms = payload.get("serverTime")
    if (
        isinstance(timestamp_ms, bool)
        or not isinstance(timestamp_ms, int)
        or timestamp_ms <= 0
    ):
        raise ValueError("Binance server-time response has an invalid timestamp.")
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError("Binance server-time timestamp is out of range.") from error


class BinanceServerClock:
    """Provide UTC corrected by a recent, bounded public server-time sample."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        maximum_offset_seconds: float = MAX_BINANCE_CLOCK_OFFSET_SECONDS,
        maximum_sample_age_seconds: float = MAX_BINANCE_CLOCK_SAMPLE_AGE_SECONDS,
        maximum_wall_jump_seconds: float = MAX_BINANCE_CLOCK_WALL_JUMP_SECONDS,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self.maximum_offset_seconds = maximum_offset_seconds
        self.maximum_sample_age_seconds = maximum_sample_age_seconds
        self.maximum_wall_jump_seconds = maximum_wall_jump_seconds
        self.offset_seconds: float | None = None
        self._sampled_wall: datetime | None = None
        self._sampled_monotonic: float | None = None

    def synchronize(
        self,
        server_time_loader: Callable[[], datetime] = fetch_binance_server_time,
    ) -> None:
        self.offset_seconds = None
        self._sampled_wall = None
        self._sampled_monotonic = None
        wall_start = _utc_datetime(self._clock())
        monotonic_start = self._monotonic()
        server_time = _utc_datetime(server_time_loader())
        monotonic_end = self._monotonic()
        wall_end = _utc_datetime(self._clock())
        round_trip = monotonic_end - monotonic_start
        wall_round_trip = (wall_end - wall_start).total_seconds()
        if (
            not math.isfinite(round_trip)
            or round_trip <= 0
            or round_trip > MAX_BINANCE_CLOCK_SAMPLE_ROUND_TRIP_SECONDS
        ):
            raise ValueError(
                "Binance server-time sample latency is invalid or exceeds "
                f"{MAX_BINANCE_CLOCK_SAMPLE_ROUND_TRIP_SECONDS:g} seconds."
            )
        if abs(wall_round_trip - round_trip) > self.maximum_wall_jump_seconds:
            raise ValueError(
                "Host clock changed during Binance time synchronization."
            )
        midpoint = wall_start + (wall_end - wall_start) / 2
        offset = (server_time - midpoint).total_seconds()
        if (
            not math.isfinite(offset)
            or abs(offset) > self.maximum_offset_seconds
        ):
            raise ValueError(
                "Binance server-time offset exceeds the permitted "
                f"{self.maximum_offset_seconds:g}-second bound."
            )
        self.offset_seconds = offset
        self._sampled_wall = midpoint
        self._sampled_monotonic = (monotonic_start + monotonic_end) / 2

    def now(self) -> datetime:
        if (
            self.offset_seconds is None
            or self._sampled_wall is None
            or self._sampled_monotonic is None
        ):
            raise ValueError("Binance server-time calibration is unavailable.")
        current_wall = _utc_datetime(self._clock())
        elapsed = self._monotonic() - self._sampled_monotonic
        if (
            not math.isfinite(elapsed)
            or elapsed < 0
            or elapsed > self.maximum_sample_age_seconds
        ):
            raise ValueError("Binance server-time calibration is stale.")
        wall_elapsed = (current_wall - self._sampled_wall).total_seconds()
        if abs(wall_elapsed - elapsed) > self.maximum_wall_jump_seconds:
            raise ValueError("Host UTC clock changed since Binance time calibration.")
        return current_wall + timedelta(seconds=self.offset_seconds)


class BinanceBookTicker:
    """Consume public best-bid/ask updates; no credentials or order APIs."""

    def __init__(
        self,
        settings: MarketDataSettings | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings or MarketDataSettings()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.connection_status = "disconnected"
        self.bid: float | None = None
        self.ask: float | None = None
        self.spread: float | None = None
        self.message_timestamp: datetime | None = None
        self.exchange_event_timestamp: datetime | None = None
        self.last_valid_update: datetime | None = None
        self.last_error: str | None = "Not connected."
        self._valid_since_connect = False

    def mark_connecting(self) -> None:
        self.connection_status = "connecting"
        self._valid_since_connect = False
        self.last_error = "Waiting for a fresh valid market-data update."

    def mark_connected(self) -> None:
        self.connection_status = "connected"
        self._valid_since_connect = False
        self.last_error = "Waiting for a fresh valid market-data update."

    def mark_reconnecting(self, reason: str) -> None:
        self.connection_status = "reconnecting"
        self._valid_since_connect = False
        self.last_error = reason

    def mark_disconnected(self, reason: str = "Connection closed.") -> None:
        self.connection_status = "disconnected"
        self._valid_since_connect = False
        self.last_error = reason

    def _reject(self, reason: str) -> bool:
        self.last_error = reason
        self._valid_since_connect = False
        logger.warning("Rejected Binance market data: %s", reason)
        return False

    def process_message(self, raw_message: str | bytes) -> bool:
        """Validate and store one raw Binance bookTicker WebSocket message."""
        try:
            message = json.loads(raw_message)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
            return self._reject(f"Malformed JSON message: {error}.")

        if not isinstance(message, dict):
            return self._reject("Message must be a JSON object.")
        payload = message.get("data", message)
        if not isinstance(payload, dict):
            return self._reject("Message data must be a JSON object.")
        if payload.get("s") != self.settings.symbol:
            return self._reject("Message symbol is not BTCUSDT.")

        try:
            bid = _price(payload.get("b"), "bid")
            ask = _price(payload.get("a"), "ask")
            if ask <= bid:
                raise ValueError("Ask must be greater than bid.")
            received_at = _utc_datetime(self._clock())
            event_timestamp = None
            if "E" in payload:
                event_time_ms = payload["E"]
                if (
                    isinstance(event_time_ms, bool)
                    or not isinstance(event_time_ms, (int, float))
                    or not math.isfinite(event_time_ms)
                ):
                    raise ValueError("Exchange event timestamp is invalid.")
                event_timestamp = datetime.fromtimestamp(
                    event_time_ms / 1000, tz=timezone.utc
                )
                event_age = (received_at - event_timestamp).total_seconds()
                if event_age < 0 or event_age > self.settings.stale_after_seconds:
                    raise ValueError(
                        "Exchange event timestamp is future-dated or stale."
                    )
        except (ValueError, OverflowError, OSError) as error:
            return self._reject(str(error))

        self.bid = bid
        self.ask = ask
        self.spread = ask - bid
        self.message_timestamp = received_at
        self.exchange_event_timestamp = event_timestamp
        self.last_valid_update = received_at
        self.last_error = None
        self._valid_since_connect = self.connection_status == "connected"
        return True

    def health(self, now: datetime | None = None) -> dict:
        """Return current feed health and whether new entries are permitted."""
        try:
            observed_at = _utc_datetime(now if now is not None else self._clock())
        except ValueError as error:
            reason = str(error)
        else:
            reason = None

        age_seconds = None
        if reason is None and self.connection_status != "connected":
            reason = f"Feed is {self.connection_status}."
        if reason is None and not self._valid_since_connect:
            reason = self.last_error or (
                "No valid update received since the current connection."
            )
        if reason is None and self.last_valid_update is None:
            reason = "No valid market-data update received."
        if reason is None:
            age_seconds = (
                observed_at - self.last_valid_update
            ).total_seconds()
            if age_seconds < 0:
                reason = "Local clock precedes the last valid update."
            elif age_seconds > self.settings.stale_after_seconds:
                reason = (
                    f"Market data is stale ({age_seconds:.3f}s; "
                    f"limit {self.settings.stale_after_seconds:.3f}s)."
                )
        if reason is None and self.last_error is not None:
            reason = self.last_error

        healthy = reason is None
        return {
            "healthy": healthy,
            "entry_allowed": healthy,
            "connection_status": self.connection_status,
            "reason": "Fresh valid market data." if healthy else reason,
            "bid": self.bid,
            "ask": self.ask,
            "spread": self.spread,
            "message_timestamp": (
                self.message_timestamp.isoformat()
                if self.message_timestamp is not None else None
            ),
            "exchange_event_timestamp": (
                self.exchange_event_timestamp.isoformat()
                if self.exchange_event_timestamp is not None else None
            ),
            "last_valid_update": (
                self.last_valid_update.isoformat()
                if self.last_valid_update is not None else None
            ),
            "age_seconds": age_seconds,
        }

    async def run(
        self,
        *,
        connector=None,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        """Run until reconnect attempts are exhausted or the task is cancelled."""
        if connector is None:
            try:
                from websockets.asyncio.client import connect
                from websockets.exceptions import WebSocketException
            except ImportError as error:
                raise RuntimeError(
                    "Install project dependencies to run the Binance feed."
                ) from error
            connector = connect
        else:
            try:
                from websockets.exceptions import WebSocketException
            except ImportError:
                WebSocketException = OSError

        reconnects = 0
        backoff = self.settings.base_backoff_seconds
        while True:
            self.mark_connecting()
            try:
                async with connector(
                    BINANCE_BOOK_TICKER_URL,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                ) as websocket:
                    self.mark_connected()
                    async for raw_message in websocket:
                        self.process_message(raw_message)
                    raise ConnectionError("WebSocket closed.")
            except asyncio.CancelledError:
                self.mark_disconnected("Feed task cancelled.")
                raise
            except (OSError, TimeoutError, ConnectionError, WebSocketException) as error:
                self.mark_disconnected(
                    f"Connection lost: {type(error).__name__}: {error}"
                )

            if reconnects >= self.settings.max_reconnect_attempts:
                break
            reconnects += 1
            self.mark_reconnecting(
                f"Reconnect {reconnects} of "
                f"{self.settings.max_reconnect_attempts} in {backoff:g}s."
            )
            await sleep(backoff)
            backoff = min(
                backoff * 2,
                self.settings.max_backoff_seconds,
            )

        logger.error(
            "Binance market-data reconnect limit exhausted after %d retries.",
            reconnects,
        )


class BinanceKlineStream:
    """Consume completed public BTC/USDT exchange klines."""

    def __init__(
        self,
        settings: MarketDataSettings | None = None,
        *,
        interval_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
        exchange_clock: Callable[[], datetime] | None = None,
        on_candle: Callable[[bool, Bar | None], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        binance_kline_interval_label(interval_seconds)
        self.settings = settings or MarketDataSettings()
        self.interval_seconds = interval_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._exchange_clock = exchange_clock or self._clock
        self._on_candle = on_candle or (lambda _accepted, _bar: None)
        self._on_status = on_status or (lambda _status: None)
        self.connection_status = "disconnected"
        self.last_closed_candle: Bar | None = None
        self.last_valid_update: datetime | None = None
        self.last_error: str | None = "Not connected."
        self._valid_since_connect = False
        self.connected_event = asyncio.Event()
        self.messages_received = 0
        self.messages_rejected = 0

    @property
    def interval_label(self) -> str:
        return binance_kline_interval_label(self.interval_seconds)

    @property
    def stream_url(self) -> str:
        return binance_kline_stream_url(self.interval_seconds)

    def exchange_now(self) -> datetime:
        return _utc_datetime(self._exchange_clock())

    def mark_connecting(self) -> None:
        self.connection_status = "connecting"
        self._valid_since_connect = False
        self._on_status(self.connection_status)

    def mark_connected(self) -> None:
        self.connection_status = "connected"
        self._valid_since_connect = False
        self.last_error = "Waiting for a completed kline."
        self.connected_event.set()
        self._on_status(self.connection_status)

    def mark_reconnecting(self, reason: str) -> None:
        self.connection_status = "reconnecting"
        self._valid_since_connect = False
        self.last_error = reason
        self._on_status(self.connection_status)

    def mark_disconnected(self, reason: str = "Connection closed.") -> None:
        self.connection_status = "disconnected"
        self._valid_since_connect = False
        self.last_error = reason
        self._on_status(self.connection_status)

    def process_message(self, raw_message: str | bytes) -> tuple[bool, Bar | None]:
        """Validate a stream message and return only its completed OHLC bar."""
        self.messages_received += 1
        try:
            message = json.loads(raw_message)
            if not isinstance(message, dict):
                raise ValueError("Message must be a JSON object.")
            payload = message.get("data", message)
            if not isinstance(payload, dict) or payload.get("s") != self.settings.symbol:
                raise ValueError("Message symbol is not BTCUSDT.")
            kline = payload.get("k")
            if not isinstance(kline, dict):
                raise ValueError("Message does not contain a kline.")
            if kline.get("s") != self.settings.symbol:
                raise ValueError("Kline symbol is not BTCUSDT.")
            if kline.get("i") != self.interval_label:
                raise ValueError("Kline interval is unexpected.")

            open_time_ms = kline.get("t")
            close_time_ms = kline.get("T")
            if (
                isinstance(open_time_ms, bool)
                or not isinstance(open_time_ms, int)
                or isinstance(close_time_ms, bool)
                or not isinstance(close_time_ms, int)
            ):
                raise ValueError("Kline timestamps are invalid.")
            interval_ms = self.interval_seconds * 1000
            if (
                open_time_ms % interval_ms != 0
                or close_time_ms != open_time_ms + interval_ms - 1
            ):
                raise ValueError("Kline timestamps do not match the interval.")

            open_price = _price(kline.get("o"), "open")
            high = _price(kline.get("h"), "high")
            low = _price(kline.get("l"), "low")
            close = _price(kline.get("c"), "close")
            if low > min(open_price, close) or high < max(open_price, close) or high < low:
                raise ValueError("Kline OHLC values are inconsistent.")
            is_closed = kline.get("x")
            if not isinstance(is_closed, bool):
                raise ValueError("Kline closed flag is invalid.")
            if not is_closed:
                return True, None

            received_at = _utc_datetime(self._exchange_clock())
            open_timestamp = datetime.fromtimestamp(
                open_time_ms / 1000,
                tz=timezone.utc,
            )
            close_timestamp = datetime.fromtimestamp(
                close_time_ms / 1000,
                tz=timezone.utc,
            )
            age = (received_at - close_timestamp).total_seconds()
            rejection = (
                "future-dated" if age < 0
                else "stale" if age > self.settings.stale_after_seconds
                else None
            )
            if rejection is not None:
                raise ValueError(
                    f"Completed kline is {rejection}: "
                    f"open_utc={open_timestamp.isoformat()}, "
                    f"close_utc={close_timestamp.isoformat()}, "
                    f"current_utc={received_at.isoformat()}, "
                    f"age_seconds={age:.6f}, "
                    f"stale_limit_seconds={self.settings.stale_after_seconds:.6f}."
                )
            timestamp = close_timestamp
            bar = Bar(timestamp, open_price, high, low, close)
            previous = self.last_closed_candle
            if previous is not None:
                if timestamp < previous.timestamp:
                    raise ValueError("Out-of-order completed kline rejected.")
                if timestamp == previous.timestamp:
                    if bar != previous:
                        raise ValueError("Conflicting duplicate completed kline rejected.")
                    return True, None
            self.last_closed_candle = bar
            self.last_valid_update = received_at
            self.last_error = None
            self._valid_since_connect = self.connection_status == "connected"
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, OverflowError, OSError) as error:
            self.last_error = str(error)
            self._valid_since_connect = False
            self.messages_rejected += 1
            logger.warning("Rejected Binance kline data: %s", error)
            self._on_candle(False, None)
            return False, None

        self._on_candle(True, bar)
        return True, bar

    def health(self, now: datetime | None = None) -> dict[str, object]:
        try:
            observed_at = _utc_datetime(
                now if now is not None else self._exchange_clock()
            )
            reason = None
        except ValueError as error:
            observed_at = None
            reason = str(error)
        age_seconds = None
        if reason is None and self.connection_status != "connected":
            reason = f"Kline feed is {self.connection_status}."
        if reason is None and not self._valid_since_connect:
            reason = self.last_error or "No completed kline received since connection."
        if reason is None and self.last_valid_update is not None and observed_at is not None:
            age_seconds = (observed_at - self.last_valid_update).total_seconds()
            if age_seconds < 0:
                reason = "Local clock precedes the last completed kline."
            elif age_seconds > self.settings.stale_after_seconds:
                reason = (
                    f"Kline data is stale ({age_seconds:.3f}s; "
                    f"limit {self.settings.stale_after_seconds:.3f}s)."
                )
        healthy = reason is None and self.last_closed_candle is not None
        if healthy and self.last_closed_candle is not None and observed_at is not None:
            candle_age = (
                observed_at - self.last_closed_candle.timestamp
            ).total_seconds()
            if candle_age < 0 or candle_age > self.settings.stale_after_seconds:
                healthy = False
                reason = "Latest completed kline is future-dated or stale."
        if reason is None and not healthy:
            reason = "No completed kline received since connection."
        return {
            "healthy": healthy,
            "connection_status": self.connection_status,
            "last_closed_candle_timestamp": (
                self.last_closed_candle.timestamp.isoformat()
                if self.last_closed_candle is not None else None
            ),
            "last_valid_update": (
                self.last_valid_update.isoformat()
                if self.last_valid_update is not None else None
            ),
            "age_seconds": age_seconds,
            "reason": reason,
            "last_error": self.last_error,
            "messages_received": self.messages_received,
            "messages_rejected": self.messages_rejected,
        }

    async def run(
        self,
        *,
        connector=None,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        """Run the kline stream with the same bounded reconnect policy as quotes."""
        if connector is None:
            try:
                from websockets.asyncio.client import connect
                from websockets.exceptions import WebSocketException
            except ImportError as error:
                raise RuntimeError(
                    "Install project dependencies to run the Binance kline feed."
                ) from error
            connector = connect
        else:
            try:
                from websockets.exceptions import WebSocketException
            except ImportError:
                WebSocketException = OSError

        reconnects = 0
        backoff = self.settings.base_backoff_seconds
        while True:
            self.mark_connecting()
            try:
                async with connector(
                    self.stream_url,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                ) as websocket:
                    self.mark_connected()
                    async for raw_message in websocket:
                        self.process_message(raw_message)
                    raise ConnectionError("Kline WebSocket closed.")
            except asyncio.CancelledError:
                self.mark_disconnected("Kline feed task cancelled.")
                raise
            except (OSError, TimeoutError, ConnectionError, WebSocketException) as error:
                self.mark_disconnected(
                    f"Kline connection lost: {type(error).__name__}: {error}"
                )

            if reconnects >= self.settings.max_reconnect_attempts:
                break
            reconnects += 1
            self.mark_reconnecting(
                f"Kline reconnect {reconnects} of "
                f"{self.settings.max_reconnect_attempts} in {backoff:g}s."
            )
            await sleep(backoff)
            backoff = min(backoff * 2, self.settings.max_backoff_seconds)

        logger.error(
            "Binance kline reconnect limit exhausted after %d retries.",
            reconnects,
        )


def fetch_recent_binance_klines(
    interval_seconds: int,
    limit: int,
    *,
    now: datetime | None = None,
    timeout: float = 10.0,
) -> tuple[Bar, ...]:
    """Fetch genuine closed klines from Binance's unauthenticated public REST API."""
    from backtester import validate_candles
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or limit > 1000
    ):
        raise ValueError("Kline history limit must be between 1 and 1000.")
    interval_label = binance_kline_interval_label(interval_seconds)
    observed_at = _utc_datetime(now if now is not None else datetime.now(timezone.utc))
    interval_ms = interval_seconds * 1000
    query = urlencode({
        "symbol": "BTCUSDT",
        "interval": interval_label,
        "limit": limit,
    })
    with urlopen(f"{BINANCE_KLINE_REST_URL}?{query}", timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        raise ValueError("Binance kline history response must be a list.")

    now_ms = int(observed_at.timestamp() * 1000)
    candles = []
    for index, row in enumerate(payload):
        if not isinstance(row, list) or len(row) < 7:
            raise ValueError(f"Binance kline history row {index} is invalid.")
        open_time_ms, close_time_ms = row[0], row[6]
        if (
            isinstance(open_time_ms, bool)
            or not isinstance(open_time_ms, int)
            or isinstance(close_time_ms, bool)
            or not isinstance(close_time_ms, int)
            or open_time_ms % interval_ms != 0
            or close_time_ms != open_time_ms + interval_ms - 1
        ):
            raise ValueError(f"Binance kline history row {index} has invalid timestamps.")
        if open_time_ms > now_ms:
            raise ValueError(
                f"Binance kline history row {index} is future-dated."
            )
        if close_time_ms >= now_ms:
            continue
        prices = tuple(
            _price(row[offset], name)
            for offset, name in zip((1, 2, 3, 4), ("open", "high", "low", "close"))
        )
        candles.append(Bar(
            datetime.fromtimestamp(
                close_time_ms / 1000,
                tz=timezone.utc,
            ),
            *prices,
        ))
    if not candles:
        return ()
    validated = validate_candles(candles, interval_seconds=interval_seconds)
    return validated[-limit:]
