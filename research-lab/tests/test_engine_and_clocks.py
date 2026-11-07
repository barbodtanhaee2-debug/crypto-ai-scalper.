import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import fixtures as lab_fixtures
from backtester import BacktestSettings
from quote_tape_dataset import load_quote_tape_session
from quote_tape_replay import QuoteTapeReplay
from research_lab.backtesting.clocks import HOST_RECEIVE, KLINE_EVENT, research_decision_time
from research_lab.backtesting.engine import ResearchQuoteReplay
from research_lab.experiments.stale_investigation import analyze_stale_decisions
from research_lab.metrics.evaluate import experiment_metrics
from research_lab.strategies.demo import DemoCloseFollowStrategy
from strategy import StrategyDecision


class RecorderStrategy:
    name = "recorder"
    spec = DemoCloseFollowStrategy().spec

    def __init__(self, settings=None):
        self.settings = DemoCloseFollowStrategy().settings
        self.calls = []

    def evaluate(self, bars, *, decision_time, market_health, equity, position=None):
        self.calls.append(
            {
                "bar_count": len(bars),
                "last_bar": bars[-1].timestamp if bars else None,
                "decision_time": decision_time,
                "ask": market_health.get("ask"),
            }
        )
        return StrategyDecision(
            "no_trade",
            self.name,
            None,
            decision_time,
            "Recorder only.",
            {"bar_count": len(bars)},
            None,
        )


class EngineAndClockTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)

    def _tape(self, *, e_lag_ms=4000, quotes_every=1, minutes=6):
        quotes = []
        for second in range(0, minutes * 60):
            if second % quotes_every == 0:
                quotes.append(lab_fixtures.quote(self.start, second, bid=100, ask=100.02))
        candles = [
            lab_fixtures.candle(
                self.start,
                minute,
                close=100 + minute * 0.5,
                e_lag_ms=e_lag_ms,
            )
            for minute in range(minutes)
        ]
        return load_quote_tape_session(
            lab_fixtures.write_session(self.root, quotes, candles, name="tape")
        )

    def test_host_receive_clock_uses_candle_received_at(self):
        dataset = self._tape(e_lag_ms=15)
        candle = dataset.candles[0]
        self.assertEqual(
            research_decision_time(candle, HOST_RECEIVE), candle.received_at
        )
        self.assertEqual(
            research_decision_time(candle, KLINE_EVENT),
            candle.exchange_event_timestamp,
        )

    def test_mixed_clock_stale_is_classified_without_raising_threshold(self):
        dataset = self._tape(e_lag_ms=4000, quotes_every=1, minutes=4)
        analysis = analyze_stale_decisions(dataset, stale_after_seconds=3.0)
        self.assertGreaterEqual(analysis["stale_on_kline_event_clock"], 1)
        self.assertEqual(analysis["proposed_correction"]["policy"], HOST_RECEIVE)
        self.assertIn("Increase the 3 s production freshness threshold", analysis["proposed_correction"]["do_not"])

    def test_kline_event_lab_engine_matches_stage2_replay_stale_count(self):
        dataset = self._tape(e_lag_ms=4000, minutes=5)
        settings = BacktestSettings(candle_interval_seconds=60)
        frozen = QuoteTapeReplay(
            dataset=dataset,
            journal_path=self.root / "frozen.jsonl",
            strategy_name="momentum_breakout",
            settings=settings,
            stale_after_seconds=3.0,
        ).run()
        lab = ResearchQuoteReplay(
            dataset=dataset,
            journal_path=self.root / "lab.jsonl",
            strategy_name="momentum_breakout",
            settings=settings,
            stale_after_seconds=3.0,
            decision_clock=KLINE_EVENT,
        ).run()
        self.assertEqual(
            lab["stale_candle_decisions"], frozen["stale_candle_decisions"]
        )
        self.assertEqual(lab["trade_count"], frozen["trade_count"])

    def test_look_ahead_prevention_never_passes_future_bars(self):
        dataset = self._tape(e_lag_ms=15, minutes=8)
        recorder = RecorderStrategy()
        ResearchQuoteReplay(
            dataset=dataset,
            journal_path=self.root / "look.jsonl",
            strategy_name="recorder",
            strategy_object=recorder,
            decision_clock=HOST_RECEIVE,
        ).run()
        self.assertGreaterEqual(len(recorder.calls), 1)
        for index, call in enumerate(recorder.calls):
            self.assertEqual(call["bar_count"], index + 1)
            self.assertLessEqual(call["last_bar"], dataset.candles[index].close_time)

    def test_demo_strategy_and_fee_on_quote_fill(self):
        quotes = [
            lab_fixtures.quote(self.start, second, bid=100.0, ask=100.10)
            for second in range(0, 8 * 60)
        ]
        candles = [
            lab_fixtures.candle(self.start, minute, close=100 + minute)
            for minute in range(8)
        ]
        dataset = load_quote_tape_session(
            lab_fixtures.write_session(self.root, quotes, candles, name="demo")
        )
        result = ResearchQuoteReplay(
            dataset=dataset,
            journal_path=self.root / "demo.jsonl",
            strategy_name="demo_close_follow",
            strategy_object=DemoCloseFollowStrategy(),
            decision_clock=HOST_RECEIVE,
            settings=BacktestSettings(candle_interval_seconds=60),
        ).run()
        self.assertGreaterEqual(result["entries_accepted"] + result["entries_rejected"], 1)
        metrics = experiment_metrics(result)
        self.assertTrue(metrics["insufficient_sample"])
        self.assertFalse(metrics["ranking_allowed"])
        if result["trades"]:
            trade = result["trades"][0]
            quantity = float(trade["quantity_btc"])
            entry = float(trade["entry_fill_price"])
            expected_entry_fee = entry * quantity * 0.001
            self.assertAlmostEqual(float(trade["entry_fee"]), expected_entry_fee, places=8)

    def test_reproducibility_two_runs(self):
        dataset = self._tape(e_lag_ms=15, minutes=8)
        settings = BacktestSettings(candle_interval_seconds=60)
        first = ResearchQuoteReplay(
            dataset=dataset,
            journal_path=self.root / "a.jsonl",
            strategy_name="demo_close_follow",
            strategy_object=DemoCloseFollowStrategy(),
            decision_clock=HOST_RECEIVE,
            settings=settings,
        ).run()
        second = ResearchQuoteReplay(
            dataset=dataset,
            journal_path=self.root / "b.jsonl",
            strategy_name="demo_close_follow",
            strategy_object=DemoCloseFollowStrategy(),
            decision_clock=HOST_RECEIVE,
            settings=settings,
        ).run()
        self.assertEqual(first["trades"], second["trades"])
        self.assertEqual(first["stale_candle_decisions"], second["stale_candle_decisions"])
        self.assertEqual(first["equity"], second["equity"])

    def test_missing_quotes_are_not_fabricated(self):
        quotes = [lab_fixtures.quote(self.start, 0, bid=100, ask=100.02)]
        quotes += [
            lab_fixtures.quote(self.start, 200 + index, bid=100, ask=100.02)
            for index in range(10)
        ]
        candles = [lab_fixtures.candle(self.start, minute, close=100) for minute in range(3)]
        dataset = load_quote_tape_session(
            lab_fixtures.write_session(self.root, quotes, candles, name="gap")
        )
        analysis = analyze_stale_decisions(dataset)
        self.assertGreaterEqual(analysis["stale_on_host_receive_clock"], 1)
