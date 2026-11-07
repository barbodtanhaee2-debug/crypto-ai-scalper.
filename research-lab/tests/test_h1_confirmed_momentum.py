import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fixtures as lab_fixtures
from backtester import BacktestSettings
from quote_tape_dataset import load_quote_tape_session
from research_lab.backtesting.engine import ResearchQuoteReplay
from research_lab.backtesting.holdout import (
    assert_no_holdout_metrics,
    dataset_excluding_holdout,
    scored_trades,
)
from research_lab.backtesting.splits import chronological_split
from research_lab.experiments.h1_comparison import economic_classification
from research_lab.isolation import assert_isolated_path
from research_lab.strategies.confirmed_momentum import (
    ConfirmedMomentumBreakoutStrategy,
    DecisionLoggingAdapter,
    quote_available_at_decision,
)
from strategy import MomentumBreakoutStrategy, StrategySettings


def _host_candle(start, minute, *, close, high=None, low=None):
    # Host receive after close so production look-ahead check permits evaluate.
    return lab_fixtures.candle(
        start, minute, close=close, high=high, low=low, receive_lead_ms=-20
    )


def _health(ask=100.02, bid=100.0, *, decision_time):
    spread = ask - bid
    return {
        "healthy": True,
        "entry_allowed": True,
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "host_received_at_utc": decision_time.isoformat(),
        "last_valid_update": decision_time.isoformat(),
        "age_seconds": 0.0,
    }


