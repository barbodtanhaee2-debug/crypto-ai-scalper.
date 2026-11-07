import unittest
from datetime import datetime, timedelta, timezone

from strategy import (
    Bar,
    MomentumBreakoutStrategy,
    MovingAverageCrossoverSettings,
    MovingAverageCrossoverStrategy,
    RangeMeanReversionStrategy,
    StrategyExperiment,
    StrategySettings,
    VolatilityTrendFollowingStrategy,
    VolatilityTrendSettings,
)


class StrategyTestHelpers:
    def setUp(self):
        self.start = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        self.health = {
            "healthy": True,
            "entry_allowed": True,
            "bid": 99.99,
            "ask": 100.01,
            "spread": 0.02,
        }

    def bars(self, *, current_close=None, range_high=100.2, range_low=99.8):
        bars = [
            Bar(
                self.start + timedelta(minutes=index),
                100,
                range_high,
                range_low,
                100,
            )
            for index in range(20)
        ]
        close = 100 if current_close is None else current_close
        bars.append(
            Bar(
                self.start + timedelta(minutes=20),
                close,
                max(close, 100.2),
                min(close, 99.8),
                close,
            )
        )
        return bars

    def quote_for(self, price):
        return {
            "healthy": True,
            "entry_allowed": True,
            "bid": price - 0.01,
            "ask": price + 0.01,
            "spread": 0.02,
        }

    def evaluate(self, strategy, bars, *, health=None, equity=100.0, **kwargs):
        return strategy.evaluate(
            bars,
            decision_time=bars[-1].timestamp,
            market_health=self.health if health is None else health,
            equity=equity,
            **kwargs,
        )


class MomentumBreakoutTests(StrategyTestHelpers, unittest.TestCase):
    def test_long_and_short_breakouts(self):
        strategy = MomentumBreakoutStrategy()
        long_bars = self.bars(current_close=100.4)
        short_bars = self.bars(current_close=99.5)

        long_result = self.evaluate(
            strategy, long_bars, health=self.quote_for(100.4)
        )
        short_result = self.evaluate(
            strategy, short_bars, health=self.quote_for(99.5)
        )

        self.assertEqual((long_result.action, long_result.direction), ("entry", "long"))
        self.assertEqual((short_result.action, short_result.direction), ("entry", "short"))
        self.assertEqual(long_result.position.direction, "long")
        self.assertEqual(short_result.position.direction, "short")

    def test_no_trade_without_breakout_and_future_data_is_rejected(self):
        strategy = MomentumBreakoutStrategy()
        flat = self.evaluate(strategy, self.bars())
        self.assertEqual(flat.action, "no_trade")

        future_bars = self.bars(current_close=100.4)
        decision_time = future_bars[-1].timestamp - timedelta(minutes=1)
        result = strategy.evaluate(
            future_bars,
            decision_time=decision_time,
            market_health=self.quote_for(100.4),
            equity=100,
        )
        self.assertIn("look-ahead", result.reason)
        self.assertEqual(result.action, "no_trade")

    def test_health_spread_and_estimated_costs_fail_closed(self):
        strategy = MomentumBreakoutStrategy()
        bars = self.bars(current_close=100.4)
        unhealthy = {**self.quote_for(100.4), "healthy": False}
        self.assertEqual(
            self.evaluate(strategy, bars, health=unhealthy).action, "no_trade"
        )

        wide_spread = {
            "healthy": True,
            "entry_allowed": True,
            "bid": 99,
            "ask": 101,
            "spread": 2,
        }
        wide_result = self.evaluate(strategy, bars, health=wide_spread)
        self.assertIn("Spread", wide_result.reason)

        costly_settings = StrategySettings(momentum_target_fraction=0.002)
        costly_strategy = MomentumBreakoutStrategy(costly_settings)
        costly_result = self.evaluate(
            costly_strategy, bars, health=self.quote_for(100.4)
        )
        self.assertIn("estimated round-trip costs", costly_result.reason)

    def test_position_size_accounts_for_stop_and_costs_without_leverage(self):
        strategy = MomentumBreakoutStrategy()
        result = self.evaluate(
            strategy, self.bars(current_close=100.4),
            health=self.quote_for(100.4), equity=100,
        )
        self.assertEqual(result.action, "entry")
        position = result.position
        self.assertLessEqual(position.quantity_btc * position.entry_price, 100)
        risk = position.quantity_btc * result.inputs["risk_per_btc"]
        self.assertLessEqual(risk, 100 * strategy.settings.risk_fraction)

    def test_stop_exit_takes_precedence_over_same_bar_target(self):
        strategy = MomentumBreakoutStrategy()
        bars = self.bars(current_close=100.4)
        entry = self.evaluate(strategy, bars, health=self.quote_for(100.4))
        position = entry.position
        bars.append(
            Bar(
                self.start + timedelta(minutes=21),
                position.entry_price,
                position.target_price + 1,
                position.stop_price - 1,
                position.entry_price,
            )
        )
        result = self.evaluate(
            strategy, bars, health=self.health, position=position
        )
        self.assertEqual(result.action, "exit")
        self.assertIn("Stop reached", result.reason)

    def test_target_exit(self):
        strategy = MomentumBreakoutStrategy()
        bars = self.bars(current_close=100.4)
        entry = self.evaluate(strategy, bars, health=self.quote_for(100.4))
        position = entry.position
        bars.append(
            Bar(
                self.start + timedelta(minutes=21),
                position.entry_price,
                position.target_price + 1,
                position.stop_price + 0.1,
                position.entry_price,
            )
        )

        result = self.evaluate(
            strategy, bars, health=self.health, position=position
        )
        self.assertEqual(result.action, "exit")
        self.assertEqual(result.reason, "Target reached.")


