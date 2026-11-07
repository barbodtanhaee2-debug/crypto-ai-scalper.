import asyncio
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from config import (
    LIVE_EXECUTION_ENABLED,
    PAPER_TRADING_ONLY,
    STARTING_BALANCE_AMOUNT,
    STARTING_BALANCE_CURRENCY,
)
from market_data import (
    BINANCE_BOOK_TICKER_URL,
    BINANCE_KLINE_REST_URL,
    BINANCE_KLINE_URL,
    BINANCE_SERVER_TIME_URL,
    BinanceServerClock,
    binance_kline_stream_url,
    BinanceBookTicker,
    BinanceKlineStream,
    MarketDataSettings,
    fetch_binance_server_time,
    fetch_recent_binance_klines,
)
from strategy import Bar


class MutableWallClock:
    def __init__(self, value):
        self.value = value
        self.elapsed = 0.0

    def __call__(self):
        return self.value

    def monotonic(self):
        self.elapsed += 0.01
        return self.elapsed


class BinanceBookTickerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        self.feed = BinanceBookTicker(clock=lambda: self.now)
        self.feed.mark_connected()

    def message(self, **updates):
        payload = {
            "u": 12345,
            "s": "BTCUSDT",
            "b": "67000.10",
            "a": "67000.20",
        }
        payload.update(updates)
        return json.dumps(payload)

    def test_valid_bid_ask_tracks_spread_and_received_timestamp(self):
        self.assertTrue(self.feed.process_message(self.message()))

        status = self.feed.health()
        self.assertTrue(status["healthy"])
        self.assertTrue(status["entry_allowed"])
        self.assertEqual(status["bid"], 67000.10)
        self.assertEqual(status["ask"], 67000.20)
        self.assertAlmostEqual(status["spread"], 0.10)
        self.assertEqual(status["message_timestamp"], self.now.isoformat())
        self.assertEqual(status["last_valid_update"], self.now.isoformat())

    def test_market_data_endpoints_are_public_and_limited_to_quotes_and_klines(self):
        self.assertEqual(
            BINANCE_BOOK_TICKER_URL,
            "wss://stream.binance.com:9443/ws/btcusdt@bookTicker",
        )
        self.assertEqual(
            binance_kline_stream_url(60),
            "wss://stream.binance.com:9443/ws/btcusdt@kline_1m",
        )
        self.assertEqual(
            BINANCE_KLINE_REST_URL,
            "https://api.binance.com/api/v3/klines",
        )
        self.assertEqual(
            BINANCE_SERVER_TIME_URL,
            "https://api.binance.com/api/v3/time",
        )
        self.assertIs(PAPER_TRADING_ONLY, True)
        self.assertIs(LIVE_EXECUTION_ENABLED, False)

    def test_broker_and_runtime_sources_have_no_private_or_order_endpoints(self):
        project_root = Path(__file__).resolve().parents[1]
        for filename in ("runtime.py", "market_data.py", "paper_broker.py"):
            source = (project_root / filename).read_text(encoding="utf-8").casefold()
            for forbidden in (
                "/api/v3/order",
                "/sapi/",
                "x-mbx-apikey",
                "api_secret",
            ):
                with self.subTest(filename=filename, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)

    def test_invalid_prices_and_crossed_markets_never_become_healthy(self):
        for bad_prices in (
            {"b": None},
            {"a": "not-a-price"},
            {"b": "0"},
            {"a": "-1"},
            {"b": "NaN"},
            {"b": "101", "a": "100"},
            {"b": "100", "a": "100"},
        ):
            with self.subTest(bad_prices=bad_prices):
                feed = BinanceBookTicker(clock=lambda: self.now)
                feed.mark_connected()
                self.assertFalse(feed.process_message(self.message(**bad_prices)))
                self.assertFalse(feed.health()["healthy"])
                self.assertFalse(feed.health()["entry_allowed"])

    def test_invalid_update_revokes_health_until_a_valid_update_arrives(self):
        self.assertTrue(self.feed.process_message(self.message()))
        self.assertFalse(
            self.feed.process_message(self.message(b="67000.30", a="67000.20"))
        )

        status = self.feed.health()
        self.assertFalse(status["healthy"])
        self.assertFalse(status["entry_allowed"])

        self.assertTrue(self.feed.process_message(self.message()))
        self.assertTrue(self.feed.health()["entry_allowed"])

    def test_stale_or_invalid_timestamps_fail_closed(self):
        stale_feed = BinanceBookTicker(
            MarketDataSettings(stale_after_seconds=2),
            clock=lambda: self.now,
        )
        stale_feed.mark_connected()
        old_event = int((self.now - timedelta(seconds=3)).timestamp() * 1000)
        self.assertFalse(stale_feed.process_message(self.message(E=old_event)))
        self.assertFalse(stale_feed.health()["entry_allowed"])

        invalid_event_feed = BinanceBookTicker(clock=lambda: self.now)
        invalid_event_feed.mark_connected()
        self.assertFalse(
            invalid_event_feed.process_message(self.message(E="invalid"))
        )
        self.assertFalse(invalid_event_feed.health()["healthy"])

        invalid_clock_feed = BinanceBookTicker(clock=lambda: datetime(2026, 1, 1))
        invalid_clock_feed.mark_connected()
        self.assertFalse(invalid_clock_feed.process_message(self.message()))
        self.assertFalse(invalid_clock_feed.health()["healthy"])

    def test_disconnected_and_reconnecting_states_block_entries(self):
        self.feed.process_message(self.message())
        self.feed.mark_disconnected("socket closed")
        self.assertFalse(self.feed.health()["entry_allowed"])

        self.feed.mark_reconnecting("retry pending")
        status = self.feed.health()
        self.assertFalse(status["healthy"])
        self.assertEqual(status["connection_status"], "reconnecting")

    def test_reconnect_requires_a_new_valid_update_before_health_recovers(self):
        self.feed.process_message(self.message())
        self.feed.mark_disconnected()
        self.feed.mark_connecting()
        self.feed.mark_connected()

        self.assertFalse(self.feed.health()["entry_allowed"])
        self.assertTrue(self.feed.process_message(self.message()))
        self.assertTrue(self.feed.health()["entry_allowed"])

    def test_freshness_limit_blocks_entries_after_valid_data_goes_stale(self):
        feed = BinanceBookTicker(
            MarketDataSettings(stale_after_seconds=2),
            clock=lambda: self.now,
        )
        feed.mark_connected()
        feed.process_message(self.message())

        status = feed.health(now=self.now + timedelta(seconds=3))
        self.assertFalse(status["healthy"])
        self.assertFalse(status["entry_allowed"])
        self.assertIn("stale", status["reason"])

    def test_stage_one_paper_safety_configuration_is_unchanged(self):
        self.assertIs(PAPER_TRADING_ONLY, True)
        self.assertIs(LIVE_EXECUTION_ENABLED, False)
        self.assertEqual(STARTING_BALANCE_AMOUNT, 100.0)
        self.assertEqual(STARTING_BALANCE_CURRENCY, "USDT")


class BinanceKlineTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        self.now = self.start + timedelta(minutes=1, milliseconds=200)
        self.feed = BinanceKlineStream(
            MarketDataSettings(stale_after_seconds=120),
            clock=lambda: self.now,
        )
        self.feed.mark_connected()

    def message(
        self,
        *,
        start=None,
        closed=True,
        interval_seconds=None,
        interval_label=None,
        **updates,
    ):
        start = start or self.start
        interval_seconds = interval_seconds or self.feed.interval_seconds
        interval_label = interval_label or f"{interval_seconds // 60}m"
        open_ms = int(start.timestamp() * 1000)
        interval_ms = interval_seconds * 1000
        payload = {
            "e": "kline",
            "E": int(self.now.timestamp() * 1000),
            "s": "BTCUSDT",
            "k": {
                "s": "BTCUSDT",
                "i": interval_label,
                "t": open_ms,
                "T": open_ms + interval_ms - 1,
                "o": "100",
                "h": "101",
                "l": "99",
                "c": "100.5",
                "v": "12.345",
                "n": 123,
                "x": closed,
                "q": "1234.5",
                "V": "6.789",
                "Q": "678.9",
                "B": "0",
            },
        }
        payload["k"].update(updates)
        return json.dumps(payload)

    def test_only_valid_completed_exchange_klines_are_emitted(self):
        accepted, bar = self.feed.process_message(self.message(closed=False))
        self.assertTrue(accepted)
        self.assertIsNone(bar)
        self.assertFalse(self.feed.health()["healthy"])

        accepted, bar = self.feed.process_message(self.message())
        self.assertTrue(accepted)
        self.assertEqual(
            bar,
            Bar(
                self.start + timedelta(minutes=1, milliseconds=-1),
                100, 101, 99, 100.5,
            ),
        )
        self.assertTrue(self.feed.health()["healthy"])

    def test_realistic_binance_closed_kline_is_valid_at_inclusive_close_time(self):
        close_time = self.start + timedelta(minutes=1, milliseconds=-1)
        feed = BinanceKlineStream(
            MarketDataSettings(stale_after_seconds=90),
            clock=lambda: close_time,
        )
        feed.mark_connected()

        accepted, bar = feed.process_message(self.message())

        self.assertTrue(accepted)
        self.assertEqual(
            bar,
            Bar(close_time, 100, 101, 99, 100.5),
        )

    def test_invalid_live_kline_logs_parsed_times_age_and_exact_rejection(self):
        close_time = self.start + timedelta(minutes=1, milliseconds=-1)
        for now, condition in (
            (close_time - timedelta(milliseconds=1), "future-dated"),
            (close_time + timedelta(seconds=4), "stale"),
        ):
            with self.subTest(condition=condition):
                feed = BinanceKlineStream(
                    MarketDataSettings(stale_after_seconds=3),
                    clock=lambda: now,
                )
                feed.mark_connected()

                with self.assertLogs("market_data", level="WARNING") as logs:
                    accepted, bar = feed.process_message(self.message())

                self.assertFalse(accepted)
                self.assertIsNone(bar)
                warning = "\n".join(logs.output)
                self.assertIn(condition, warning)
                self.assertIn("open_utc=2026-10-21T12:00:00+00:00", warning)
                self.assertIn("close_utc=2026-10-21T12:00:59.999000+00:00", warning)
                self.assertIn(f"current_utc={now.isoformat()}", warning)
                self.assertIn("age_seconds=", warning)

    def test_calibrated_clock_accepts_positive_and_negative_host_offsets(self):
        close_time = self.start + timedelta(minutes=1, milliseconds=-1)
        for offset in (0.85, -0.85):
            with self.subTest(offset=offset):
                wall_clock = MutableWallClock(
                    close_time - timedelta(seconds=offset)
                )
                calibrated = BinanceServerClock(
                    clock=wall_clock,
                    monotonic=wall_clock.monotonic,
                )
                calibrated.synchronize(
                    lambda: wall_clock.value + timedelta(seconds=offset)
                )
                feed = BinanceKlineStream(
                    MarketDataSettings(stale_after_seconds=120),
                    clock=wall_clock,
                    exchange_clock=calibrated.now,
                )
                feed.mark_connected()

                accepted, bar = feed.process_message(self.message())

                self.assertTrue(accepted, feed.last_error)
                self.assertEqual(bar.timestamp, close_time)

    def test_calibrated_clock_rejects_offsets_beyond_bound_and_future_candles(self):
        wall_clock = MutableWallClock(self.now)
        calibrated = BinanceServerClock(
            clock=wall_clock,
            monotonic=wall_clock.monotonic,
        )
        with self.assertRaisesRegex(ValueError, "exceeds the permitted"):
            calibrated.synchronize(
                lambda: wall_clock.value + timedelta(seconds=6)
            )
        with self.assertRaisesRegex(ValueError, "calibration is unavailable"):
            calibrated.now()

        calibrated.synchronize(lambda: wall_clock.value)
        feed = BinanceKlineStream(
            MarketDataSettings(stale_after_seconds=120),
            clock=wall_clock,
            exchange_clock=calibrated.now,
        )
        feed.mark_connected()
        accepted, bar = feed.process_message(
            self.message(start=self.start + timedelta(minutes=1))
        )

        self.assertFalse(accepted)
        self.assertIsNone(bar)
        self.assertIn("future-dated", feed.last_error)

    def test_server_time_sample_with_excessive_latency_is_rejected(self):
        wall_clock = MutableWallClock(self.now)
        samples = iter((0.0, 2.1))
        calibrated = BinanceServerClock(
            clock=wall_clock,
            monotonic=lambda: next(samples),
        )

        with self.assertRaisesRegex(ValueError, "latency"):
            calibrated.synchronize(lambda: wall_clock.value)

        with self.assertRaisesRegex(ValueError, "calibration is unavailable"):
            calibrated.now()

    def test_failed_resynchronization_invalidates_previous_calibration(self):
        wall_clock = MutableWallClock(self.now)
        calibrated = BinanceServerClock(
            clock=wall_clock,
            monotonic=wall_clock.monotonic,
        )
        calibrated.synchronize(lambda: wall_clock.value)

        with self.assertRaisesRegex(ValueError, "exceeds the permitted"):
            calibrated.synchronize(
                lambda: wall_clock.value + timedelta(seconds=6)
            )

        with self.assertRaisesRegex(ValueError, "calibration is unavailable"):
            calibrated.now()

    def test_stale_server_time_measurement_fails_closed(self):
        wall_clock = MutableWallClock(self.now)
        calibrated = BinanceServerClock(
            clock=wall_clock,
            monotonic=wall_clock.monotonic,
        )
        calibrated.synchronize(lambda: wall_clock.value)
        wall_clock.elapsed += 121
        feed = BinanceKlineStream(
            clock=wall_clock,
            exchange_clock=calibrated.now,
        )
        feed.mark_connected()

        accepted, bar = feed.process_message(self.message())

        self.assertFalse(accepted)
        self.assertIsNone(bar)
        self.assertIn("calibration is stale", feed.last_error)
        self.assertFalse(feed.health()["healthy"])

    def test_host_clock_jump_invalidates_server_time_measurement(self):
        wall_clock = MutableWallClock(self.now)
        calibrated = BinanceServerClock(
            clock=wall_clock,
            monotonic=wall_clock.monotonic,
        )
        calibrated.synchronize(lambda: wall_clock.value)
        wall_clock.value += timedelta(seconds=1)

        with self.assertRaisesRegex(ValueError, "Host UTC clock changed"):
            calibrated.now()

    def test_public_server_time_response_is_validated(self):
        response = io.BytesIO(json.dumps({"serverTime": 1_792_000_000_000}).encode())
        with patch("market_data.urlopen", return_value=response) as open_url:
            server_time = fetch_binance_server_time()

        self.assertEqual(server_time.tzinfo, timezone.utc)
        self.assertEqual(open_url.call_args.args[0], BINANCE_SERVER_TIME_URL)

        for payload in ([], {}, {"serverTime": True}, {"serverTime": "123"}):
            with self.subTest(payload=payload):
                response = io.BytesIO(json.dumps(payload).encode())
                with (
                    patch("market_data.urlopen", return_value=response),
                    self.assertRaisesRegex(ValueError, "invalid timestamp|must be an object"),
                ):
                    fetch_binance_server_time()

    def test_rejected_completed_kline_reason_survives_valid_open_kline(self):
        close_time = self.start + timedelta(minutes=1, milliseconds=-1)
        feed = BinanceKlineStream(
            MarketDataSettings(stale_after_seconds=90),
            clock=lambda: close_time - timedelta(seconds=1),
        )
        feed.mark_connected()

        accepted, bar = feed.process_message(self.message())
        self.assertFalse(accepted)
        self.assertIsNone(bar)
        rejection = feed.last_error

        accepted, bar = feed.process_message(self.message(closed=False))

        self.assertTrue(accepted)
        self.assertIsNone(bar)
        self.assertEqual(feed.last_error, rejection)
        self.assertIn("future-dated", feed.health()["last_error"])

    def test_duplicate_candles_are_deduplicated_and_out_of_order_is_rejected(self):
        first = self.feed.process_message(self.message())
        duplicate = self.feed.process_message(self.message())
        older = self.feed.process_message(
            self.message(start=self.start - timedelta(minutes=1))
        )

        self.assertTrue(first[0])
        self.assertIsNotNone(first[1])
        self.assertEqual(duplicate, (True, None))
        self.assertEqual(older, (False, None))
        self.assertEqual(self.feed.messages_rejected, 1)
        self.assertFalse(self.feed.health()["healthy"])

    def test_invalid_kline_shape_interval_ohlc_and_stale_close_are_rejected(self):
        for updates in (
            {"i": "5m"},
            {"x": "true"},
            {"h": "98"},
            {"t": int((self.start + timedelta(seconds=1)).timestamp() * 1000)},
        ):
            with self.subTest(updates=updates):
                feed = BinanceKlineStream(clock=lambda: self.now)
                feed.mark_connected()
                self.assertEqual(feed.process_message(self.message(**updates)), (False, None))

        stale_feed = BinanceKlineStream(
            MarketDataSettings(stale_after_seconds=3),
            clock=lambda: self.start + timedelta(minutes=3),
        )
        stale_feed.mark_connected()
        self.assertEqual(stale_feed.process_message(self.message()), (False, None))

    def test_supported_one_and_five_minute_streams_validate_their_interval(self):
        for interval_seconds, label in ((60, "1m"), (300, "5m")):
            close_time = self.start + timedelta(seconds=interval_seconds) - timedelta(milliseconds=1)
            feed = BinanceKlineStream(
                MarketDataSettings(stale_after_seconds=1200),
                interval_seconds=interval_seconds,
                clock=lambda: close_time,
            )
            feed.mark_connected()
            with self.subTest(interval=label):
                self.assertEqual(feed.interval_label, label)
                self.assertEqual(
                    feed.stream_url,
                    f"wss://stream.binance.com:9443/ws/btcusdt@kline_{label}",
                )
                accepted, bar = feed.process_message(
                    self.message(
                        interval_seconds=interval_seconds,
                        interval_label=label,
                    )
                )
                self.assertTrue(accepted)
                self.assertEqual(bar.timestamp, close_time)

                rejected = feed.process_message(
                    self.message(
                        interval_seconds=interval_seconds,
                        interval_label="1m" if label == "5m" else "5m",
                    )
                )
                self.assertEqual(rejected, (False, None))

    def test_unsupported_kline_intervals_are_rejected_without_fallback(self):
        for interval_seconds in (0, 120, 600, 3600):
            with self.subTest(interval=interval_seconds):
                with self.assertRaisesRegex(ValueError, "Unsupported Binance"):
                    BinanceKlineStream(interval_seconds=interval_seconds)
                with self.assertRaisesRegex(ValueError, "Unsupported Binance"):
                    fetch_recent_binance_klines(interval_seconds, 21, now=self.now)

        self.assertEqual(binance_kline_stream_url(60), BINANCE_KLINE_URL)

    def test_five_minute_history_uses_matching_rest_interval_and_validates_close(self):
        interval_seconds = 300
        rows = []
        for index in range(22):
            open_ms = int(
                (self.start + timedelta(seconds=index * interval_seconds)).timestamp()
                * 1000
            )
            rows.append([
                open_ms, "100", "101", "99", "100.5", "1",
                open_ms + interval_seconds * 1000 - 1,
                "0", 1, "0", "0", "0",
            ])
        response = io.BytesIO(json.dumps(rows).encode("utf-8"))
        with patch("market_data.urlopen", return_value=response) as open_url:
            bars = fetch_recent_binance_klines(
                interval_seconds,
                21,
                now=self.start + timedelta(seconds=22 * interval_seconds),
            )

        self.assertEqual(len(bars), 21)
        self.assertEqual(
            parse_qs(urlparse(open_url.call_args.args[0]).query)["interval"],
            ["5m"],
        )
        self.assertEqual(
            (bars[1].timestamp - bars[0].timestamp).total_seconds(),
            interval_seconds,
        )

        invalid_rows = [row.copy() for row in rows]
        invalid_rows[0][6] -= 1
        invalid_response = io.BytesIO(json.dumps(invalid_rows).encode("utf-8"))
        with (
            patch("market_data.urlopen", return_value=invalid_response),
            self.assertRaisesRegex(ValueError, "invalid timestamps"),
        ):
            fetch_recent_binance_klines(
                interval_seconds,
                21,
                now=self.start + timedelta(seconds=22 * interval_seconds),
            )

    def test_history_fetch_rejects_future_dated_open_candles(self):
        open_ms = int((self.start + timedelta(minutes=1)).timestamp() * 1000)
        response = io.BytesIO(json.dumps([[
            open_ms, "100", "101", "99", "100.5", "1",
            open_ms + 59_999, "0", 1, "0", "0", "0",
        ]]).encode("utf-8"))
        with (
            patch("market_data.urlopen", return_value=response),
            self.assertRaisesRegex(ValueError, "future-dated"),
        ):
            fetch_recent_binance_klines(
                60,
                1,
                now=self.start,
            )

    def test_reconnect_requires_a_new_completed_kline(self):
        self.assertTrue(self.feed.process_message(self.message())[0])
        self.feed.mark_disconnected("test disconnect")
        self.assertFalse(self.feed.health()["healthy"])
        self.feed.mark_connecting()
        self.feed.mark_connected()
        self.assertFalse(self.feed.health()["healthy"])
        self.now += timedelta(minutes=1)
        self.assertTrue(self.feed.process_message(
            self.message(start=self.start + timedelta(minutes=1))
        )[0])
        self.assertTrue(self.feed.health()["healthy"])

    def test_history_fetch_uses_public_rest_and_returns_only_closed_valid_bars(self):
        rows = []
        for index in range(22):
            open_ms = int((self.start + timedelta(minutes=index)).timestamp() * 1000)
            rows.append([
                open_ms, "100", "101", "99", "100.5", "1",
                open_ms + 59_999, "0", 1, "0", "0", "0",
            ])
        response = io.BytesIO(json.dumps(rows).encode("utf-8"))
        with patch("market_data.urlopen", return_value=response) as open_url:
            bars = fetch_recent_binance_klines(
                60,
                21,
                now=self.start + timedelta(minutes=21, milliseconds=200),
            )

        self.assertEqual(len(bars), 21)
        self.assertEqual(
            bars[-1].timestamp,
            self.start + timedelta(minutes=21, milliseconds=-1),
        )
        request_url = open_url.call_args.args[0]
        self.assertTrue(request_url.startswith(BINANCE_KLINE_REST_URL))
        self.assertEqual(
            parse_qs(urlparse(request_url).query),
            {"symbol": ["BTCUSDT"], "interval": ["1m"], "limit": ["21"]},
        )
        self.assertEqual(BINANCE_KLINE_URL.rsplit("@", 1)[-1], "kline_1m")

    def test_history_fetch_rejects_gaps_in_exchange_candles(self):
        rows = []
        for index in (0, 1, 3):
            open_ms = int((self.start + timedelta(minutes=index)).timestamp() * 1000)
            rows.append([
                open_ms, "100", "101", "99", "100.5", "1",
                open_ms + 59_999, "0", 1, "0", "0", "0",
            ])
        response = io.BytesIO(json.dumps(rows).encode("utf-8"))

        with (
            patch("market_data.urlopen", return_value=response),
            self.assertRaisesRegex(ValueError, "Missing candle interval"),
        ):
            fetch_recent_binance_klines(
                60,
                3,
                now=self.start + timedelta(minutes=4),
            )


class ReconnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_kline_stream_uses_configured_interval_in_socket_url(self):
        class EmptyWebSocket:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class Connection:
            async def __aenter__(self):
                return EmptyWebSocket()

            async def __aexit__(self, *_args):
                return False

        for interval_seconds, label in ((60, "1m"), (300, "5m")):
            requested_urls = []

            def connector(url, **_kwargs):
                requested_urls.append(url)
                return Connection()

            feed = BinanceKlineStream(
                MarketDataSettings(max_reconnect_attempts=0),
                interval_seconds=interval_seconds,
            )
            await feed.run(connector=connector)
            self.assertEqual(
                requested_urls,
                [f"wss://stream.binance.com:9443/ws/btcusdt@kline_{label}"],
            )

    async def test_retries_are_bounded_with_exponential_backoff(self):
        attempts = []
        delays = []

        class FailedConnection:
            async def __aenter__(self):
                raise OSError("connection unavailable")

            async def __aexit__(self, *_args):
                return False

        def connector(url, **kwargs):
            attempts.append((url, kwargs))
            return FailedConnection()

        async def sleep(delay):
            delays.append(delay)

        feed = BinanceBookTicker(MarketDataSettings(
            max_reconnect_attempts=2,
            base_backoff_seconds=0.25,
            max_backoff_seconds=1.0,
        ))
        await feed.run(connector=connector, sleep=sleep)

        self.assertEqual(len(attempts), 3)
        self.assertEqual(
            [delay for delay in delays],
            [0.25, 0.5],
        )
        self.assertTrue(all(
            url == BINANCE_BOOK_TICKER_URL
            for url, _kwargs in attempts
        ))
        self.assertEqual(feed.connection_status, "disconnected")
        self.assertFalse(feed.health()["entry_allowed"])

    async def test_large_retry_limit_keeps_backoff_bounded(self):
        delays = []

        class FailedConnection:
            async def __aenter__(self):
                raise OSError("connection unavailable")

            async def __aexit__(self, *_args):
                return False

        async def sleep(delay):
            delays.append(delay)

        feed = BinanceBookTicker(MarketDataSettings(max_reconnect_attempts=1025))
        await feed.run(
            connector=lambda _url, **_kwargs: FailedConnection(),
            sleep=sleep,
        )

        self.assertEqual(len(delays), 1025)
        self.assertTrue(all(0 < delay <= 30 for delay in delays))
        self.assertEqual(delays[-1], 30)

    async def test_connection_without_valid_message_never_becomes_healthy(self):
        class EmptyConnection:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        feed = BinanceBookTicker(MarketDataSettings(max_reconnect_attempts=0))
        await feed.run(
            connector=lambda _url, **_kwargs: EmptyConnection(),
            sleep=asyncio.sleep,
        )
        self.assertFalse(feed.health()["healthy"])
        self.assertFalse(feed.health()["entry_allowed"])


if __name__ == "__main__":
    unittest.main()