class H1ConfirmedMomentumTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        self.settings = StrategySettings()

    def _bars_and_quotes(
        self,
        *,
        minutes=30,
        breakout_minute=22,
        next_close=101.0,
        skip_confirm_minute=False,
        fail_confirm=False,
    ):
        quotes = [
            lab_fixtures.quote(self.start, second, bid=100.0, ask=100.10)
            for second in range(0, (minutes + 2) * 60)
        ]
        candles = []
        for minute in range(minutes):
            if skip_confirm_minute and minute == breakout_minute + 1:
                continue
            if minute < breakout_minute:
                close = 100.0
                high = 100.2
                low = 99.8
            elif minute == breakout_minute:
                close = 101.0
                high = 101.2
                low = 100.0
            elif fail_confirm:
                close = 100.0
                high = 100.2
                low = 99.8
            else:
                close = next_close
                high = close + 0.2
                low = close - 0.2
            candles.append(
                _host_candle(self.start, minute, close=close, high=high, low=low)
            )
        return quotes, candles

    def _dataset(self, quotes, candles, name="h1"):
        return load_quote_tape_session(
            lab_fixtures.write_session(self.root, quotes, candles, name=name)
        )

    def test_inner_long_signal_matches_production_frozen_settings(self):
        quotes, candles = self._bars_and_quotes()
        dataset = self._dataset(quotes, candles, name="sig")
        bars = [candle.as_bar() for candle in dataset.candles[:23]]
        decision_time = dataset.candles[22].received_at
        health = _health(decision_time=decision_time)
        production = MomentumBreakoutStrategy(self.settings)
        logged = DecisionLoggingAdapter(self.settings)
        prod = production.evaluate(
            bars, decision_time=decision_time, market_health=health, equity=100.0
        )
        base = logged.evaluate(
            bars, decision_time=decision_time, market_health=health, equity=100.0
        )
        self.assertEqual(prod.action, "entry")
        self.assertEqual(prod.direction, "long")
        self.assertEqual(base.action, prod.action)
        self.assertEqual(base.inputs.get("prior_high"), prod.inputs.get("prior_high"))
        confirmed = ConfirmedMomentumBreakoutStrategy(self.settings)
        held = confirmed.evaluate(
            bars, decision_time=decision_time, market_health=health, equity=100.0
        )
        self.assertEqual(held.action, "no_trade")
        self.assertIn("pending", held.reason.lower())
        self.assertEqual(held.inputs.get("prior_high"), prod.inputs.get("prior_high"))

    def test_enters_only_after_next_bar_holds_breakout(self):
        quotes, candles = self._bars_and_quotes(next_close=101.0)
        dataset = self._dataset(quotes, candles, name="pass")
        result = ResearchQuoteReplay(
            dataset=dataset,
            journal_path=self.root / "pass.jsonl",
            strategy_name="h1_confirmed_momentum_breakout",
            strategy_object=ConfirmedMomentumBreakoutStrategy(),
            settings=BacktestSettings(candle_interval_seconds=60),
        ).run()
        self.assertGreaterEqual(result["entries_accepted"], 1)
        first_entry = next(
            event
            for event in result["events"]
            if event.get("strategy_action") == "entry" and event.get("accepted")
        )
        self.assertIn("H1 next-bar close confirmation passed", first_entry.get("strategy_reason", ""))

    def test_no_enter_when_next_close_fails_and_pending_cleared(self):
        quotes, candles = self._bars_and_quotes(fail_confirm=True)
        dataset = self._dataset(quotes, candles, name="fail")
        strategy = ConfirmedMomentumBreakoutStrategy()
        ResearchQuoteReplay(
            dataset=dataset,
            journal_path=self.root / "fail.jsonl",
            strategy_name="h1_confirmed_momentum_breakout",
            strategy_object=strategy,
            settings=BacktestSettings(candle_interval_seconds=60),
        ).run()
        events = [row["event"] for row in strategy.decision_log]
        self.assertIn("pending_breakout", events)
        self.assertIn("confirmation_fail", events)
        self.assertNotIn("confirmation_pass", events)

    def test_missing_next_minute_cancels_without_fill(self):
        quotes, candles = self._bars_and_quotes(skip_confirm_minute=True)
        dataset = self._dataset(quotes, candles, name="gap")
        strategy = ConfirmedMomentumBreakoutStrategy()
        result = ResearchQuoteReplay(
            dataset=dataset,
            journal_path=self.root / "gap.jsonl",
            strategy_name="h1_confirmed_momentum_breakout",
            strategy_object=strategy,
            settings=BacktestSettings(candle_interval_seconds=60),
        ).run()
        self.assertIn(
            "pending_cancelled_missing_minute",
            [row["event"] for row in strategy.decision_log],
        )
        self.assertEqual(result["entries_accepted"], 0)

    def test_no_future_bars_and_quote_not_after_decision(self):
        quotes, candles = self._bars_and_quotes()
        dataset = self._dataset(quotes, candles, name="time")
        strategy = ConfirmedMomentumBreakoutStrategy()
        ResearchQuoteReplay(
            dataset=dataset,
            journal_path=self.root / "time.jsonl",
            strategy_name="h1_confirmed_momentum_breakout",
            strategy_object=strategy,
            settings=BacktestSettings(candle_interval_seconds=60),
        ).run()
        for row in strategy.decision_log:
            self.assertTrue(row.get("bar_available_at_decision"))
            self.assertTrue(row.get("quote_available_at_decision"))
        later = (self.start + timedelta(hours=1)).isoformat()
        self.assertFalse(
            quote_available_at_decision(
                {"host_received_at_utc": later},
                self.start,
            )
        )

    def test_holdout_excluded_and_injected_holdout_trade_refused(self):
        quotes = [
            lab_fixtures.quote(self.start, second, bid=100, ask=100.02)
            for second in range(0, 20 * 60)
        ]
        candles = [
            _host_candle(self.start, minute, close=100.0) for minute in range(12)
        ]
        dataset = self._dataset(quotes, candles, name="hold")
        split = chronological_split(dataset)
        masked = dataset_excluding_holdout(dataset, split)
        self.assertTrue(all(candle.close_time < split.test_start for candle in masked.candles))
        self.assertEqual(len(masked.candles), split.train_candles + split.validation_candles)
        leaked = {
            "entry_timestamp": split.test_start.isoformat(),
            "exit_timestamp": split.test_end.isoformat(),
            "net_realized_pnl": 99.0,
        }
        with self.assertRaises(ValueError):
            assert_no_holdout_metrics([leaked], split)
        self.assertEqual(scored_trades([leaked], split), [])

    def test_identical_execution_assumptions_in_config(self):
        import json

        config = json.loads(
            Path(
                r"F:\crypto-ai-scalper\research-lab\configs\h1_next_bar_close.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(config["decision_clock"], "host_receive")
        self.assertEqual(config["fee_rate_per_side"], 0.001)
        self.assertEqual(config["slippage_rate_per_side"], 0.0005)
        self.assertEqual(config["stale_after_seconds"], 3.0)
        self.assertEqual(config["initial_equity"], 100.0)

    def test_journal_isolation_and_short_still_long_only(self):
        with self.assertRaises(ValueError):
            assert_isolated_path("paper_broker.jsonl")
        quotes, candles = self._bars_and_quotes()
        # Deep down-bar for a short signal after warmup.
        candles[22] = _host_candle(self.start, 22, close=98.0, high=98.1, low=97.5)
        candles[23] = _host_candle(self.start, 23, close=97.5, high=97.6, low=97.0)
        dataset = self._dataset(quotes, candles, name="short")
        result = ResearchQuoteReplay(
            dataset=dataset,
            journal_path=self.root / "short.jsonl",
            strategy_name="momentum_breakout_logged",
            strategy_object=DecisionLoggingAdapter(),
            settings=BacktestSettings(candle_interval_seconds=60),
        ).run()
        self.assertGreaterEqual(result["entries_rejected"], 1)

    def test_two_run_reproducibility_on_fixture(self):
        quotes, candles = self._bars_and_quotes()
        dataset = self._dataset(quotes, candles, name="rep")
        settings = BacktestSettings(candle_interval_seconds=60)
        first = ResearchQuoteReplay(
            dataset=dataset,
            journal_path=self.root / "r1.jsonl",
            strategy_name="h1_confirmed_momentum_breakout",
            strategy_object=ConfirmedMomentumBreakoutStrategy(),
            settings=settings,
        ).run()
        second = ResearchQuoteReplay(
            dataset=dataset,
            journal_path=self.root / "r2.jsonl",
            strategy_name="h1_confirmed_momentum_breakout",
            strategy_object=ConfirmedMomentumBreakoutStrategy(),
            settings=settings,
        ).run()
        self.assertEqual(first["trades"], second["trades"])
        self.assertEqual(first["stale_candle_decisions"], second["stale_candle_decisions"])

    def test_economic_gate_inconclusive_under_30(self):
        result = economic_classification(3, 1)
        self.assertEqual(result["economic_result"], "inconclusive")
