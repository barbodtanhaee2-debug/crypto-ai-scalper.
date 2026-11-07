import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from market_data import BINANCE_BOOK_TICKER_URL, binance_kline_stream_url
from quote_tape_collector import (
    QuoteTapeCollector,
    parse_book_ticker,
    parse_kline,
    resolve_session_directory,
    write_session_json,
)


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class ScriptedSocket:
    def __init__(self, messages, *, close_error=None):
        self._messages = list(messages)
        self._close_error = close_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        if self._close_error is not None:
            raise self._close_error
        raise StopAsyncIteration


class QuoteParseTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)

    def message(self, **updates):
        payload = {
            "u": 100,
            "s": "BTCUSDT",
            "b": "67000.10",
            "B": "1.5",
            "a": "67000.20",
            "A": "0.8",
            "E": int(self.now.timestamp() * 1000) - 20,
        }
        payload.update(updates)
        return json.dumps(payload)

    def test_valid_quote_spread_and_event_age(self):
        parsed = parse_book_ticker(self.message(), received_at=self.now)
        self.assertTrue(parsed["accepted"])
        self.assertEqual(parsed["symbol"], "BTCUSDT")
        self.assertEqual(parsed["bid"], 67000.10)
        self.assertEqual(parsed["ask"], 67000.20)
        self.assertEqual(parsed["bid_quantity"], 1.5)
        self.assertEqual(parsed["ask_quantity"], 0.8)
        self.assertEqual(parsed["update_id"], 100)
        self.assertAlmostEqual(parsed["spread"], 0.10)
        self.assertAlmostEqual(parsed["spread_percent"], (0.10 / 67000.15) * 100)
        self.assertAlmostEqual(parsed["event_age_seconds"], 0.02)
        self.assertEqual(parsed["bid_raw"], "67000.10")

    def test_missing_exchange_event_time_is_null_not_fabricated(self):
        payload = json.loads(self.message())
        del payload["E"]
        parsed = parse_book_ticker(json.dumps(payload), received_at=self.now)
        self.assertTrue(parsed["accepted"])
        self.assertIsNone(parsed["exchange_event_timestamp"])
        self.assertIsNone(parsed["event_age_seconds"])

    def test_invalid_or_missing_bid_ask_is_rejected(self):
        for bad in (
            {"b": None},
            {"a": "not-a-price"},
            {"b": "0"},
            {"a": "-1"},
            {"b": "NaN"},
            {"b": "101", "a": "100"},
            {"b": "100", "a": "100"},
            {"s": "ETHUSDT"},
        ):
            with self.subTest(bad=bad):
                parsed = parse_book_ticker(self.message(**bad), received_at=self.now)
                self.assertFalse(parsed["accepted"])
                self.assertIsNotNone(parsed["rejection"])

    def test_malformed_json_preserves_raw_preview(self):
        parsed = parse_book_ticker("{not-json", received_at=self.now)
        self.assertFalse(parsed["accepted"])
        self.assertIn("raw_preview", parsed)
        self.assertIn("{not-json", parsed["raw_preview"])


