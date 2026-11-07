"""Read-only execution-parity audit: live protective bids vs candle exits.

Does not change broker, runtime, or strategy production code.
Does not infer historical bid/ask from OHLC; live quotes are supplied explicitly.
"""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtester import BacktestSettings, Backtester, _health, _position_for_strategy
from historical_replay import HistoricalReplay
from paper_broker import (
    EXIT_FILL_DECISION_PRICE,
    EXIT_FILL_EXECUTABLE_QUOTE,
    PaperBroker,
    PaperBrokerSettings,
)
from runtime import PaperRuntime, RuntimeSettings
from strategy import (
    Bar,
    MomentumBreakoutStrategy,
    RangeMeanReversionStrategy,
    SimulatedPosition,
    StrategyDecision,
    StrategyExperiment,
    StrategySettings,
)


class ExecutionParityAuditTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        self.slip = 0.0005
        self.fee = 0.001

    def tearDown(self):
        self.tempdir.cleanup()

    def health(self, timestamp, *, bid, ask, **updates):
        status = {
            "healthy": True,
            "entry_allowed": True,
            "connection_status": "connected",
            "bid": bid,
            "ask": ask,
            "spread": ask - bid,
            "message_timestamp": timestamp.isoformat(),
            "last_valid_update": timestamp.isoformat(),
            "age_seconds": 0.0,
        }
        status.update(updates)
        return status

    def broker(self, name):
        return PaperBroker(
            PaperBrokerSettings(
                journal_path=self.root / f"{name}.jsonl",
                fee_rate_per_side=self.fee,
                slippage_rate_per_side=self.slip,
            )
        )

    def open_long(self, ledger, timestamp):
        ask = 100.01
        stop = 99.8
        target = 101.0
        quantity = 0.4
        decision = StrategyDecision(
            "entry",
            "momentum_breakout",
            "long",
            timestamp,
            "audit synthetic entry",
            {},
            SimulatedPosition(
                "momentum_breakout",
                "long",
                ask,
                quantity,
                stop,
                target,
                timestamp,
            ),
        )
        result = ledger.process(
            decision,
            market_health=self.health(timestamp, bid=99.99, ask=ask),
        )
        self.assertTrue(result.accepted)
        return result

    def last_trade(self, ledger):
        events = [
            json.loads(line)
            for line in Path(ledger.settings.journal_path).read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        closed = [event for event in events if event["event"] == "trade_closed"]
        self.assertTrue(closed)
        return closed[-1]["details"]

    def test_protective_bid_exits_before_completed_candle_stop(self):
        live = self.broker("live_stop")
        candle = self.broker("candle_stop")
        self.open_long(live, self.now)
        self.open_long(candle, self.now)
        stop = live.position.stop_price
        self.assertEqual(stop, candle.position.stop_price)

        first_cross = self.now + timedelta(seconds=10)
        live_exit = live.process_protective_quote(
            self.health(first_cross, bid=99.75, ask=99.77),
            timestamp=first_cross,
        )
        self.assertTrue(live_exit.accepted)
        self.assertEqual(live_exit.action, "exit")
        self.assertAlmostEqual(live_exit.fill_price, 99.75 * (1 - self.slip))

        candle_close_time = self.now + timedelta(minutes=1)
        completed = Bar(
            candle_close_time,
            100.0,
            100.1,
            99.6,
            99.65,
        )
        synthetic = _health(completed, 0.0002)
        candle.mark_to_market(synthetic, timestamp=candle_close_time)
        decision = MomentumBreakoutStrategy().evaluate(
            [
                Bar(self.now, 100, 100.2, 99.9, 100),
                completed,
            ],
            decision_time=candle_close_time,
            market_health=synthetic,
            equity=candle.equity,
            position=_position_for_strategy(candle),
        )
        self.assertEqual(decision.action, "exit")
        self.assertIn("Stop", decision.reason)
        candle_exit = candle.process(decision, market_health=synthetic)
        self.assertTrue(candle_exit.accepted)

        live_trade = self.last_trade(live)
        candle_trade = self.last_trade(candle)
        timestamp_gap = (
            datetime.fromisoformat(candle_trade["exit_timestamp"])
            - datetime.fromisoformat(live_trade["exit_timestamp"])
        ).total_seconds()
        fill_gap = candle_exit.fill_price - live_exit.fill_price
        pnl_gap = candle_trade["net_realized_pnl"] - live_trade["net_realized_pnl"]

        self.assertEqual(timestamp_gap, 50.0)
        self.assertGreater(abs(fill_gap), 0)
        self.assertNotAlmostEqual(live_trade["net_realized_pnl"], candle_trade["net_realized_pnl"])
        self.assertAlmostEqual(
            live_exit.fill_price,
            99.75 * (1 - self.slip),
        )
        self.assertAlmostEqual(
            candle_exit.fill_price,
            synthetic["bid"] * (1 - self.slip),
        )
        self.assertAlmostEqual(live_exit.fill_price, 99.700125)
        self.assertAlmostEqual(candle_exit.fill_price, 99.59021498)
        self.assertAlmostEqual(fill_gap, -0.10991002)
        self.assertAlmostEqual(pnl_gap, -0.04392004)
        self.assertIn("process_protective_quote", inspect.getsource(PaperRuntime._handle_update))
        self.assertNotIn("process_protective_quote", inspect.getsource(Backtester.run))

    def test_protective_target_bid_vs_candle_close_bid(self):
        live = self.broker("live_target")
        candle = self.broker("candle_target")
        self.open_long(live, self.now)
        self.open_long(candle, self.now)
        target = live.position.target_price

        first_cross = self.now + timedelta(seconds=15)
        live_exit = live.process_protective_quote(
            self.health(first_cross, bid=101.05, ask=101.07),
            timestamp=first_cross,
        )
        self.assertTrue(live_exit.accepted)
        self.assertIn("Target", live_exit.reason)
        self.assertAlmostEqual(live_exit.fill_price, 101.05 * (1 - self.slip))

        candle_close_time = self.now + timedelta(minutes=1)
        completed = Bar(candle_close_time, 100.5, target + 1, 100.2, 100.9)
        synthetic = _health(completed, 0.0002)
        candle.mark_to_market(synthetic, timestamp=candle_close_time)
        decision = MomentumBreakoutStrategy().evaluate(
            [Bar(self.now, 100, 100.2, 99.9, 100), completed],
            decision_time=candle_close_time,
            market_health=synthetic,
            equity=candle.equity,
            position=_position_for_strategy(candle),
        )
        self.assertEqual(decision.action, "exit")
        self.assertEqual(decision.reason, "Target reached.")
        candle_exit = candle.process(decision, market_health=synthetic)
        self.assertTrue(candle_exit.accepted)
        live_trade = self.last_trade(live)
        candle_trade = self.last_trade(candle)
        self.assertEqual(
            (
                datetime.fromisoformat(candle_trade["exit_timestamp"])
                - datetime.fromisoformat(live_trade["exit_timestamp"])
            ).total_seconds(),
            45.0,
        )
        self.assertAlmostEqual(live_exit.fill_price, 100.999475)
        self.assertAlmostEqual(candle_exit.fill_price, 100.83946505)
        self.assertAlmostEqual(
            candle_trade["net_realized_pnl"] - live_trade["net_realized_pnl"],
            -0.06393998,
        )

    def test_same_candle_stop_and_target_first_bid_wins_live_stop_wins_candle(self):
        live = self.broker("live_both")
        candle = self.broker("candle_both")
        self.open_long(live, self.now)
        self.open_long(candle, self.now)
        target = live.position.target_price
        stop = live.position.stop_price

        target_hit = self.now + timedelta(seconds=8)
        live.process_protective_quote(
            self.health(target_hit, bid=target + 0.02, ask=target + 0.04),
            timestamp=target_hit,
        )
        self.assertIsNone(live.position)
        live_trade = self.last_trade(live)
        self.assertEqual(live_trade["trigger"], "take_profit")

        later_stop_bid = self.now + timedelta(seconds=20)
        self.assertIsNone(
            live.process_protective_quote(
                self.health(later_stop_bid, bid=stop - 0.05, ask=stop - 0.03),
                timestamp=later_stop_bid,
            )
        )

        candle_close_time = self.now + timedelta(minutes=1)
        completed = Bar(
            candle_close_time,
            100.0,
            target + 1,
            stop - 1,
            100.0,
        )
        synthetic = _health(completed, 0.0002)
        candle.mark_to_market(synthetic, timestamp=candle_close_time)
        decision = MomentumBreakoutStrategy().evaluate(
            [Bar(self.now, 100, 100.2, 99.9, 100), completed],
            decision_time=candle_close_time,
            market_health=synthetic,
            equity=candle.equity,
            position=_position_for_strategy(candle),
        )
        self.assertIn("Stop", decision.reason)
        candle.process(decision, market_health=synthetic)
        candle_trade = self.last_trade(candle)
        self.assertEqual(candle_trade["trigger"], "stop_loss")
        self.assertNotEqual(live_trade["trigger"], candle_trade["trigger"])
        self.assertAlmostEqual(live_trade["exit_fill_price"], 100.96949)
        self.assertAlmostEqual(candle_trade["exit_fill_price"], 99.940005)

    def test_stale_executable_quote_does_not_arm_protective_pending(self):
        live = self.broker("stale_protective")
        self.open_long(live, self.now)
        stale_time = self.now + timedelta(seconds=10)
        stale = self.health(
            stale_time - timedelta(seconds=5),
            bid=99.7,
            ask=99.72,
            age_seconds=5.0,
        )
        result = live.process_protective_quote(stale, timestamp=stale_time)
        self.assertIsNone(result)
        self.assertIsNotNone(live.position)
        self.assertIsNone(live.exit_pending)

    def test_pending_exit_blocks_entries_keeps_last_mark_and_resolves_once(self):
        runtime = PaperRuntime(
            RuntimeSettings(
                journal_path=self.root / "runtime.jsonl",
                status_path=self.root / "status.json",
            ),
            broker=self.broker("pending_runtime"),
        )
        runtime._running = True
        self.open_long(runtime.broker, self.now)
        last_bid = runtime.broker.last_bid
        equity_before = runtime.broker.equity
        trigger = self.now + timedelta(minutes=1)
        pending = runtime.broker.process(
            StrategyDecision(
                "exit",
                "momentum_breakout",
                "long",
                trigger,
                "Stop reached.",
                {"exit_price": 99.8},
            ),
            market_health=self.health(
                trigger,
                bid=99.7,
                ask=99.72,
                healthy=False,
                entry_allowed=False,
                connection_status="disconnected",
            ),
        )
        self.assertEqual(pending.action, "exit_pending")
        self.assertIsNotNone(runtime.broker.position)
        self.assertAlmostEqual(runtime.broker.equity, equity_before)
        self.assertEqual(runtime.broker.last_bid, last_bid)
        allowed, reason = runtime._entry_gate(
            trigger,
            self.health(
                trigger,
                bid=99.7,
                ask=99.72,
                healthy=False,
                entry_allowed=False,
                connection_status="disconnected",
            ),
        )
        self.assertFalse(allowed)
        self.assertIn("pending fresh-quote verification", reason)
        self.assertFalse(runtime.broker.exit_pending["loss_bound_guaranteed"])
        self.assertTrue(runtime.broker.exit_pending["unresolved_simulated_exposure"])

        still_stale = trigger + timedelta(seconds=30)
        status = runtime._status_record(
            now=still_stale,
            health=self.health(
                still_stale,
                bid=99.7,
                ask=99.72,
                healthy=False,
                entry_allowed=False,
                connection_status="disconnected",
            ),
            reason=reason,
        )
        self.assertEqual(status["exit_pending"]["age_seconds"], 30.0)
        self.assertFalse(status["exit_pending"]["fill_fabricated_from_stale_quote"])
        self.assertTrue(status["entry_permission"] is False)

        recover = still_stale + timedelta(seconds=5)
        closed = runtime.broker.process_protective_quote(
            self.health(recover, bid=99.71, ask=99.73),
            timestamp=recover,
        )
        self.assertTrue(closed.accepted)
        self.assertIsNone(runtime.broker.position)
        self.assertIsNone(runtime.broker.exit_pending)
        events = [
            json.loads(line)
            for line in (self.root / "pending_runtime.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(sum(event["event"] == "trade_closed" for event in events), 1)
        self.assertEqual(sum(event["event"] == "exit_pending" for event in events), 1)
        self.assertAlmostEqual(closed.fill_price, 99.71 * (1 - self.slip))

    def test_replay_does_not_propagate_exit_fill_mode(self):
        settings = BacktestSettings(exit_fill_mode=EXIT_FILL_DECISION_PRICE)
        candles = [
            Bar(
                self.now + timedelta(minutes=index),
                100, 100.2, 99.8, 100,
            )
            for index in range(22)
        ]
        session = HistoricalReplay(
            candles,
            strategy_name="momentum_breakout",
            journal_path=self.root / "replay_mode.jsonl",
            settings=settings,
        )
        self.assertEqual(settings.exit_fill_mode, EXIT_FILL_DECISION_PRICE)
        self.assertEqual(
            session.broker.settings.exit_fill_mode,
            EXIT_FILL_EXECUTABLE_QUOTE,
        )

    def test_open_position_at_last_candle_is_force_closed_only_by_backtester(self):
        prior = [
            Bar(self.now + timedelta(minutes=index), 100, 100.2, 99.8, 100)
            for index in range(20)
        ]
        entry_bar = Bar(
            self.now + timedelta(minutes=20),
            100.0,
            100.5,
            99.9,
            100.4,
        )
        candles = prior + [entry_bar]
        settings = BacktestSettings()
        backtest = Backtester(settings).run(candles, MomentumBreakoutStrategy())
        session = HistoricalReplay(
            candles,
            strategy_name="momentum_breakout",
            journal_path=self.root / "replay_open_end.jsonl",
            settings=settings,
        )
        session.run_all()
        self.assertTrue(backtest["overall"]["period_boundary_force_liquidation"])
        snapshot = session.snapshot()
        self.assertIsNotNone(snapshot["open_position"])
        self.assertNotEqual(snapshot["equity"], backtest["overall"]["final_equity"])
        self.assertEqual(len(snapshot["trades"]), 0)
        self.assertGreater(backtest["overall"]["trade_count"], 0)
        self.assertAlmostEqual(snapshot["equity"], 99.83649231)
        self.assertAlmostEqual(backtest["overall"]["final_equity"], 99.69233845)

    def test_replay_matches_backtester_when_all_trades_close_under_default_mode(self):
        prior = [
            Bar(self.now + timedelta(minutes=index), 100, 100.2, 99.8, 100)
            for index in range(20)
        ]
        candles = prior + [
            Bar(self.now + timedelta(minutes=20), 100.0, 100.5, 99.9, 100.4),
            Bar(self.now + timedelta(minutes=21), 100.4, 101.5, 100.3, 101.2),
        ]
        settings = BacktestSettings()
        expected = Backtester(settings).run(candles, MomentumBreakoutStrategy())
        session = HistoricalReplay(
            candles,
            strategy_name="momentum_breakout",
            journal_path=self.root / "replay_closed.jsonl",
            settings=settings,
        )
        session.run_all()
        self.assertEqual(session.snapshot()["equity"], expected["overall"]["final_equity"])
        self.assertEqual(len(session.snapshot()["trades"]), expected["overall"]["trade_count"])
        self.assertFalse(expected["overall"]["period_boundary_force_liquidation"])

    def test_rejected_momentum_short_occupies_experiment_slot(self):
        costs = StrategySettings(
            fee_rate_per_side=0,
            slippage_rate_per_side=0,
            minimum_net_edge_fraction=0,
        )
        experiment = StrategyExperiment(
            MomentumBreakoutStrategy(costs),
            RangeMeanReversionStrategy(costs),
        )
        start = self.now
        both_signals = 0
        blocked_mean_reversion = 0
        independent_longs = 0
        for index, close in enumerate(
            [round(99.40 + step * 0.02, 2) for step in range(61)]
        ):
            bars = [
                Bar(start + timedelta(minutes=bar), 100, 100.2, 99.8, 100)
                for bar in range(20)
            ]
            bars.append(
                Bar(
                    start + timedelta(minutes=20 + index),
                    close,
                    max(close, 100.2),
                    min(close, 99.8),
                    close,
                )
            )
            quote = {
                "healthy": True,
                "entry_allowed": True,
                "bid": close - 0.01,
                "ask": close + 0.01,
                "spread": 0.02,
            }
            momentum = MomentumBreakoutStrategy(costs).evaluate(
                bars, decision_time=bars[-1].timestamp, market_health=quote, equity=100
            )
            mean_rev = RangeMeanReversionStrategy(costs).evaluate(
                bars, decision_time=bars[-1].timestamp, market_health=quote, equity=100
            )
            if (
                momentum.action == "entry"
                and momentum.direction == "short"
                and mean_rev.action == "entry"
                and mean_rev.direction == "long"
            ):
                both_signals += 1
                joint = experiment.evaluate(
                    bars,
                    decision_time=bars[-1].timestamp,
                    market_health=quote,
                    equity=100,
                )
                if joint["momentum_breakout"].action == "entry":
                    if joint["range_mean_reversion"].action != "entry":
                        blocked_mean_reversion += 1
            if mean_rev.action == "entry" and mean_rev.direction == "long":
                independent_longs += 1

        self.assertEqual(both_signals, 18)
        self.assertEqual(blocked_mean_reversion, 18)
        self.assertEqual(independent_longs, 25)

    def test_holding_time_is_wall_minutes_and_runtime_rejects_one_hour_klines(self):
        strategy = MomentumBreakoutStrategy()
        position = SimulatedPosition(
            "momentum_breakout",
            "long",
            100.01,
            0.4,
            99.8,
            101.0,
            self.now,
        )
        quote = {
            "healthy": True,
            "entry_allowed": True,
            "bid": 100.0,
            "ask": 100.02,
            "spread": 0.02,
        }
        one_minute_later = [
            Bar(self.now, 100, 100.1, 99.9, 100),
            Bar(self.now + timedelta(minutes=1), 100, 100.1, 99.9, 100),
        ]
        one_hour_later = [
            Bar(self.now, 100, 100.1, 99.9, 100),
            Bar(self.now + timedelta(hours=1), 100, 100.1, 99.9, 100),
        ]
        minute_decision = strategy.evaluate(
            one_minute_later,
            decision_time=one_minute_later[-1].timestamp,
            market_health=quote,
            equity=100,
            position=position,
        )
        hour_decision = strategy.evaluate(
            one_hour_later,
            decision_time=one_hour_later[-1].timestamp,
            market_health=quote,
            equity=100,
            position=position,
        )
        self.assertEqual(minute_decision.action, "no_trade")
        self.assertEqual(hour_decision.action, "exit")
        self.assertIn("Maximum holding time", hour_decision.reason)
        with self.assertRaisesRegex(ValueError, "Unsupported Binance kline interval"):
            RuntimeSettings(candle_interval_seconds=3600)
        scaled = MomentumBreakoutStrategy(
            StrategySettings(max_holding_bars=10 * (3600 // 60))
        )
        scaled_hour = scaled.evaluate(
            one_hour_later,
            decision_time=one_hour_later[-1].timestamp,
            market_health=quote,
            equity=100,
            position=position,
        )
        self.assertEqual(scaled_hour.action, "no_trade")


if __name__ == "__main__":
    unittest.main()
