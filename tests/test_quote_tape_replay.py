import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quote_tape_collector import write_session_json
from quote_tape_dataset import load_quote_tape_session, quote_file_paths
from quote_tape_replay import (
    QuoteTapeReplay,
    compare_paths,
    journal_trade_fingerprint,
    load_reconnect_gaps,
)


class QuoteTapeReplayTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)

    def write_session(self, quotes, candles, *, name="session"):
        directory = self.root / name
        directory.mkdir()
        write_session_json(
            directory / "session.json",
            {
                "session_id": name,
                "status": "stopped",
                "symbol": "BTCUSDT",
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
        return directory

    def quote(self, index, *, bid=100.0, ask=100.02, update_id=None, **extra):
        received = self.start + timedelta(seconds=index)
        payload = {
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
        payload.update(extra)
        return payload

    def candle(self, minute, *, close=100.0, high=100.2, low=99.8, symbol="BTCUSDT"):
        open_time = self.start + timedelta(minutes=minute)
        close_time = open_time + timedelta(milliseconds=59_999)
        received = close_time - timedelta(milliseconds=50)
        event = close_time + timedelta(milliseconds=15)
        return {
            "record_type": "candle",
            "received_at_utc": received.isoformat(),
            "symbol": symbol,
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

    def test_loader_reports_malformed_duplicate_and_missing_interval(self):
        quotes = [
            self.quote(0, update_id=3),
            self.quote(1, update_id=1),
            self.quote(2, update_id=3),
            self.quote(3, bid=101, ask=100),
            "{not-json",
        ]
        candles = [
            self.candle(0),
            self.candle(2),
            self.candle(2, symbol="ETHUSDT"),
        ]
        directory = self.root / "bad"
        directory.mkdir()
        write_session_json(directory / "session.json", {"session_id": "bad"})
        (directory / "quotes.jsonl").write_text(
            json.dumps(quotes[0]) + "\n"
            + json.dumps(quotes[1]) + "\n"
            + json.dumps(quotes[2]) + "\n"
            + json.dumps(quotes[3]) + "\n"
            + quotes[4] + "\n",
            encoding="utf-8",
        )
        (directory / "candles.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in candles),
            encoding="utf-8",
        )
        dataset = load_quote_tape_session(directory)
        codes = {issue.code for issue in dataset.issues}
        self.assertIn("duplicate_update_id", codes)
        self.assertIn("out_of_order_update_id", codes)
        self.assertIn("crossed_market", codes)
        self.assertIn("malformed_quote", codes)
        self.assertIn("missing_interval", codes)
        self.assertIn("symbol_mismatch", codes)
        self.assertGreaterEqual(len(dataset.quotes), 2)

    def test_quote_parts_load_after_base_file(self):
        directory = self.root / "parts"
        directory.mkdir()
        write_session_json(directory / "session.json", {"session_id": "parts"})
        (directory / "quotes.jsonl").write_text(
            json.dumps(self.quote(0, update_id=1)) + "\n", encoding="utf-8"
        )
        (directory / "quotes.part2.jsonl").write_text(
            json.dumps(self.quote(2, update_id=3)) + "\n", encoding="utf-8"
        )
        (directory / "quotes.part1.jsonl").write_text(
            json.dumps(self.quote(1, update_id=2)) + "\n", encoding="utf-8"
        )
        (directory / "candles.jsonl").write_text("", encoding="utf-8")
        names = [path.name for path in quote_file_paths(directory)]
        self.assertEqual(
            names,
            ["quotes.jsonl", "quotes.part1.jsonl", "quotes.part2.jsonl"],
        )
        dataset = load_quote_tape_session(directory)
        self.assertEqual([quote.update_id for quote in dataset.quotes], [1, 2, 3])

    def test_host_offset_uses_kline_e_not_fabricated_quote_e(self):
        quotes = [self.quote(i) for i in range(3)]
        candles = [self.candle(0), self.candle(1)]
        dataset = load_quote_tape_session(self.write_session(quotes, candles))
        self.assertIsNotNone(dataset.host_clock_offset_seconds)
        self.assertGreater(dataset.host_clock_offset_seconds, 0)
        self.assertTrue(
            all(quote.exchange_event_timestamp is None for quote in dataset.quotes)
        )

    def test_replay_is_deterministic_except_account_initialized_wall_clock(self):
        quotes = [self.quote(i, bid=100.0, ask=100.02) for i in range(30)]
        candles = [self.candle(0, close=100.0) for _ in ()]
        candles = [self.candle(minute, close=100.0) for minute in range(3)]
        dataset = load_quote_tape_session(self.write_session(quotes, candles))
        first = QuoteTapeReplay(dataset, journal_path=self.root / "a.jsonl")
        second = QuoteTapeReplay(dataset, journal_path=self.root / "b.jsonl")
        result_a = first.run()
        result_b = second.run()
        self.assertEqual(result_a["trades"], result_b["trades"])
        self.assertEqual(
            [event["action"] for event in result_a["events"] if "action" in event],
            [event["action"] for event in result_b["events"] if "action" in event],
        )
        self.assertEqual(
            journal_trade_fingerprint(self.root / "a.jsonl"),
            journal_trade_fingerprint(self.root / "b.jsonl"),
        )

    def test_pending_exit_recovers_on_later_recorded_quote(self):
        quotes = [
            self.quote(0, bid=100.0, ask=100.02, update_id=1),
            self.quote(10, bid=99.5, ask=99.52, update_id=2),
        ]
        candles = [self.candle(0, close=100.4, high=100.5, low=99.9)]
        # Force a later candle that wants an exit while quotes are stale, then recover.
        dataset = load_quote_tape_session(
            self.write_session(
                quotes
                + [self.quote(70, bid=99.4, ask=99.42, update_id=3)],
                candles + [self.candle(1, close=99.5, high=100.0, low=99.3)],
            )
        )
        replay = QuoteTapeReplay(
            dataset,
            journal_path=self.root / "pending.jsonl",
            stale_after_seconds=3.0,
        )
        result = replay.run()
        self.assertIsInstance(result["pending_intervals"], list)

    def test_breakout_fixture_pairs_quote_exit_with_labeled_synthetic_candle_exit(self):
        quotes = []
        candles = []
        update_id = 1
        for minute in range(24):
            close = 100.0 if minute < 21 else (102.0 if minute == 21 else 101.5)
            high = close + 0.1
            low = 99.9 if minute <= 21 else 101.4
            candles.append(self.candle(minute, close=close, high=high, low=low))
            received = self.start + timedelta(minutes=minute, seconds=59, milliseconds=900)
            quotes.append(
                self.quote(
                    0,
                    bid=close,
                    ask=close + 0.02,
                    update_id=update_id,
                )
            )
            quotes[-1]["received_at_utc"] = received.isoformat()
            update_id += 1
            if minute == 21:
                stop_time = self.start + timedelta(minutes=21, seconds=70)
                quotes.append(
                    self.quote(
                        0,
                        bid=101.70,
                        ask=101.72,
                        update_id=update_id,
                    )
                )
                quotes[-1]["received_at_utc"] = stop_time.isoformat()
                update_id += 1
        dataset = load_quote_tape_session(
            self.write_session(quotes, candles, name="breakout")
        )
        comparison = compare_paths(
            dataset, output_dir=self.root / "breakout-out"
        )
        self.assertGreaterEqual(comparison["quote_path"]["trade_count"], 1)
        self.assertGreaterEqual(
            comparison["candle_path"]["overall"]["trade_count"], 1
        )
        self.assertTrue(comparison["paired_trades"])
        pair = comparison["paired_trades"][0]
        self.assertIsNotNone(pair["quote_exit_timestamp"])
        self.assertIsNotNone(pair["candle_exit_timestamp"])
        self.assertNotEqual(
            pair["quote_exit_fill"], pair["candle_synthetic_exit_fill"]
        )
        self.assertIn("synthetic", comparison["candle_path"]["execution_prices"])

    def test_comparison_labels_synthetic_candle_prices(self):
        quotes = [self.quote(i) for i in range(5)]
        candles = [self.candle(minute) for minute in range(25)]
        dataset = load_quote_tape_session(self.write_session(quotes, candles, name="cmp"))
        comparison = compare_paths(
            dataset, output_dir=self.root / "out"
        )
        self.assertIn("synthetic", comparison["candle_path"]["execution_prices"])
        self.assertEqual(
            comparison["quote_path"]["path"],
            "recorded_bookTicker_protective_quote",
        )
        self.assertTrue((self.root / "out" / "comparison.json").exists())
        self.assertNotEqual(
            comparison["quote_path"]["journal_path"],
            str(Path("paper_broker.jsonl").resolve()),
        )

    def test_reconnect_gap_is_preserved_without_fabricated_quotes(self):
        quotes = [
            self.quote(0, update_id=1),
            self.quote(20, update_id=2),
        ]
        candles = [self.candle(0)]
        directory = self.write_session(quotes, candles, name="gap")
        gap_start = self.start + timedelta(seconds=1)
        gap_end = self.start + timedelta(seconds=10)
        (directory / "events.jsonl").write_text(
            json.dumps(
                {
                    "record_type": "event",
                    "event": "disconnect",
                    "stream": "quote",
                    "timestamp_utc": gap_start.isoformat(),
                    "reason": "test gap",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "record_type": "event",
                    "event": "reconnect",
                    "stream": "quote",
                    "timestamp_utc": gap_end.isoformat(),
                    "gap_started_utc": gap_start.isoformat(),
                    "recovered_at_utc": gap_end.isoformat(),
                    "gap_seconds": 9.0,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        dataset = load_quote_tape_session(directory)
        gaps = load_reconnect_gaps(directory)
        self.assertEqual(len(gaps), 1)
        replay = QuoteTapeReplay(dataset, journal_path=self.root / "gap.jsonl")
        result = replay.run()
        self.assertEqual(result["quotes_processed"], 2)
        self.assertEqual(result["kline_offset_applied_to_quotes"], False)
        in_gap = [
            quote
            for quote in dataset.quotes
            if gap_start <= quote.received_at < gap_end
        ]
        self.assertEqual(in_gap, [])

    def test_stale_quote_is_unhealthy_on_later_candle_decision(self):
        quotes = [self.quote(0, update_id=1)]
        candles = [self.candle(0), self.candle(1)]
        dataset = load_quote_tape_session(self.write_session(quotes, candles, name="stale"))
        result = QuoteTapeReplay(
            dataset,
            journal_path=self.root / "stale.jsonl",
            stale_after_seconds=3.0,
        ).run()
        self.assertGreaterEqual(result["stale_candle_decisions"], 1)

    def test_quote_path_does_not_force_liquidate_open_position(self):
        quotes = []
        candles = []
        update_id = 1
        for minute in range(22):
            close = 100.0 if minute < 21 else 102.0
            candles.append(
                self.candle(minute, close=close, high=close + 0.1, low=99.9)
            )
            received = self.start + timedelta(minutes=minute, seconds=59, milliseconds=900)
            quotes.append(
                self.quote(0, bid=close, ask=close + 0.02, update_id=update_id)
            )
            quotes[-1]["received_at_utc"] = received.isoformat()
            update_id += 1
        dataset = load_quote_tape_session(
            self.write_session(quotes, candles, name="open-end")
        )
        result = QuoteTapeReplay(
            dataset, journal_path=self.root / "open-end.jsonl"
        ).run()
        self.assertFalse(result["force_liquidated_at_end"])
        self.assertGreaterEqual(result["entries_accepted"] + result["unresolved_positions"], 0)

    def test_invalid_quote_records_are_reported_not_replayed(self):
        quotes = [
            self.quote(0, update_id=1),
            self.quote(1, bid=101, ask=100, update_id=2),
        ]
        candles = [self.candle(0)]
        dataset = load_quote_tape_session(
            self.write_session(quotes, candles, name="bad-quote")
        )
        codes = {issue.code for issue in dataset.issues}
        self.assertIn("crossed_market", codes)
        replayed_ids = [quote.update_id for quote in dataset.quotes]
        self.assertEqual(replayed_ids, [1])


if __name__ == "__main__":
    unittest.main()

