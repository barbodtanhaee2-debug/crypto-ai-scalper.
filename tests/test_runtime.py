import asyncio
import json
import signal
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dashboard_data import load_runtime_status
from market_data import (
    BINANCE_BOOK_TICKER_URL,
    binance_kline_stream_url,
)
from paper_broker import PaperBroker, PaperBrokerSettings
from runtime import (
    PaperRuntime,
    RuntimeSettings,
    _CandleSeries,
)
import runtime as runtime_module
from runtime_status import write_runtime_status
from strategy import Bar, SimulatedPosition, StrategyDecision


class MutableClock:
    def __init__(self, value):
        self.value = value
        self._origin = value
        self._monotonic_origin = 1000.0
        self._monotonic_calls = 0

    def __call__(self):
        return self.value

    def monotonic(self):
        self._monotonic_calls += 1
        return self._monotonic_origin + (
            self.value - self._origin
        ).total_seconds() + self._monotonic_calls * 0.01


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        self.clock = MutableClock(self.now)
        self.broker = PaperBroker(
            PaperBrokerSettings(journal_path=self.root / "paper.jsonl")
        )
        self.runtime = PaperRuntime(
            RuntimeSettings(
                journal_path=self.root / "paper.jsonl",
                status_path=self.root / "status.json",
                heartbeat_interval_seconds=0.01,
                mark_interval_seconds=1,
                market_stale_after_seconds=3,
                max_reconnect_attempts=0,
            ),
            broker=self.broker,
            clock=self.clock,
            monotonic=self.clock.monotonic,
        )
        self.runtime._exchange_clock.synchronize(
            lambda: self.clock.value,
        )
        self.runtime._running = True

    def tearDown(self):
        self.tempdir.cleanup()

    def test_runtime_rejects_unsupported_binance_kline_interval(self):
        for interval_seconds in (120, 600, 3600):
            with self.subTest(interval=interval_seconds):
                with self.assertRaisesRegex(ValueError, "Unsupported Binance"):
                    RuntimeSettings(candle_interval_seconds=interval_seconds)

    @staticmethod
    def message(bid="99.99", ask="100.01"):
        return json.dumps({"s": "BTCUSDT", "b": bid, "a": ask})

    def send_quote(self, timestamp=None, bid="99.99", ask="100.01"):
        if timestamp is not None:
            self.clock.value = timestamp
            self.runtime._exchange_clock.synchronize(lambda: self.clock.value)
        if self.runtime._feed.connection_status != "connected":
            self.runtime._feed.mark_connected()
        accepted = self.runtime._feed.process_message(self.message(bid, ask))
        self.runtime._handle_update(
            accepted,
            self.runtime._feed.health(now=self.clock.value),
        )
        return accepted

    def seed_test_candles(self, count=21, latest_timestamp=None):
        latest_timestamp = latest_timestamp or self.clock.value
        bars = [
            Bar(
                latest_timestamp - timedelta(minutes=count - index - 1),
                100,
                100.1,
                99.9,
                100,
            )
            for index in range(count)
        ]
        self.set_candle_history(bars)
        return bars

    def set_candle_history(self, bars):
        self.runtime._candle_series.seed(
            bars,
            now=self.clock.value,
            maximum_age_seconds=self.runtime.settings.candle_max_age_seconds,
        )
        self.runtime._bars = list(self.runtime._candle_series.bars)
        self.runtime._latest_completed_bar = self.runtime._bars[-1]
        feed = self.runtime._candle_feed
        feed.mark_connected()
        feed.last_closed_candle = self.runtime._latest_completed_bar
        feed.last_valid_update = self.clock.value
        feed.last_error = None
        feed._valid_since_connect = True
        self.runtime._last_candle_feed_status = "connected"

    def test_historical_and_live_candles_deduplicate_and_continue_contiguously(self):
        self.seed_test_candles()
        last_history_bar = self.runtime._bars[-1]

        self.assertEqual(self.runtime._candle_series.append(last_history_bar), "duplicate")
        self.assertEqual(
            self.runtime._candle_series.append(self.runtime._bars[5]),
            "duplicate",
        )
        self.assertEqual(len(self.runtime._candle_series.bars), 21)
        next_bar = Bar(
            last_history_bar.timestamp + timedelta(minutes=1),
            100,
            100.2,
            99.8,
            100.1,
        )
        self.assertEqual(self.runtime._candle_series.append(next_bar), "added")
        self.assertEqual(len(self.runtime._candle_series.bars), 22)

    def test_buffered_live_candles_reconcile_with_historical_seed(self):
        history = self.seed_test_candles(
            latest_timestamp=self.now - timedelta(minutes=1)
        )
        self.runtime._candle_history_ready = False
        self.send_quote()
        live_bar = Bar(self.now, 100, 100.2, 99.8, 100.1)
        self.runtime._candle_feed.last_closed_candle = live_bar
        self.runtime._candle_feed.last_valid_update = self.now
        self.runtime._pending_candles = [history[-2], history[-1], live_bar]

        self.runtime._handle_history(history, None, self.now)
        first_evaluation = self.runtime._last_strategy_evaluation_timestamp
        self.runtime._on_completed_bar(
            live_bar,
            self.now,
            self.runtime._feed.health(now=self.now),
        )

        self.assertEqual(len(self.runtime._bars), 22)
        self.assertEqual(self.runtime._bars[-1], live_bar)
        self.assertIsNotNone(first_evaluation)
        self.assertEqual(
            self.runtime._last_strategy_evaluation_timestamp,
            first_evaluation,
        )

    def test_invalid_live_kline_resets_warmup_until_history_is_rebuilt(self):
        self.seed_test_candles()
        self.runtime._candle_history_ready = True
        next_bar = Bar(
            self.now + timedelta(minutes=1),
            100,
            100.2,
            99.8,
            100.1,
        )
        self.clock.value = next_bar.timestamp
        self.runtime._exchange_clock.synchronize(lambda: self.clock.value)
        self.runtime._handle_candle_update(
            False, None, self.clock.value
        )
        feed = self.runtime._candle_feed
        feed.last_closed_candle = next_bar
        feed.last_valid_update = self.clock.value
        feed.last_error = None
        feed._valid_since_connect = True

        self.runtime._handle_candle_update(
            True, next_bar, self.clock.value
        )

        self.assertEqual(self.runtime._bars, [next_bar])
        self.assertFalse(
            self.runtime._entry_gate(
                self.clock.value,
                self.runtime._feed.health(now=self.clock.value),
            )[0]
        )

    def test_invalid_live_kline_cannot_be_followed_by_stale_history_seed(self):
        history = self.seed_test_candles()
        self.runtime._candle_series.clear()
        self.runtime._bars.clear()
        self.runtime._latest_completed_bar = None
        self.runtime._candle_history_ready = False
        self.runtime._handle_candle_update(False, None, self.now)

        self.runtime._handle_history(history, None, self.now)

        self.assertEqual(self.runtime._bars, [])
        self.assertTrue(self.runtime._candle_history_ready)

    def test_gap_resets_history_to_real_live_bar_without_fabrication(self):
        self.seed_test_candles()
        next_bar = Bar(
            self.runtime._bars[-1].timestamp + timedelta(minutes=2),
            100,
            100.2,
            99.8,
            100.1,
        )

        self.assertEqual(self.runtime._candle_series.append(next_bar), "gap")
        self.assertEqual(self.runtime._candle_series.bars, [next_bar])

    def test_out_of_order_candle_clears_history(self):
        self.seed_test_candles()
        earlier = Bar(
            self.runtime._bars[0].timestamp - timedelta(minutes=1),
            100,
            100.1,
            99.9,
            100,
        )

        with self.assertRaisesRegex(ValueError, "Out-of-order"):
            self.runtime._candle_series.append(earlier)
        self.assertEqual(self.runtime._candle_series.bars, [])

    def test_runtime_status_distinguishes_received_valid_and_rejected_messages(self):
        self.send_quote()
        valid_timestamp = self.runtime._feed.last_valid_update
        self.runtime._feed.process_message(self.message("101", "100"))
        self.runtime._handle_update(
            False,
            self.runtime._feed.health(now=self.clock.value),
        )
        self.runtime._write_status(
            self.clock.value,
            self.runtime._feed.health(now=self.clock.value),
            self.runtime._last_decision_reason or "Heartbeat snapshot.",
        )

        status = json.loads(
            (self.root / "status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(status["websocket_messages_received"], 2)
        self.assertEqual(status["websocket_messages_accepted"], 1)
        self.assertEqual(status["websocket_messages_rejected"], 1)
        self.assertFalse(status["last_websocket_message_accepted"])
        self.assertEqual(
            status["last_websocket_message_timestamp"],
            self.now.isoformat(),
        )
        self.assertEqual(
            status["latest_valid_market_data_timestamp"],
            valid_timestamp.isoformat(),
        )
        self.assertIn("Ask must be greater", status["last_market_data_error"])
        self.assertFalse(status["entry_permission"])

    def test_queued_valid_quotes_are_processed_in_order_during_feed_bursts(self):
        first_time = self.now
        queued_updates = []
        self.runtime._feed.mark_connected()
        for offset, bid in enumerate(("99.99", "100.00", "100.01")):
            received_at = first_time + timedelta(seconds=offset)
            self.clock.value = received_at
            self.runtime._exchange_clock.synchronize(lambda: self.clock.value)
            accepted = self.runtime._feed.process_message(
                self.message(bid, str(float(bid) + 0.02))
            )
            queued_updates.append(
                (accepted, self.runtime._feed.health(now=received_at))
            )

        latest_feed_time = self.runtime._feed.last_valid_update
        self.clock.value = first_time + timedelta(seconds=2)
        self.runtime._exchange_clock.synchronize(lambda: self.clock.value)
        current_health = self.runtime._feed.health(now=self.clock.value)
        for index, (accepted, event_health) in enumerate(queued_updates):
            self.runtime._handle_update(accepted, event_health)
            if index == 0:
                self.runtime._write_status(
                    self.clock.value,
                    current_health,
                    self.runtime._last_decision_reason or "Processing queued quote.",
                )
                intermediate_status = json.loads(
                    (self.root / "status.json").read_text(encoding="utf-8")
                )
                reported_timestamp = datetime.fromisoformat(
                    intermediate_status["latest_valid_market_data_timestamp"]
                )
                processed_timestamp = datetime.fromisoformat(
                    intermediate_status[
                        "latest_processed_market_data_timestamp"
                    ]
                )
                self.assertEqual(
                    intermediate_status["market_data_age_seconds"],
                    (self.clock.value - reported_timestamp).total_seconds(),
                )
                self.assertEqual(processed_timestamp, first_time)
                self.assertEqual(reported_timestamp, latest_feed_time)

        self.assertEqual(self.runtime._latest_valid_market_timestamp, latest_feed_time)
        self.assertTrue(current_health["healthy"])
        self.assertFalse(self.runtime._entry_permission)
        self.assertIn("Kline feed", self.runtime._last_decision_reason)
        self.runtime._write_status(
            self.clock.value,
            current_health,
            self.runtime._last_decision_reason,
        )

        status = json.loads(
            (self.root / "status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            status["latest_valid_market_data_timestamp"],
            latest_feed_time.isoformat(),
        )
        self.assertEqual(
            status["latest_processed_market_data_timestamp"],
            latest_feed_time.isoformat(),
        )

    def test_silent_open_feed_reports_connected_but_stale_without_new_messages(self):
        self.send_quote()
        self.seed_test_candles()
        last_message_time = self.runtime._feed.last_message_timestamp
        self.clock.value += timedelta(seconds=4)
        stale_health = self.runtime._feed.health(now=self.clock.value)
        allowed, reason = self.runtime._entry_gate(
            self.clock.value, stale_health
        )
        self.runtime._write_status(
            self.clock.value,
            stale_health,
            str(stale_health["reason"]),
        )

        self.assertFalse(allowed)
        self.assertIn("stale", reason.lower())
        status = json.loads(
            (self.root / "status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(status["connection_status"], "connected")
        self.assertTrue(status["entry_diagnostics"]["strategy_warmup_ready"])
        self.assertFalse(status["market_data_healthy"])
        self.assertFalse(status["entry_permission"])
        self.assertGreater(status["market_data_age_seconds"], 3)
        self.assertEqual(status["websocket_messages_received"], 1)
        self.assertEqual(
            status["last_websocket_message_timestamp"],
            last_message_time.isoformat(),
        )
        self.assertTrue(status["last_websocket_message_accepted"])
        self.assertIsNone(status["last_market_data_error"])
        quote_diagnostics = status["quote_diagnostics"]
        self.assertEqual(quote_diagnostics["messages_received"], 1)
        self.assertEqual(quote_diagnostics["valid_quotes_accepted"], 1)
        self.assertEqual(quote_diagnostics["invalid_messages_rejected"], 0)
        self.assertEqual(quote_diagnostics["connection_state"], "connected")
        self.assertGreater(quote_diagnostics["last_receive_age_seconds"], 3)

    def test_quote_diagnostics_track_receives_rejections_and_reconnects(self):
        feed = self.runtime._feed
        feed.mark_connecting()
        feed.mark_connected()

        valid = self.message()
        invalid = "{malformed"
        self.assertTrue(feed.process_message(valid))
        accepted_at = feed.last_valid_update
        self.assertFalse(feed.process_message(invalid))
        self.assertEqual(feed.last_valid_update, accepted_at)
        snapshot = feed.diagnostic_snapshot()
        self.assertEqual(snapshot["messages_received"], 2)
        self.assertEqual(snapshot["valid_quotes_accepted"], 1)
        self.assertEqual(snapshot["invalid_messages_rejected"], 1)
        self.assertEqual(snapshot["rejection_reasons"], {"malformed_json": 1})
        self.assertEqual(snapshot["last_rejection_reason"], "malformed_json")
        self.assertIsNotNone(snapshot["last_receive_at"])
        self.assertIsNotNone(snapshot["last_accepted_quote_at"])
        self.assertIsNotNone(snapshot["last_processing_duration_seconds"])
        self.assertFalse(feed.health(now=self.clock.value)["entry_allowed"])

        feed.mark_disconnected("test disconnect")
        feed.mark_reconnecting("test reconnect")
        feed.mark_connecting()
        feed.mark_connected()
        reconnect_snapshot = feed.diagnostic_snapshot()
        self.assertEqual(reconnect_snapshot["reconnect_count"], 1)
        self.assertEqual(reconnect_snapshot["connection_attempts"], 2)
        self.assertEqual(reconnect_snapshot["messages_received"], 2)
        self.assertEqual(reconnect_snapshot["connection_messages_received"], 0)
        self.assertEqual(reconnect_snapshot["invalid_messages_rejected"], 1)
        self.assertFalse(feed.health(now=self.clock.value)["entry_allowed"])

    def test_quote_diagnostics_report_runtime_queue_delay(self):
        self.send_quote()
        queued_at = self.clock.monotonic()
        self.runtime._handle_update(
            True,
            self.runtime._feed.health(now=self.clock.value),
            queued_monotonic=queued_at,
        )
        status = self.runtime._status_record(
            now=self.clock.value,
            health=self.runtime._feed.health(now=self.clock.value),
            reason="test diagnostics",
        )
        diagnostics = status["quote_diagnostics"]
        self.assertGreaterEqual(
            diagnostics["runtime_queue_delay_seconds"], 0
        )
        self.assertGreaterEqual(
            diagnostics["runtime_event_loop_lag_seconds"], 0
        )
        self.assertIn("runtime_update_queue_depth", diagnostics)

    def test_quote_updates_do_not_write_status_for_each_message(self):
        self.seed_test_candles()
        status_writes = []
        self.runtime._status_writer = (
            lambda path, status: status_writes.append(dict(status))
        )

        self.send_quote()
        self.assertTrue(self.runtime._entry_permission)
        self.assertEqual(status_writes, [])

        self.runtime._write_status(
            self.clock.value,
            self.runtime._feed.health(now=self.clock.value),
            "Heartbeat snapshot.",
        )
        self.assertEqual(len(status_writes), 1)
        self.assertTrue(status_writes[0]["entry_permission"])

    def test_market_health_and_completed_candle_are_both_required_for_entries(self):
        self.send_quote()
        health = self.runtime._feed.health(now=self.clock.value)
        self.assertFalse(self.runtime._entry_gate(self.clock.value, health)[0])
        self.assertIn("kline", self.runtime._entry_gate(
            self.clock.value, health
        )[1].lower())

        self.seed_test_candles()
        self.runtime._latest_valid_market_timestamp = self.now
        self.assertTrue(self.runtime._entry_gate(self.now, health)[0])

        stale = self.now + timedelta(seconds=4)
        self.assertFalse(
            self.runtime._entry_gate(stale, self.runtime._feed.health(now=stale))[0]
        )

    def test_status_reports_strategy_warmup_and_other_entry_gate_conditions(self):
        self.send_quote()
        health = self.runtime._feed.health(now=self.clock.value)
        status = self.runtime._status_record(
            now=self.clock.value,
            health=health,
            reason="test",
        )
        diagnostics = status["entry_diagnostics"]
        self.assertEqual(diagnostics["completed_candle_count"], 0)
        self.assertEqual(diagnostics["required_completed_candle_count"], 21)
        self.assertEqual(diagnostics["warmup_candles_remaining"], 21)
        self.assertFalse(diagnostics["completed_candle_fresh"])
        self.assertFalse(diagnostics["open_position"])
        self.assertFalse(diagnostics["risk_lockout"])
        self.assertTrue(any(
            "Kline feed" in blocker or "completed kline" in blocker
            for blocker in diagnostics["readiness_blockers"]
        ))
        self.assertTrue(any(
            "Strategy warm-up needs 21 contiguous completed bars" in blocker
            for blocker in diagnostics["readiness_blockers"]
        ))

    def test_entries_remain_blocked_when_exchange_history_has_short_warmup(self):
        first = self.now - timedelta(minutes=1)
        previous = Bar(first, 100, 100.1, 99.9, 100)
        current = Bar(self.now, 100, 100.1, 99.9, 100)
        self.send_quote()
        self.set_candle_history([previous])
        health = self.runtime._feed.health(now=self.now)

        self.runtime._on_completed_bar(current, self.now, health)
        self.runtime._write_status(self.now, health, "Testing warm-up gate.")

        status = json.loads(
            (self.root / "status.json").read_text(encoding="utf-8")
        )
        diagnostics = status["entry_diagnostics"]
        self.assertFalse(status["entry_permission"])
        self.assertEqual(diagnostics["completed_candle_count"], 2)
        self.assertEqual(diagnostics["required_completed_candle_count"], 21)
        self.assertEqual(diagnostics["warmup_candles_remaining"], 19)
        self.assertFalse(diagnostics["strategy_warmup_ready"])
        self.assertIn(
            "Strategy warm-up needs 21 contiguous completed bars",
            status["entry_permission_reason"],
        )

    def test_readiness_diagnostics_identify_open_position_and_risk_lockout(self):
        self.send_quote()
        self._open_test_position()
        self.broker.last_bid = 94.0
        self.broker.last_ask = 94.02
        health = self.runtime._feed.health(now=self.now)

        diagnostics = self.runtime._entry_diagnostics(self.now, health)

        self.assertTrue(diagnostics["open_position"])
        self.assertTrue(diagnostics["risk_lockout"])
        self.assertIn(
            "An open simulated position blocks another entry.",
            diagnostics["readiness_blockers"],
        )
        self.assertIn(
            "Broker risk limit lockout is active.",
            diagnostics["readiness_blockers"],
        )

    def test_kline_disconnection_and_reconnect_clear_candle_warmup(self):
        self.seed_test_candles()
        self.runtime._candle_feed.mark_disconnected("test disconnect")
        self.runtime._observe_candle_connection_status("disconnected")
        self.assertEqual(self.runtime._bars, [])
        self.assertIsNone(self.runtime._latest_completed_bar)

        self.runtime._candle_feed.mark_connecting()
        self.runtime._observe_candle_connection_status("connecting")
        self.runtime._candle_feed.mark_connected()
        self.runtime._observe_candle_connection_status("connected")
        self.send_quote(self.now + timedelta(seconds=1))
        self.assertEqual(self.runtime._bars, [])
        health = self.runtime._feed.health(now=self.clock.value)
        self.assertFalse(self.runtime._entry_gate(self.clock.value, health)[0])

    def test_failed_reconnect_reseed_keeps_warmup_and_entries_blocked(self):
        self.seed_test_candles()
        self.runtime._observe_candle_connection_status("disconnected")
        self.runtime._observe_candle_connection_status("connecting")
        self.runtime._candle_feed.mark_connected()
        should_reseed = self.runtime._observe_candle_connection_status("connected")
        generation = self.runtime._candle_generation
        self.assertTrue(should_reseed)

        self.runtime._handle_history(
            None,
            "offline during test",
            self.now,
            generation=generation,
        )

        self.assertEqual(self.runtime._bars, [])
        self.assertTrue(self.runtime._candle_history_ready)
        allowed, _reason = self.runtime._entry_gate(
            self.now, self.runtime._feed.health(now=self.now)
        )
        self.assertFalse(allowed)

    def test_entries_recover_after_reconnect_reseed_and_fresh_completed_candle(self):
        self.send_quote()
        history = self.seed_test_candles(
            latest_timestamp=self.now - timedelta(minutes=1)
        )
        self.assertTrue(
            self.runtime._entry_gate(
                self.now, self.runtime._feed.health(now=self.now)
            )[0]
        )

        self.runtime._candle_feed.mark_disconnected("test reconnect")
        self.runtime._observe_candle_connection_status("disconnected")
        self.runtime._candle_feed.mark_connecting()
        self.runtime._observe_candle_connection_status("connecting")
        self.runtime._candle_feed.mark_connected()
        self.assertTrue(
            self.runtime._observe_candle_connection_status("connected")
        )
        generation = self.runtime._candle_generation

        self.runtime._handle_history(
            history,
            None,
            self.now,
            generation=generation,
        )
        self.assertEqual(len(self.runtime._bars), 21)
        self.assertFalse(
            self.runtime._entry_gate(
                self.now, self.runtime._feed.health(now=self.now)
            )[0]
        )

        live_bar = Bar(self.now, 100, 100.1, 99.9, 100)
        self.runtime._candle_feed.last_closed_candle = live_bar
        self.runtime._candle_feed.last_valid_update = self.now
        self.runtime._candle_feed._valid_since_connect = True
        self.runtime._handle_candle_update(True, live_bar, self.now)

        self.assertEqual(len(self.runtime._bars), 22)
        self.assertTrue(
            self.runtime._entry_gate(
                self.now, self.runtime._feed.health(now=self.now)
            )[0]
        )

    def test_invalid_exchange_clock_blocks_entries_until_fresh_calibration(self):
        self.send_quote()
        self.seed_test_candles()
        health = self.runtime._feed.health(now=self.now)
        self.assertTrue(self.runtime._entry_gate(self.now, health)[0])

        with self.assertRaisesRegex(ValueError, "exceeds the permitted"):
            self.runtime._exchange_clock.synchronize(
                lambda: self.now + timedelta(seconds=6)
            )
        self.assertFalse(self.runtime._entry_gate(self.now, health)[0])

        self.runtime._exchange_clock.synchronize(lambda: self.now)
        self.assertTrue(self.runtime._entry_gate(self.now, health)[0])

    def test_runtime_five_minute_interval_keeps_21_bar_readiness_gate(self):
        runtime = PaperRuntime(
            RuntimeSettings(
                candle_interval_seconds=300,
                journal_path=self.root / "five-minute.jsonl",
                status_path=self.root / "five-minute-status.json",
            ),
            clock=self.clock,
            monotonic=self.clock.monotonic,
        )
        runtime._exchange_clock.synchronize(lambda: self.clock.value)
        runtime._running = True
        runtime._feed.mark_connected()
        runtime._feed.process_message(self.message())
        runtime._candle_feed.mark_connected()
        self.assertEqual(runtime._required_bar_count(), 21)

        def history(count):
            first_close = self.now - timedelta(minutes=5 * (count - 1), milliseconds=1)
            return [
                Bar(
                    first_close + timedelta(seconds=300 * index),
                    100,
                    100.1,
                    99.9,
                    100,
                )
                for index in range(count)
            ]

        runtime._candle_series.seed(
            history(20),
            now=self.now,
            maximum_age_seconds=runtime.settings.candle_max_age_seconds,
        )
        runtime._bars = list(runtime._candle_series.bars)
        runtime._latest_completed_bar = runtime._bars[-1]
        runtime._candle_feed.last_closed_candle = runtime._bars[-1]
        runtime._candle_feed.last_valid_update = self.now
        runtime._candle_feed._valid_since_connect = True
        runtime._last_candle_feed_status = "connected"
        runtime._latest_valid_market_timestamp = self.now

        self.assertFalse(
            runtime._entry_gate(self.now, runtime._feed.health(now=self.now))[0]
        )
        runtime._candle_series.seed(
            history(21),
            now=self.now,
            maximum_age_seconds=runtime.settings.candle_max_age_seconds,
        )
        runtime._bars = list(runtime._candle_series.bars)
        runtime._latest_completed_bar = runtime._bars[-1]
        runtime._candle_feed.last_closed_candle = runtime._bars[-1]

        self.assertTrue(
            runtime._entry_gate(self.now, runtime._feed.health(now=self.now))[0]
        )

    def test_quote_disconnect_blocks_entries_without_destroying_candle_history(self):
        self.seed_test_candles()
        self.send_quote()
        self.runtime._feed.mark_disconnected("test quote disconnect")
        health = self.runtime._feed.health(now=self.clock.value)
        self.runtime._observe_connection_status("disconnected")

        self.assertFalse(self.runtime._entry_gate(self.clock.value, health)[0])
        self.assertEqual(len(self.runtime._bars), 21)

    def test_stale_quote_blocks_entries_even_with_fresh_candle_history(self):
        self.seed_test_candles()
        self.send_quote()
        stale_time = self.now + timedelta(seconds=4)
        self.clock.value = stale_time
        self.runtime._exchange_clock.synchronize(lambda: self.clock.value)

        allowed, reason = self.runtime._entry_gate(
            stale_time,
            self.runtime._feed.health(now=stale_time),
        )

        self.assertFalse(allowed)
        self.assertIn("stale", reason.lower())

    def test_invalid_data_does_not_close_an_open_position_or_create_a_fill(self):
        self.send_quote()
        position = self._open_test_position()
        self.runtime._feed.process_message(self.message("101", "100"))
        self.runtime._handle_update(
            False,
            self.runtime._feed.health(now=self.clock.value),
        )
        self.assertIs(self.broker.position, position)
        events = [
            json.loads(line)
            for line in (self.root / "paper.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertFalse(any(item["event"] == "trade_closed" for item in events))
        self.assertFalse(self.runtime._entry_permission)

    def test_signal_uses_existing_paper_broker_and_one_position_limit(self):
        self.send_quote(self.now + timedelta(minutes=20), "100.39", "100.41")
        previous = [
            Bar(
                self.now + timedelta(minutes=index),
                100,
                100.2,
                99.8,
                100,
            )
            for index in range(20)
        ]
        self.set_candle_history(previous)
        self.runtime._latest_valid_market_timestamp = self.clock.value
        decision_bar = Bar(
            self.clock.value, 100.3, 100.4, 99.8, 100.4
        )
        health = self.runtime._feed.health(now=self.clock.value)
        self.runtime._on_completed_bar(decision_bar, self.clock.value, health)
        self.runtime._on_completed_bar(decision_bar, self.clock.value, health)

        self.assertIsNotNone(self.broker.position)
        self.assertEqual(self.broker.position.strategy, "momentum_breakout")
        self.assertEqual(self.broker.position.direction, "long")
        self.assertFalse(self.runtime._entry_permission)
        self.assertEqual(
            json.loads((self.root / "status.json").read_text(encoding="utf-8"))[
                "entry_permission"
            ],
            False,
        )
        events = [
            json.loads(line)
            for line in (self.root / "paper.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(
            sum(event["event"] == "entry_fill" for event in events),
            1,
        )

    def test_fresh_completed_bar_manages_a_simulated_stop_exit(self):
        entry_time = self.now + timedelta(minutes=20)
        self.send_quote(entry_time, "100.39", "100.41")
        previous = [
            Bar(
                self.now + timedelta(minutes=index),
                100,
                100.2,
                99.8,
                100,
            )
            for index in range(20)
        ]
        self.set_candle_history(previous)
        self.runtime._latest_valid_market_timestamp = entry_time
        self.runtime._on_completed_bar(
            Bar(entry_time, 100.3, 100.4, 99.8, 100.4),
            entry_time,
            self.runtime._feed.health(now=entry_time),
        )
        self.assertIsNotNone(self.broker.position)

        exit_time = entry_time + timedelta(minutes=1)
        self.send_quote(exit_time, "100.18", "100.20")
        self.runtime._on_completed_bar(
            Bar(exit_time, 100.3, 100.4, 100.1, 100.2),
            exit_time,
            self.runtime._feed.health(now=exit_time),
        )
        self.assertIsNone(self.broker.position)
        events = [
            json.loads(line)
            for line in (self.root / "paper.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(any(event["event"] == "trade_closed" for event in events))

    def test_fresh_quote_triggers_long_stop_before_a_candle_completes(self):
        self.send_quote()
        self._open_test_position()
        exit_time = self.now + timedelta(seconds=1)

        self.assertTrue(self.send_quote(exit_time, "99.79", "99.81"))

        self.assertIsNone(self.broker.position)
        events = [
            json.loads(line)
            for line in (self.root / "paper.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        closed = [event for event in events if event["event"] == "trade_closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["details"]["trigger"], "stop_loss")
        self.assertAlmostEqual(
            closed[0]["details"]["exit_fill_price"],
            99.79 * (1 - self.broker.settings.slippage_rate_per_side),
        )

    def test_fresh_quote_triggers_long_target_once_using_bid(self):
        self.send_quote()
        self._open_test_position()
        exit_time = self.now + timedelta(seconds=1)

        self.assertTrue(self.send_quote(exit_time, "101.01", "101.03"))
        same_quote = self.runtime._feed.health(now=exit_time)
        self.runtime._handle_update(True, same_quote)

        events = [
            json.loads(line)
            for line in (self.root / "paper.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        closed = [event for event in events if event["event"] == "trade_closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["details"]["trigger"], "take_profit")
        self.assertAlmostEqual(
            closed[0]["details"]["exit_fill_price"],
            101.01 * (1 - self.broker.settings.slippage_rate_per_side),
        )
        self.assertEqual(
            sum(event["event"] == "entry_fill" for event in events), 1
        )

    def test_unchanged_runtime_quotes_do_not_duplicate_broker_snapshots(self):
        self.assertTrue(self.send_quote(self.now))
        first_events = [
            json.loads(line)
            for line in (self.root / "paper.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        first_mark_count = sum(
            event["event"] == "mark_to_market" for event in first_events
        )
        self.assertEqual(first_mark_count, 1)

        self.assertTrue(
            self.send_quote(self.now + timedelta(seconds=2))
        )
        later_events = [
            json.loads(line)
            for line in (self.root / "paper.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(
            sum(event["event"] == "mark_to_market" for event in later_events),
            first_mark_count,
        )

    def test_pending_exit_is_reported_and_resolved_on_a_later_fresh_quote(self):
        self.send_quote()
        self._open_test_position()
        trigger_time = self.now + timedelta(minutes=1)
        pending = self.broker.process(
            StrategyDecision(
                "exit",
                "test_strategy",
                "long",
                trigger_time,
                "Stop reached.",
                {"exit_price": 99.8},
            ),
            market_health=self.runtime._feed.health(now=self.now),
        )

        self.assertEqual(pending.action, "exit_pending")
        allowed, reason = self.runtime._entry_gate(
            self.now, self.runtime._feed.health(now=self.now)
        )
        self.assertFalse(allowed)
        self.assertIn("pending fresh-quote verification", reason)
        self.runtime._write_status(
            self.now, self.runtime._feed.health(now=self.now), reason
        )
        status = json.loads((self.root / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["exit_pending"]["trigger"], "stop_loss")
        self.assertTrue(status["exit_pending"]["unresolved_simulated_exposure"])
        self.assertFalse(status["exit_pending"]["loss_bound_guaranteed"])
        self.assertFalse(status["exit_pending"]["fill_fabricated_from_stale_quote"])
        self.assertTrue(status["exit_pending"]["unresolved_loss_possible"])
        self.assertEqual(status["exit_pending"]["age_seconds"], 0.0)

        self.assertTrue(
            self.send_quote(trigger_time + timedelta(seconds=1), "99.9", "99.92")
        )
        self.assertIsNone(self.broker.position)
        events = [
            json.loads(line)
            for line in (self.root / "paper.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(sum(event["event"] == "trade_closed" for event in events), 1)

    def test_entry_journal_failure_rolls_back_position_and_latches_status(self):
        timestamp = self.now + timedelta(minutes=20)
        self.send_quote(timestamp, "100.39", "100.41")
        previous = [
            Bar(
                self.now + timedelta(minutes=index),
                100,
                100.2,
                99.8,
                100,
            )
            for index in range(20)
        ]
        self.set_candle_history(previous)
        self.runtime._latest_valid_market_timestamp = timestamp
        original_record = self.broker._record

        def fail_entry_record(event, *args, **kwargs):
            if event == "entry_fill":
                raise OSError("simulated journal failure")
            return original_record(event, *args, **kwargs)

        self.broker._record = fail_entry_record
        bar = Bar(timestamp, 100.3, 100.4, 99.8, 100.4)
        self.runtime._on_completed_bar(
            bar, timestamp, self.runtime._feed.health(now=timestamp)
        )

        self.assertIsNone(self.broker.position)
        self.assertFalse(self.runtime._journal_ok)
        self.assertFalse(self.runtime._entry_permission)
        status = json.loads((self.root / "status.json").read_text(encoding="utf-8"))
        self.assertFalse(status["entry_permission"])
        self.assertFalse(status["journal_ok"])

    def test_journal_and_status_write_failures_fail_closed(self):
        self.broker.mark_to_market = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("journal unavailable")
        )
        self.send_quote()
        self.assertFalse(self.runtime._journal_ok)
        self.assertFalse(self.runtime._entry_permission)

        failing_runtime = PaperRuntime(
            RuntimeSettings(status_path=self.root / "unwritten.json"),
            broker=self.broker,
            clock=self.clock,
            status_writer=lambda *_args: (_ for _ in ()).throw(
                OSError("status unavailable")
            ),
        )
        failing_runtime._running = True
        health = {"healthy": True, "entry_allowed": True}
        self.assertFalse(failing_runtime._write_status(self.now, health))
        self.assertFalse(failing_runtime._entry_permission)
        self.assertFalse(failing_runtime._entry_gate(self.now, health)[0])

    def test_runtime_status_parser_masks_stale_and_malformed_records(self):
        status_path = self.root / "status.json"
        write_runtime_status(status_path, {
            "process_running": True,
            "process_heartbeat": self.now.isoformat(),
            "connection_status": "connected",
            "latest_valid_market_data_timestamp": self.now.isoformat(),
            "market_data_healthy": True,
            "market_data_age_seconds": 0.2,
            "market_data_stale_limit_seconds": 3,
            "spread": 0.02,
            "entry_permission": True,
            "paper_only": True,
            "risk_lockout": False,
            "open_position": False,
            "journal_ok": True,
            "status_write_ok": True,
            "candle_fresh": True,
            "candle_connection_status": "connected",
            "candle_data_healthy": True,
            "candle_data_stale_limit_seconds": 90,
            "latest_completed_candle_timestamp": self.now.isoformat(),
            "entry_diagnostics": {
                "completed_candle_count": 21,
                "required_completed_candle_count": 21,
                "candle_history_contiguous": True,
                "strategy_warmup_ready": True,
            },
        })
        fresh = load_runtime_status(
            status_path, now=self.now + timedelta(seconds=1)
        )
        self.assertTrue(fresh.process_running)
        self.assertTrue(fresh.data["entry_permission"])

        stale = load_runtime_status(
            status_path, now=self.now + timedelta(seconds=10)
        )
        self.assertFalse(stale.process_running)
        self.assertFalse(stale.data["entry_permission"])
        self.assertIsNone(stale.data["connection_status"])
        self.assertIsNotNone(stale.data["last_recorded_market_data_timestamp"])

        status_path.write_text("{invalid", encoding="utf-8")
        malformed = load_runtime_status(status_path, now=self.now)
        self.assertIsNone(malformed.process_running)
        self.assertIsNone(malformed.data)
        self.assertTrue(malformed.errors)

    def test_missing_runtime_fields_cannot_grant_entry_permission(self):
        status_path = self.root / "incomplete.json"
        status_path.write_text(
            json.dumps({
                "process_running": True,
                "process_heartbeat": self.now.isoformat(),
                "market_data_healthy": True,
                "entry_permission": True,
                "connection_status": "connected",
                "latest_valid_market_data_timestamp": self.now.isoformat(),
            }),
            encoding="utf-8",
        )
        parsed = load_runtime_status(status_path, now=self.now)
        self.assertTrue(parsed.process_running)
        self.assertFalse(parsed.data["entry_permission"])
        self.assertFalse(parsed.data["market_data_healthy"])

    def _open_test_position(self):
        timestamp = self.clock.value
        position = self._entry_position(timestamp)
        result = self.broker.process(
            position,
            market_health=self.runtime._feed.health(now=timestamp),
        )
        self.assertTrue(result.accepted)
        return self.broker.position

    @staticmethod
    def _entry_position(timestamp):
        from strategy import SimulatedPosition

        simulated = SimulatedPosition(
            "test_strategy",
            "long",
            100.01,
            0.5,
            99.8,
            101,
            timestamp,
        )
        return StrategyDecision(
            "entry",
            "test_strategy",
            "long",
            timestamp,
            "synthetic position for runtime test",
            {},
            simulated,
        )


class RuntimeShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_signals_shutdown_once_and_preserve_open_paper_position(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc)
            journal_path = root / "paper.jsonl"
            status_path = root / "status.json"
            broker = PaperBroker(PaperBrokerSettings(journal_path=journal_path))
            position = SimulatedPosition(
                "test_strategy",
                "long",
                100.01,
                0.01,
                99.8,
                101.0,
                now,
            )
            entry = StrategyDecision(
                "entry",
                "test_strategy",
                "long",
                now,
                "synthetic isolated shutdown test entry",
                {},
                position,
            )
            quote = {
                "healthy": True,
                "entry_allowed": True,
                "connection_status": "connected",
                "bid": 99.99,
                "ask": 100.01,
                "spread": 0.02,
                "message_timestamp": now.isoformat(),
                "last_valid_update": now.isoformat(),
                "age_seconds": 0.0,
            }
            self.assertTrue(broker.process(entry, market_health=quote).accepted)
            journal_before_shutdown = journal_path.read_bytes()
            runtime = PaperRuntime(
                RuntimeSettings(
                    journal_path=journal_path,
                    status_path=status_path,
                    heartbeat_interval_seconds=0.01,
                    max_reconnect_attempts=0,
                ),
                broker=broker,
                clock=lambda: now,
            )

            feeds_connected = asyncio.Event()
            feeds_closed = asyncio.Event()
            connected_count = 0
            closed_count = 0

            class WaitingWebSocket:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    await asyncio.Future()

            class Connection:
                async def __aenter__(self):
                    nonlocal connected_count
                    connected_count += 1
                    if connected_count == 2:
                        feeds_connected.set()
                    return WaitingWebSocket()

                async def __aexit__(self, *_args):
                    nonlocal closed_count
                    closed_count += 1
                    if closed_count == 2:
                        feeds_closed.set()
                    return False

            original_run = runtime.run

            async def run_with_isolated_feeds(*, stop_event):
                runtime_task = asyncio.create_task(
                    original_run(
                        connector=lambda *_args, **_kwargs: Connection(),
                        history_loader=lambda *_args, **_kwargs: (),
                        server_time_loader=lambda: now,
                        stop_event=stop_event,
                    )
                )
                await asyncio.wait_for(feeds_connected.wait(), timeout=1)
                signal_handlers[signal.SIGINT]()
                signal_handlers[signal.SIGINT]()
                signal_handlers[signal.SIGTERM]()
                await asyncio.wait_for(runtime_task, timeout=1)

            previous_handlers = {
                sig: object() for sig in (signal.SIGINT, signal.SIGTERM)
            }
            signal_handlers = {}
            restored_signals = []

            def set_signal_handler(sig, handler):
                if handler is previous_handlers[sig]:
                    restored_signals.append(sig)
                else:
                    signal_handlers[sig] = handler

            args = SimpleNamespace(
                journal=journal_path,
                status=status_path,
                candle_interval_seconds=60,
                candle_max_age_seconds=90.0,
                market_stale_after_seconds=3.0,
                max_reconnect_attempts=0,
            )
            with (
                patch.object(
                    runtime_module.signal,
                    "getsignal",
                    side_effect=lambda sig: previous_handlers[sig],
                ),
                patch.object(
                    runtime_module.signal, "signal", side_effect=set_signal_handler
                ),
                patch.object(runtime_module, "PaperRuntime", return_value=runtime),
                patch.object(runtime, "run", new=run_with_isolated_feeds),
            ):
                await runtime_module._run_from_cli(args)

            await asyncio.wait_for(feeds_closed.wait(), timeout=1)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(connected_count, 2)
            self.assertEqual(closed_count, 2)
            self.assertEqual(set(restored_signals), set(previous_handlers))
            self.assertFalse(status["process_running"])
            self.assertFalse(status["entry_permission"])
            self.assertIsNone(status["process_heartbeat"])
            self.assertIsNotNone(broker.position)
            journal_after_shutdown = journal_path.read_bytes()
            journal_events = [
                json.loads(line)
                for line in journal_after_shutdown.decode("utf-8").splitlines()
            ]
            self.assertNotIn(
                "trade_closed", [event["event"] for event in journal_events]
            )
            self.assertEqual(journal_after_shutdown, journal_before_shutdown)

    async def test_runtime_restart_does_not_duplicate_journal_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc)

            class FailedConnection:
                async def __aenter__(self):
                    raise OSError("offline test feed")

                async def __aexit__(self, *_args):
                    return False

            async def run_once():
                runtime = PaperRuntime(
                    RuntimeSettings(
                        journal_path=root / "paper.jsonl",
                        status_path=root / "status.json",
                        max_reconnect_attempts=0,
                        heartbeat_interval_seconds=0.01,
                    ),
                    clock=lambda: now,
                )
                await runtime.run(
                    connector=lambda *_args, **_kwargs: FailedConnection(),
                    history_loader=lambda *_args, **_kwargs: (),
                    server_time_loader=lambda: now,
                )
                self.assertIsNone(runtime.broker.position)
                self.assertIsNone(runtime.broker.exit_pending)

            await run_once()
            journal_path = root / "paper.jsonl"
            first_journal = journal_path.read_bytes()
            first_events = [
                json.loads(line)
                for line in first_journal.decode("utf-8").splitlines()
            ]

            await run_once()

            self.assertEqual(journal_path.read_bytes(), first_journal)
            self.assertEqual(
                [event["event"] for event in first_events],
                ["account_initialized"],
            )

    async def test_mocked_public_feeds_start_independently_reach_readiness_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc)
            minute = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
            open_time_ms = int(minute.timestamp() * 1000)
            close_time_ms = open_time_ms + 59_999
            closed_at = datetime.fromtimestamp(
                close_time_ms / 1000,
                tz=timezone.utc,
            )
            quote_url = BINANCE_BOOK_TICKER_URL
            candle_url = binance_kline_stream_url(60)
            connected_urls = []
            history_requests = []
            stop_event = asyncio.Event()

            class MessageWebSocket:
                def __init__(self, message):
                    self.message = message

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if self.message is not None:
                        message, self.message = self.message, None
                        return message
                    await asyncio.Future()

            class Connection:
                def __init__(self, message):
                    self.websocket = MessageWebSocket(message)

                async def __aenter__(self):
                    return self.websocket

                async def __aexit__(self, *_args):
                    return False

            quote_message = json.dumps({
                "s": "BTCUSDT",
                "b": "100.00",
                "a": "100.02",
            })
            candle_message = json.dumps({
                "s": "BTCUSDT",
                "k": {
                    "s": "BTCUSDT",
                    "i": "1m",
                    "t": open_time_ms,
                    "T": close_time_ms,
                    "o": "100",
                    "h": "100.1",
                    "l": "99.9",
                    "c": "100",
                    "x": True,
                },
            })

            def connector(url, **_kwargs):
                connected_urls.append(url)
                if url == quote_url:
                    return Connection(quote_message)
                if url == candle_url:
                    return Connection(candle_message)
                raise AssertionError(f"Unexpected market-data endpoint: {url}")

            def history_loader(interval_seconds, limit, *, now):
                history_requests.append((interval_seconds, limit))
                return tuple(
                    Bar(
                        closed_at - timedelta(seconds=60 * (20 - index)),
                        100,
                        100.1,
                        99.9,
                        100,
                    )
                    for index in range(21)
                )

            runtime = PaperRuntime(
                RuntimeSettings(
                    journal_path=root / "paper.jsonl",
                    status_path=root / "status.json",
                    heartbeat_interval_seconds=0.01,
                    max_reconnect_attempts=0,
                )
            )
            task = asyncio.create_task(
                runtime.run(
                    connector=connector,
                    history_loader=history_loader,
                    server_time_loader=lambda: datetime.now(timezone.utc),
                    stop_event=stop_event,
                )
            )
            try:
                deadline = asyncio.get_running_loop().time() + 5
                while True:
                    current = datetime.now(timezone.utc)
                    healthy, _reason = runtime._entry_gate(
                        current,
                        runtime._current_health(current),
                    )
                    if (
                        healthy
                        and quote_url in connected_urls
                        and candle_url in connected_urls
                        and history_requests
                    ):
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        self.fail(
                            "Mock quote and candle feeds did not reach readiness: "
                            f"urls={connected_urls!r}, history={history_requests!r}, "
                            f"bars={len(runtime._bars)}, "
                            f"quote={runtime._feed.health(now=current)!r}, "
                            f"candle={runtime._candle_feed.health(now=current)!r}, "
                            f"history_ready={runtime._candle_history_ready}."
                        )
                    await asyncio.sleep(0.01)
            finally:
                stop_event.set()
                await asyncio.wait_for(task, timeout=2)

            status = json.loads((root / "status.json").read_text(encoding="utf-8"))
            journal_events = [
                json.loads(line)
                for line in (root / "paper.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertCountEqual(connected_urls, [quote_url, candle_url])
            self.assertEqual(history_requests, [(60, 22)])
            self.assertEqual(len(runtime._bars), 21)
            self.assertEqual(runtime._candle_feed.interval_label, "1m")
            self.assertFalse(status["process_running"])
            self.assertFalse(status["entry_permission"])
            self.assertEqual(status["candle_interval"], "1m")
            self.assertFalse(any(
                event["event"] in ("entry_fill", "trade_closed")
                for event in journal_events
            ))

    async def test_kline_reconnect_triggers_a_new_historical_reseed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 10, 21, 12, 1, 0, 100000, tzinfo=timezone.utc)
            runtime = PaperRuntime(
                RuntimeSettings(
                    journal_path=root / "paper.jsonl",
                    status_path=root / "status.json",
                    max_reconnect_attempts=1,
                    base_backoff_seconds=0.01,
                    max_backoff_seconds=0.01,
                    heartbeat_interval_seconds=0.01,
                ),
                clock=lambda: now,
            )
            history_calls = []
            kline_connections = 0
            stop_event = asyncio.Event()

            class EmptyWebSocket:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    raise StopAsyncIteration

            class WaitingWebSocket:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    await asyncio.Future()

            class Connection:
                def __init__(self, websocket):
                    self.websocket = websocket

                async def __aenter__(self):
                    return self.websocket

                async def __aexit__(self, *_args):
                    return False

            def kline_connector(_url, **_kwargs):
                nonlocal kline_connections
                kline_connections += 1
                return Connection(
                    EmptyWebSocket() if kline_connections == 1
                    else WaitingWebSocket()
                )

            def quote_connector(_url, **_kwargs):
                return Connection(WaitingWebSocket())

            def load_history(interval_seconds, limit, *, now):
                history_calls.append((interval_seconds, limit))
                last_close = now - timedelta(milliseconds=101)
                first_close = last_close - timedelta(
                    seconds=interval_seconds * 20
                )
                return tuple(
                    Bar(
                        first_close + timedelta(seconds=interval_seconds * index),
                        100,
                        100.1,
                        99.9,
                        100,
                    )
                    for index in range(21)
                )

            task = asyncio.create_task(
                runtime.run(
                    connector=quote_connector,
                    candle_connector=kline_connector,
                    history_loader=load_history,
                    server_time_loader=lambda: now,
                    stop_event=stop_event,
                )
            )
            try:
                deadline = asyncio.get_running_loop().time() + 3
                while not (
                    kline_connections >= 2
                    and len(history_calls) >= 2
                    and runtime._candle_history_ready
                    and len(runtime._bars) == 21
                ):
                    if asyncio.get_running_loop().time() >= deadline:
                        self.fail(
                            "Reconnect did not complete its historical candle reseed."
                        )
                    await asyncio.sleep(0.01)
            finally:
                stop_event.set()
                await asyncio.wait_for(task, timeout=2)

            self.assertEqual(history_calls, [(60, 22), (60, 22)])
            self.assertEqual(len(runtime._bars), 21)
            self.assertGreater(runtime._candle_generation, 1)

    async def test_feed_failure_is_bounded_and_status_reports_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
            runtime = PaperRuntime(
                RuntimeSettings(
                    journal_path=root / "paper.jsonl",
                    status_path=root / "status.json",
                    max_reconnect_attempts=0,
                    heartbeat_interval_seconds=0.01,
                ),
                clock=lambda: now,
            )

            class FailedConnection:
                async def __aenter__(self):
                    raise OSError("offline test")

                async def __aexit__(self, *_args):
                    return False

            await runtime.run(
                connector=lambda *_args, **_kwargs: FailedConnection(),
                history_loader=lambda *_args, **_kwargs: (),
                server_time_loader=lambda: now,
            )
            status = json.loads((root / "status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["process_running"])
            self.assertFalse(status["entry_permission"])
            self.assertEqual(status["connection_status"], "disconnected")
            self.assertIsNone(status["process_heartbeat"])

    async def test_unexpected_feed_task_error_is_reported_after_shutdown_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
            runtime = PaperRuntime(
                RuntimeSettings(
                    journal_path=root / "paper.jsonl",
                    status_path=root / "status.json",
                    heartbeat_interval_seconds=0.01,
                ),
                clock=lambda: now,
            )

            with self.assertRaisesRegex(RuntimeError, "Market-data runtime task failed"):
                await runtime.run(
                    connector=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        TypeError("unexpected connector error")
                    ),
                    history_loader=lambda *_args, **_kwargs: (),
                    server_time_loader=lambda: now,
                )
            status = json.loads((root / "status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["process_running"])
            self.assertFalse(status["entry_permission"])

    async def test_stop_event_cancels_the_feed_and_persists_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
            runtime = PaperRuntime(
                RuntimeSettings(
                    journal_path=root / "paper.jsonl",
                    status_path=root / "status.json",
                    heartbeat_interval_seconds=0.01,
                ),
                clock=lambda: now,
            )
            connected = asyncio.Event()
            stopped = asyncio.Event()
            stop_event = asyncio.Event()

            class WaitingWebSocket:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    await stopped.wait()
                    raise StopAsyncIteration

            class Connection:
                async def __aenter__(self):
                    connected.set()
                    return WaitingWebSocket()

                async def __aexit__(self, *_args):
                    stopped.set()
                    return False

            runtime_task = asyncio.create_task(
                runtime.run(
                    connector=lambda *_args, **_kwargs: Connection(),
                    history_loader=lambda *_args, **_kwargs: (),
                    server_time_loader=lambda: now,
                    stop_event=stop_event,
                )
            )
            await connected.wait()
            stop_event.set()
            await asyncio.wait_for(runtime_task, timeout=1)
            status = json.loads((root / "status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["process_running"])
            self.assertFalse(status["entry_permission"])
            self.assertIsNone(status["process_heartbeat"])


if __name__ == "__main__":
    unittest.main()
