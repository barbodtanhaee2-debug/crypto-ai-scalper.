import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtester import BacktestSettings, Backtester
from historical_replay import HistoricalReplay, PLAYBACK_SPEEDS, resolve_replay_journal
from strategy import Bar, MomentumBreakoutStrategy


class HistoricalReplayTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def bars(self, values):
        return [
            Bar(
                self.start + timedelta(minutes=index),
                open_price,
                high,
                low,
                close,
            )
            for index, (open_price, high, low, close) in enumerate(values)
        ]

    def sample_bars(self):
        prior = [(100, 100.2, 99.8, 100)] * 20
        return self.bars(
            prior
            + [
                (100.0, 100.5, 99.9, 100.4),
                (100.4, 101.5, 100.3, 101.2),
            ]
        )

    def test_playback_speeds_are_fixed(self):
        self.assertEqual(PLAYBACK_SPEEDS, (1, 2, 5, 10, 30, 60))

    def test_replay_journal_cannot_use_live_files(self):
        with self.assertRaisesRegex(ValueError, "live paper runtime"):
            resolve_replay_journal("paper_broker.jsonl")
        with self.assertRaisesRegex(ValueError, "live paper runtime"):
            resolve_replay_journal("runtime_status.json")

    def test_replay_does_not_read_or_write_live_journal(self):
        candles = self.sample_bars()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live = root / "paper_broker.jsonl"
            live.write_text("live-journal\n", encoding="utf-8")
            replay_journal = root / "replay.jsonl"
            session = HistoricalReplay(
                candles,
                strategy_name="momentum_breakout",
                journal_path=replay_journal,
                live_journal=live,
            )
            session.run_all()
            self.assertEqual(live.read_text(encoding="utf-8"), "live-journal\n")
            self.assertTrue(replay_journal.exists())
            self.assertNotEqual(replay_journal.read_text(encoding="utf-8"), "live-journal\n")

    def test_step_uses_only_available_candles(self):
        candles = self.sample_bars()
        seen = []

        test = self

        class Probe(MomentumBreakoutStrategy):
            def evaluate(self, bars, **kwargs):
                seen.append(tuple(bar.timestamp for bar in bars))
                decision_time = kwargs["decision_time"]
                for bar in bars:
                    test.assertLessEqual(bar.timestamp, decision_time)
                return super().evaluate(bars, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            session = HistoricalReplay(
                candles,
                strategy_name="momentum_breakout",
                journal_path=Path(directory) / "replay.jsonl",
            )
            session._strategy = Probe(session._strategy.settings)
            session.step()
            session.step()
            session.step()
        self.assertEqual(len(seen[0]), 1)
        self.assertEqual(len(seen[1]), 2)
        self.assertEqual(len(seen[2]), 3)
        self.assertEqual(seen[2][-1], candles[2].timestamp)
        self.assertNotIn(candles[-1].timestamp, seen[0])

    def test_speed_does_not_change_trade_results(self):
        candles = self.sample_bars()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            slow = HistoricalReplay(
                candles,
                strategy_name="momentum_breakout",
                journal_path=root / "slow.jsonl",
            )
            while slow.step():
                pass
            fast = HistoricalReplay(
                candles,
                strategy_name="momentum_breakout",
                journal_path=root / "fast.jsonl",
            )
            while not fast.finished:
                for _ in range(10):
                    if not fast.step():
                        break
            self.assertEqual(slow.snapshot()["equity"], fast.snapshot()["equity"])
            self.assertEqual(slow.snapshot()["trades"], fast.snapshot()["trades"])
            batch = HistoricalReplay(
                candles,
                strategy_name="momentum_breakout",
                journal_path=root / "all.jsonl",
            )
            batch.run_all()
            self.assertEqual(slow.snapshot()["equity"], batch.snapshot()["equity"])

    def test_restart_resets_account_and_clock(self):
        candles = self.sample_bars()
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "replay.jsonl"
            session = HistoricalReplay(
                candles,
                strategy_name="momentum_breakout",
                journal_path=journal,
            )
            session.step()
            session.step()
            self.assertEqual(session.cursor, 1)
            first_jsonl = journal.read_text(encoding="utf-8")
            session.restart()
            self.assertEqual(session.cursor, -1)
            self.assertIsNone(session.snapshot()["clock"])
            self.assertEqual(session.broker.cash, 100.0)
            self.assertTrue(journal.exists())
            self.assertNotEqual(journal.read_text(encoding="utf-8"), first_jsonl)
            self.assertIn("account_initialized", journal.read_text(encoding="utf-8"))

    def test_replay_matches_backtester_trade_count(self):
        candles = self.sample_bars()
        settings = BacktestSettings()
        expected = Backtester(settings).run(candles, MomentumBreakoutStrategy())
        with tempfile.TemporaryDirectory() as directory:
            session = HistoricalReplay(
                candles,
                strategy_name="momentum_breakout",
                journal_path=Path(directory) / "replay.jsonl",
                settings=settings,
            )
            session.run_all()
        self.assertEqual(
            session.snapshot()["equity"],
            expected["overall"]["final_equity"],
        )
        self.assertEqual(
            len(session.snapshot()["trades"]),
            expected["overall"]["trade_count"],
        )


if __name__ == "__main__":
    unittest.main()