class CandleParseTests(unittest.TestCase):
    def setUp(self):
        self.open = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        self.received = self.open + timedelta(minutes=1, milliseconds=50)

    def message(self, *, closed=True, **updates):
        open_ms = int(self.open.timestamp() * 1000)
        payload = {
            "e": "kline",
            "s": "BTCUSDT",
            "k": {
                "s": "BTCUSDT",
                "i": "1m",
                "t": open_ms,
                "T": open_ms + 59_999,
                "o": "100",
                "h": "101",
                "l": "99",
                "c": "100.5",
                "v": "12.345",
                "q": "1234.5",
                "x": closed,
            },
        }
        payload["k"].update(updates)
        return json.dumps(payload)

    def test_completed_candle_records_volumes_and_times(self):
        parsed = parse_kline(self.message(), received_at=self.received)
        self.assertTrue(parsed["accepted"])
        self.assertTrue(parsed["is_closed"])
        self.assertEqual(parsed["open"], 100.0)
        self.assertEqual(parsed["base_volume"], 12.345)
        self.assertEqual(parsed["quote_volume"], 1234.5)
        self.assertEqual(parsed["symbol"], "BTCUSDT")
        self.assertEqual(
            parsed["close_time_utc"],
            (self.open + timedelta(milliseconds=59_999)).isoformat(),
        )
        self.assertIsNone(parsed["exchange_event_timestamp"])

    def test_completed_candle_keeps_kline_symbol_when_payload_symbol_omitted(self):
        payload = json.loads(self.message())
        del payload["s"]
        parsed = parse_kline(json.dumps(payload), received_at=self.received)
        self.assertTrue(parsed["accepted"])
        self.assertEqual(parsed["symbol"], "BTCUSDT")

    def test_mismatched_kline_and_message_symbols_are_rejected(self):
        parsed = parse_kline(self.message(s="ETHUSDT"), received_at=self.received)
        self.assertFalse(parsed["accepted"])
        self.assertEqual(parsed["symbol"], "ETHUSDT")
        self.assertIn("does not match", parsed["rejection"])

    def test_non_btcusdt_symbol_is_rejected(self):
        payload = json.loads(self.message())
        payload["s"] = "ETHUSDT"
        payload["k"]["s"] = "ETHUSDT"
        parsed = parse_kline(json.dumps(payload), received_at=self.received)
        self.assertFalse(parsed["accepted"])
        self.assertEqual(parsed["symbol"], "ETHUSDT")
        self.assertIn("BTCUSDT", parsed["rejection"])

    def test_negative_close_age_is_recorded_and_accepted(self):
        early = self.open + timedelta(seconds=58)
        parsed = parse_kline(self.message(), received_at=early)
        self.assertTrue(parsed["accepted"])
        self.assertLess(parsed["close_age_seconds"], 0)

    def test_kline_exchange_event_before_close_is_measured(self):
        open_ms = int(self.open.timestamp() * 1000)
        event_ms = open_ms + 58_000
        payload = json.loads(self.message())
        payload["E"] = event_ms
        parsed = parse_kline(json.dumps(payload), received_at=self.received)
        self.assertTrue(parsed["accepted"])
        self.assertEqual(
            parsed["exchange_event_timestamp"],
            datetime.fromtimestamp(event_ms / 1000, tz=timezone.utc).isoformat(),
        )
        self.assertLess(parsed["event_minus_close_seconds"], 0)

    def test_in_progress_candle_is_not_accepted(self):
        parsed = parse_kline(self.message(closed=False), received_at=self.received)
        self.assertFalse(parsed["accepted"])
        self.assertFalse(parsed["is_closed"])
        self.assertEqual(parsed["rejection"], "Candle is still in progress.")

    def test_invalid_ohlc_is_rejected(self):
        parsed = parse_kline(self.message(h="90"), received_at=self.received)
        self.assertFalse(parsed["accepted"])
        self.assertIn("OHLC", parsed["rejection"])


class QuoteTapeCollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        self.clock = MutableClock(self.now)

    def collector(self, **kwargs):
        instance = QuoteTapeCollector(
            output_dir=self.root / "session",
            session_id="audit-session",
            clock=self.clock,
            **kwargs,
        )
        self.addCleanup(instance.close)
        return instance

    def quote(self, update_id=1, **updates):
        payload = {
            "u": update_id,
            "s": "BTCUSDT",
            "b": "100.00",
            "B": "1",
            "a": "100.02",
            "A": "2",
        }
        payload.update(updates)
        return json.dumps(payload)

    def kline(self, *, open_time, closed=True):
        open_ms = int(open_time.timestamp() * 1000)
        return json.dumps(
            {
                "s": "BTCUSDT",
                "k": {
                    "s": "BTCUSDT",
                    "i": "1m",
                    "t": open_ms,
                    "T": open_ms + 59_999,
                    "o": "100",
                    "h": "101",
                    "l": "99",
                    "c": "100.5",
                    "v": "1",
                    "q": "100",
                    "x": closed,
                },
            }
        )

    def test_duplicate_and_out_of_order_quotes(self):
        collector = self.collector()
        self.assertTrue(collector.ingest_quote(self.quote(1))["accepted"])
        duplicate = collector.ingest_quote(self.quote(1))
        self.assertEqual(duplicate.get("ordering"), "duplicate")
        self.assertEqual(collector.counts.quotes_duplicate, 1)
        later = collector.ingest_quote(self.quote(3))
        self.assertTrue(later["accepted"])
        out_of_order = collector.ingest_quote(self.quote(2))
        self.assertEqual(out_of_order.get("ordering"), "out_of_order")
        self.assertEqual(collector.counts.quotes_out_of_order, 1)
        quotes = (self.root / "session" / "quotes.jsonl").read_text(encoding="utf-8")
        self.assertEqual(quotes.count("\n"), 3)

    def test_duplicate_and_missing_candles(self):
        collector = self.collector()
        first = self.now
        second = self.now + timedelta(minutes=1)
        skipped = self.now + timedelta(minutes=3)
        self.assertTrue(collector.ingest_kline(self.kline(open_time=first))["accepted"])
        duplicate = collector.ingest_kline(self.kline(open_time=first))
        self.assertEqual(duplicate.get("ordering"), "duplicate")
        in_progress = collector.ingest_kline(
            self.kline(open_time=second, closed=False)
        )
        self.assertFalse(in_progress["accepted"])
        self.assertEqual(collector.counts.candles_in_progress, 1)
        self.assertTrue(collector.ingest_kline(self.kline(open_time=second))["accepted"])
        gap = collector.ingest_kline(self.kline(open_time=skipped))
        self.assertTrue(gap["accepted"])
        self.assertEqual(gap.get("ordering"), "missing_interval")
        self.assertEqual(collector.counts.candles_missing_interval, 1)
        candles = (self.root / "session" / "candles.jsonl").read_text(encoding="utf-8")
        self.assertEqual(candles.count("\n"), 3)
        collector.close()

    def test_storage_appends_after_reopen(self):
        first = self.collector()
        first.ingest_quote(self.quote(1))
        first.close()
        second = QuoteTapeCollector(
            output_dir=self.root / "session",
            session_id="audit-session-2",
            clock=self.clock,
        )
        second.ingest_quote(self.quote(2))
        second.close()
        lines = (self.root / "session" / "quotes.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["update_id"], 1)
        self.assertEqual(json.loads(lines[1])["update_id"], 2)

    def test_live_journal_path_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "live paper"):
            resolve_session_directory("paper_broker.jsonl")

    def test_optional_status_does_not_infer_missing_position(self):
        collector = self.collector(runtime_status_path=self.root / "runtime_status.json")
        snapshot = collector.observe_runtime_status()
        self.assertFalse(snapshot["status_exists"])
        self.assertIsNone(snapshot["open_position"])
        self.assertFalse(snapshot["position_state_available"])
        collector.close()

    def test_optional_status_reads_fresh_open_position_flag(self):
        status_path = self.root / "status.json"
        status_path.write_text(
            json.dumps(
                {
                    "process_running": True,
                    "process_heartbeat": self.now.isoformat(),
                    "open_position": True,
                    "paper_only": True,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        collector = self.collector(runtime_status_path=status_path)
        snapshot = collector.observe_runtime_status()
        self.assertTrue(snapshot["position_state_available"])
        self.assertTrue(snapshot["open_position"])
        collector.close()

    def test_no_private_or_order_endpoints_in_collector(self):
        source = (
            Path(__file__).resolve().parents[1] / "quote_tape_collector.py"
        ).read_text(encoding="utf-8").casefold()
        for forbidden in (
            "/api/v3/order",
            "/sapi/",
            "x-mbx-apikey",
            "api_secret",
            "paperbroker",
            "paperruntime",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertEqual(
            BINANCE_BOOK_TICKER_URL,
            "wss://stream.binance.com:9443/ws/btcusdt@bookTicker",
        )
        self.assertEqual(
            binance_kline_stream_url(60),
            "wss://stream.binance.com:9443/ws/btcusdt@kline_1m",
        )

    async def test_reconnect_gap_and_graceful_shutdown(self):
        sockets = [
            ScriptedSocket(
                [self.quote(1)],
                close_error=ConnectionError("quote dropped"),
            ),
            ScriptedSocket([self.quote(2)]),
        ]

        def quote_connector(_url, **_kwargs):
            if sockets:
                return sockets.pop(0)
            return ScriptedSocket([])

        kline_messages = [self.kline(open_time=self.now)]

        def kline_connector(_url, **_kwargs):
            return ScriptedSocket(kline_messages)

        async def instant(_delay):
            return None

        collector = self.collector()
        stop_event = asyncio.Event()

        async def stop_after_reconnect():
            deadline = asyncio.get_running_loop().time() + 2
            while collector.counts.session_gaps < 1:
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("Reconnect gap was not recorded.")
                await asyncio.sleep(0.01)
            stop_event.set()

        await asyncio.wait_for(
            asyncio.gather(
                collector.run(
                    quote_connector=quote_connector,
                    kline_connector=kline_connector,
                    sleep=instant,
                    stop_event=stop_event,
                ),
                stop_after_reconnect(),
            ),
            timeout=3,
        )
        events = [
            json.loads(line)
            for line in (self.root / "session" / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        names = [event["event"] for event in events]
        self.assertIn("disconnect", names)
        self.assertIn("reconnect", names)
        self.assertIn("shutdown", names)
        session = json.loads(
            (self.root / "session" / "session.json").read_text(encoding="utf-8")
        )
        self.assertEqual(session["status"], "stopped")
        self.assertGreaterEqual(collector.counts.quotes_accepted, 2)
        self.assertEqual(collector.counts.candles_accepted, 1)

    def test_periodic_session_metadata_matches_written_quotes(self):
        collector = QuoteTapeCollector(
            output_dir=self.root / "meta",
            session_id="meta-session",
            clock=self.clock,
            session_metadata_interval_seconds=0,
        )
        self.addCleanup(collector.close)
        path = self.root / "meta" / "session.json"
        started = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(started["status"], "starting")
        self.assertEqual(started["quotes_written"], 0)
        collector.ingest_quote(self.quote(1))
        collector.ingest_quote(self.quote(2))
        mid = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(mid["status"], "running")
        self.assertEqual(mid["quotes_written"], 2)
        self.assertEqual(mid["counts"]["quotes_accepted"], 2)
        quotes = (self.root / "meta" / "quotes.jsonl").read_text(encoding="utf-8")
        self.assertEqual(quotes.count("\n"), 2)
        collector.close()
        final = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(final["status"], "stopped")
        self.assertEqual(final["quotes_written"], 2)
        self.assertEqual(final["candles_written"], 0)
        self.assertIn("ended_at_utc", final)

    def test_throttled_metadata_skips_until_interval_elapses(self):
        collector = QuoteTapeCollector(
            output_dir=self.root / "throttle",
            session_id="throttle-session",
            clock=self.clock,
            session_metadata_interval_seconds=5,
        )
        self.addCleanup(collector.close)
        path = self.root / "throttle" / "session.json"
        collector.ingest_quote(self.quote(1))
        early = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(early["quotes_written"], 0)
        self.clock.value = self.now + timedelta(seconds=5)
        collector.ingest_quote(self.quote(2))
        later = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(later["quotes_written"], 2)
        self.assertEqual(later["status"], "running")

    def test_atomic_metadata_keeps_previous_file_if_write_fails(self):
        path = self.root / "atomic.json"
        write_session_json(path, {"ok": True, "n": 1})
        before = path.read_text(encoding="utf-8")
        with patch("quote_tape_collector.json.dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_session_json(path, {"ok": True, "n": 2})
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        leftover = list(self.root.glob(".atomic.json*.tmp"))
        self.assertEqual(leftover, [])

    def test_session_replace_retries_permission_error(self):
        path = self.root / "retry.json"
        write_session_json(path, {"n": 1})
        attempts = {"count": 0}
        real_replace = os.replace

        def flaky(source, destination):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise PermissionError("Access is denied")
            return real_replace(source, destination)

        with patch("quote_tape_collector.os.replace", side_effect=flaky):
            with patch("quote_tape_collector.time.sleep", return_value=None):
                write_session_json(path, {"n": 2})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["n"], 2)
        self.assertGreaterEqual(attempts["count"], 3)

    def test_metadata_permission_error_does_not_drop_accepted_quote(self):
        collector = self.collector(session_metadata_interval_seconds=0)
        self.addCleanup(collector.close)
        self.assertTrue(collector.ingest_quote(self.quote(1))["accepted"])
        with patch(
            "quote_tape_collector.write_session_json",
            side_effect=PermissionError("Access is denied"),
        ):
            parsed = collector.ingest_quote(self.quote(2))
        self.assertTrue(parsed["accepted"])
        quote_lines = (
            self.root / "session" / "quotes.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(quote_lines), 2)
        self.assertEqual(collector.counts.session_metadata_write_failures, 1)
        diagnostics = (
            self.root / "session" / "diagnostics.jsonl"
        ).read_text(encoding="utf-8")
        self.assertIn("session_metadata_write_failed", diagnostics)
        session = json.loads(
            (self.root / "session" / "session.json").read_text(encoding="utf-8")
        )
        self.assertIn("status", session)

    def test_final_counts_match_jsonl_rows(self):
        collector = QuoteTapeCollector(
            output_dir=self.root / "counts",
            session_id="count-session",
            clock=self.clock,
            session_metadata_interval_seconds=0,
        )
        self.addCleanup(collector.close)
        collector.ingest_quote(self.quote(1))
        collector.ingest_quote(self.quote(1))
        collector.ingest_kline(self.kline(open_time=self.now))
        collector.close()
        session = json.loads(
            (self.root / "counts" / "session.json").read_text(encoding="utf-8")
        )
        quote_lines = (self.root / "counts" / "quotes.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        candle_lines = (self.root / "counts" / "candles.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(session["quotes_written"], len(quote_lines))
        self.assertEqual(session["candles_written"], len(candle_lines))
        self.assertEqual(session["counts"]["quotes_duplicate"], 1)
        self.assertEqual(json.loads(candle_lines[0])["symbol"], "BTCUSDT")


if __name__ == "__main__":
    unittest.main()