class RangeMeanReversionTests(StrategyTestHelpers, unittest.TestCase):
    def test_long_and_short_entries_only_in_range_regime(self):
        strategy = RangeMeanReversionStrategy()
        long_bars = self.bars(current_close=99.52)
        short_bars = self.bars(current_close=100.48)

        long_result = self.evaluate(
            strategy, long_bars, health=self.quote_for(99.52)
        )
        short_result = self.evaluate(
            strategy, short_bars, health=self.quote_for(100.48)
        )

        self.assertEqual((long_result.action, long_result.direction), ("entry", "long"))
        self.assertEqual((short_result.action, short_result.direction), ("entry", "short"))

    def test_trending_regime_and_interior_price_do_not_trade(self):
        strategy = RangeMeanReversionStrategy()
        trending = self.bars(current_close=98.8, range_high=101, range_low=99)
        interior = self.bars(current_close=100)

        self.assertEqual(self.evaluate(strategy, trending).action, "no_trade")
        self.assertEqual(self.evaluate(strategy, interior).action, "no_trade")

    def test_mean_reversion_cost_filter_and_time_exit(self):
        too_costly = RangeMeanReversionStrategy(
            StrategySettings(minimum_net_edge_fraction=0.01)
        )
        bars = self.bars(current_close=99.52)
        result = self.evaluate(
            too_costly, bars, health=self.quote_for(99.52)
        )
        self.assertIn("estimated round-trip costs", result.reason)

        settings = StrategySettings(max_holding_bars=2)
        strategy = RangeMeanReversionStrategy(settings)
        entry = self.evaluate(strategy, bars, health=self.quote_for(99.52))
        position = entry.position
        later_bars = bars + [
            Bar(
                self.start + timedelta(minutes=22),
                99.8,
                99.95,
                99.7,
                99.9,
            )
        ]
        exit_result = self.evaluate(
            strategy, later_bars, position=position
        )
        self.assertEqual(exit_result.action, "exit")
        self.assertIn("Maximum holding time", exit_result.reason)


class AlternativeStrategyTests(StrategyTestHelpers, unittest.TestCase):
    def test_moving_average_crossover_opens_long_after_warmup(self):
        closes = [100.0] * 20 + [99.0] * 10 + [110.0]
        bars = [
            Bar(
                self.start + timedelta(minutes=index),
                close,
                close * 1.001,
                close * 0.999,
                close,
            )
            for index, close in enumerate(closes)
        ]
        strategy = MovingAverageCrossoverStrategy()
        result = self.evaluate(
            strategy,
            bars,
            health=self.quote_for(110.0),
        )

        self.assertEqual(strategy.minimum_history_bars, 31)
        self.assertEqual((result.action, result.direction), ("entry", "long"))
        self.assertEqual(result.position.direction, "long")
        self.assertEqual(result.inputs["fast_period"], 10)
        self.assertEqual(result.inputs["slow_period"], 30)

    def test_moving_average_crossover_requires_slow_period_warmup(self):
        strategy = MovingAverageCrossoverStrategy()
        bars = [
            Bar(
                self.start + timedelta(minutes=index),
                100,
                100.1,
                99.9,
                100,
            )
            for index in range(strategy.minimum_history_bars - 1)
        ]

        result = self.evaluate(strategy, bars)

        self.assertEqual(result.action, "no_trade")
        self.assertIn("Insufficient completed bars", result.reason)

    def test_moving_average_settings_reject_invalid_periods(self):
        for values in (
            {"fast_period": 1},
            {"fast_period": 10, "slow_period": 10},
            {"fast_period": 31, "slow_period": 30},
            {"fast_period": True},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    MovingAverageCrossoverSettings(**values)

    def test_moving_average_down_cross_does_not_open_short(self):
        closes = [99.0] * 20 + [100.0] * 11
        bars = [
            Bar(
                self.start + timedelta(minutes=index),
                close,
                close * 1.001,
                close * 0.999,
                close,
            )
            for index, close in enumerate(closes)
        ]

        result = self.evaluate(
            MovingAverageCrossoverStrategy(),
            bars,
            health=self.quote_for(100.0),
        )

        self.assertEqual(result.action, "no_trade")
        self.assertIsNone(result.position)

    def test_volatility_filtered_trend_breakout_opens_long(self):
        bars = [
            Bar(
                self.start + timedelta(minutes=index),
                100,
                100.1,
                99.9,
                100,
            )
            for index in range(20)
        ]
        bars.append(
            Bar(
                self.start + timedelta(minutes=20),
                100.1,
                100.4,
                100.0,
                100.3,
            )
        )
        strategy = VolatilityTrendFollowingStrategy()

        result = self.evaluate(
            strategy,
            bars,
            health=self.quote_for(100.3),
        )

        self.assertEqual((result.action, result.direction), ("entry", "long"))
        self.assertEqual(result.position.direction, "long")
        self.assertGreaterEqual(
            result.inputs["atr_fraction"],
            strategy.settings.minimum_atr_fraction,
        )

    def test_volatility_filter_blocks_quiet_market_and_downtrend(self):
        quiet_bars = [
            Bar(
                self.start + timedelta(minutes=index),
                100,
                100.001,
                99.999,
                100,
            )
            for index in range(20)
        ]
        quiet_bars.append(
            Bar(
                self.start + timedelta(minutes=20),
                100.09,
                100.101,
                100.089,
                100.1,
            )
        )
        downward = [
            Bar(
                self.start + timedelta(minutes=index),
                100 - index * 0.1,
                100 - index * 0.1 + 0.05,
                100 - index * 0.1 - 0.05,
                100 - index * 0.1,
            )
            for index in range(21)
        ]
        strategy = VolatilityTrendFollowingStrategy()

        quiet = self.evaluate(
            strategy,
            quiet_bars,
            health=self.quote_for(100.1),
        )
        falling = self.evaluate(
            strategy,
            downward,
            health=self.quote_for(downward[-1].close),
        )

        self.assertEqual(quiet.action, "no_trade")
        self.assertEqual(falling.action, "no_trade")
        self.assertIsNone(falling.position)

    def test_volatility_settings_reject_invalid_filters(self):
        for values in (
            {"atr_period": 0},
            {"lookback_bars": 10, "atr_period": 14},
            {"minimum_atr_fraction": 0.02, "maximum_atr_fraction": 0.01},
            {"maximum_atr_fraction": float("inf")},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    VolatilityTrendSettings(**values)

    def test_alternative_strategies_reject_future_candles(self):
        strategies = (
            MovingAverageCrossoverStrategy(),
            VolatilityTrendFollowingStrategy(),
        )
        for strategy in strategies:
            bars = [
                Bar(
                    self.start + timedelta(minutes=index),
                    100,
                    100.1,
                    99.9,
                    100,
                )
                for index in range(strategy.minimum_history_bars)
            ]
            result = strategy.evaluate(
                bars,
                decision_time=bars[-1].timestamp - timedelta(minutes=1),
                market_health=self.quote_for(100),
                equity=100,
            )
            with self.subTest(strategy=strategy.name):
                self.assertEqual(result.action, "no_trade")
                self.assertIn("look-ahead", result.reason)


class StrategyExperimentTests(StrategyTestHelpers, unittest.TestCase):
    def test_only_one_strategy_can_open_a_position_at_a_time(self):
        low_costs = StrategySettings(
            fee_rate_per_side=0,
            slippage_rate_per_side=0,
            minimum_net_edge_fraction=0,
        )
        experiment = StrategyExperiment(
            MomentumBreakoutStrategy(low_costs),
            RangeMeanReversionStrategy(low_costs),
        )
        bars = self.bars(current_close=100.4)
        results = experiment.evaluate(
            bars,
            decision_time=bars[-1].timestamp,
            market_health=self.quote_for(100.4),
            equity=100,
        )

        entries = [result for result in results.values() if result.action == "entry"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].strategy, "momentum_breakout")
        self.assertIn(
            "Position belongs to another strategy.",
            results["range_mean_reversion"].reason,
        )

        follow_up = experiment.evaluate(
            bars,
            decision_time=bars[-1].timestamp,
            market_health=self.quote_for(100.4),
            equity=100,
            position=entries[0].position,
        )
        self.assertNotIn("entry", [result.action for result in follow_up.values()])


if __name__ == "__main__":
    unittest.main()
