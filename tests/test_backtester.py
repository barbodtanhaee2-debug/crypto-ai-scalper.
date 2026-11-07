import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtester import (
    BacktestSettings,
    Backtester,
    _drawdown,
    _metric_set,
    load_candles_csv,
    run_strategy_comparison,
    validate_candles,
)
from paper_broker import EXIT_FILL_DECISION_PRICE
from config import LIVE_EXECUTION_ENABLED, PAPER_TRADING_ONLY
from strategy import (
    Bar,
    MomentumBreakoutStrategy,
    MovingAverageCrossoverSettings,
    MovingAverageCrossoverStrategy,
    RangeMeanReversionStrategy,
    StrategySettings,
    VolatilityTrendFollowingStrategy,
    VolatilityTrendSettings,
)


class BacktesterTests(unittest.TestCase):
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

    def flat_bars(self, count=20, *, high=100.2, low=99.8):
        return [(100, high, low, 100)] * count

    def run_strategy(self, bars, strategy, **kwargs):
        settings = kwargs.pop("settings", BacktestSettings())
        return Backtester(settings).run(bars, strategy, **kwargs)

    def test_momentum_replay_is_spot_long_only_without_lookahead(self):
        prior = self.flat_bars()
        long_bars = self.bars(
            prior
            + [
                (100.0, 100.5, 99.9, 100.4),
                (100.4, 101.5, 100.3, 101.2),
            ]
        )
        short_bars = self.bars(
            prior
            + [
                (100.0, 100.1, 99.4, 99.5),
                (99.5, 99.6, 98.5, 98.8),
            ]
        )
        strategy = MomentumBreakoutStrategy()
        long_result = self.run_strategy(long_bars, strategy)
        short_result = self.run_strategy(short_bars, strategy)

        self.assertGreaterEqual(long_result["overall"]["trade_count"], 1)
        self.assertEqual(short_result["overall"]["trade_count"], 0)
        self.assertGreater(long_result["long"]["trade_count"], 0)
        self.assertNotIn("short", long_result)
        self.assertNotIn("short", short_result)
        self.assertEqual(long_result["market"], "Binance Spot")
        self.assertEqual(long_result["position_mode"], "Long-only")
        self.assertEqual(long_result["account_currency"], "USDT")
        self.assertEqual(
            long_result["overall"]["currency"],
            "USDT",
        )

    def test_range_mean_reversion_is_replayed_independently(self):
        prior = self.flat_bars(high=100.7, low=99.3)
        bars = self.bars(
            prior
            + [
                (99.7, 99.9, 99.4, 99.55),
                (99.6, 100.1, 99.5, 100.0),
            ]
        )
        result = self.run_strategy(bars, RangeMeanReversionStrategy())
        self.assertGreater(result["overall"]["trade_count"], 0)
        self.assertGreater(result["long"]["trade_count"], 0)
        self.assertGreater(result["overall"]["total_fees"], 0)

    def test_range_mean_reversion_short_signals_do_not_enter_spot_results(self):
        prior = self.flat_bars(high=100.7, low=99.3)
        bars = self.bars(
            prior
            + [
                (100.3, 100.6, 100.1, 100.45),
                (100.45, 100.6, 99.8, 100.0),
            ]
        )
        result = self.run_strategy(bars, RangeMeanReversionStrategy())
        self.assertNotIn("short", result)
        self.assertEqual(result["overall"]["trade_count"], 0)

    def test_chronological_replay_and_development_oos_trade_gate(self):
        bars = self.bars(
            self.flat_bars()
            + [
                (100, 100.5, 99.9, 100.4),
                (100.4, 101.5, 100.3, 101.2),
                (101.2, 101.3, 100.8, 101),
                (101, 101.5, 100.9, 101.4),
            ]
        )
        boundary = bars[20].timestamp
        result = Backtester().run(
            bars,
            MomentumBreakoutStrategy(),
            evaluation_start=boundary + timedelta(minutes=1),
        )
        self.assertEqual(
            result["evaluation_start"],
            (boundary + timedelta(minutes=1)).isoformat(),
        )
        self.assertGreater(result["overall"]["trade_count"], 0)
        self.assertGreaterEqual(result["candles_processed"], len(bars))

    def test_no_future_candle_is_used_before_its_timestamp(self):
        bars = self.bars(
            self.flat_bars()
            + [
                (100, 100.5, 99.9, 100.4),
                (100.4, 101.5, 100.3, 101.2),
            ]
        )
        with tempfile.TemporaryDirectory() as root:
            result = self.run_strategy(
                bars, MomentumBreakoutStrategy(), output_dir=root
            )
            with (Path(root) / "trades.csv").open(
                newline="", encoding="utf-8"
            ) as source:
                trades = list(csv.DictReader(source))
        self.assertEqual(trades[0]["entry_timestamp"], bars[20].timestamp.isoformat())
        self.assertGreater(result["overall"]["trade_count"], 0)
        self.assertEqual(result["data_end"], bars[-1].timestamp.isoformat())
        # The breakout is only available after bar 20 has closed.
        self.assertGreaterEqual(result["candles_processed"], 22)

    def test_candle_validation_rejects_bad_timestamps_gaps_and_ohlc(self):
        valid = self.bars(self.flat_bars(3))
        invalid_datasets = (
            valid[:1] + [valid[0]],
            valid[:2] + [Bar(valid[2].timestamp + timedelta(minutes=1), 100, 101, 99, 100)],
            valid[:2] + [Bar(datetime(2026, 1, 1, 2), 100, 101, 99, 100)],
            valid[:2] + [Bar(valid[2].timestamp, 100, 99, 101, 100)],
            [Bar(datetime(2026, 1, 1), 100, 101, 99, 100)],
        )
        for dataset in invalid_datasets:
            with self.subTest(dataset=dataset):
                with self.assertRaises(ValueError):
                    validate_candles(dataset)

    def test_zero_trade_and_zero_loss_metrics_are_safe(self):
        no_signal = self.bars(self.flat_bars(24))
        result = self.run_strategy(no_signal, MomentumBreakoutStrategy())
        self.assertEqual(result["overall"]["trade_count"], 0)
        self.assertIsNone(result["overall"]["win_rate"])
        self.assertIn("undefined", result["overall"]["win_rate_note"])
        self.assertIsNone(result["overall"]["expectancy_after_costs_usdt"])
        self.assertIsNone(result["overall"]["profit_factor"])

        profitable = self.bars(
            self.flat_bars()
            + [
                (100, 100.5, 99.9, 100.4),
                (100.4, 101.5, 100.3, 101.2),
            ]
        )
        with tempfile.TemporaryDirectory() as root:
            reports = self.run_strategy(
                profitable,
                MomentumBreakoutStrategy(),
                output_dir=root,
            )
            self.assertTrue((Path(root) / "results.json").exists())
            self.assertTrue((Path(root) / "trades.csv").exists())
            saved = json.loads((Path(root) / "results.json").read_text())
            self.assertEqual(saved["overall"]["trade_count"], reports["overall"]["trade_count"])
            with (Path(root) / "trades.csv").open(newline="", encoding="utf-8") as source:
                saved_trades = list(csv.DictReader(source))
            self.assertGreater(len(saved_trades), 0)
            self.assertTrue(
                all(float(trade["net_realized_pnl"]) > 0 for trade in saved_trades)
            )
            self.assertIsNone(reports["overall"]["profit_factor"])

    def test_chronological_development_and_oos_are_replayed_separately(self):
        values = self.flat_bars()
        values.extend(
            [
                (100, 100.5, 99.9, 100.4),
                (100.4, 101.5, 100.3, 101.2),
            ]
        )
        bars = self.bars(values)
        boundary = bars[20].timestamp
        development = [bar for bar in bars if bar.timestamp <= boundary]
        out_of_sample = [bar for bar in bars if bar.timestamp > boundary]

        dev = self.run_strategy(development, MomentumBreakoutStrategy())
        oos = self.run_strategy(
            bars,
            MomentumBreakoutStrategy(),
            evaluation_start=out_of_sample[0].timestamp,
            evaluation_end=out_of_sample[-1].timestamp,
        )
        self.assertEqual(dev["data_end"], boundary.isoformat())
        self.assertEqual(oos["candles_processed"], len(bars))
        self.assertGreater(oos["overall"]["trade_count"], 0)

    def test_daily_loss_limit_blocks_later_entry(self):
        values = self.flat_bars()
        values.extend(
            [
                (100, 100.5, 99.9, 100.4),
                (100.3, 100.4, 100.0, 100.1),
                (100.1, 100.9, 100.0, 100.8),
                (100.8, 101.8, 100.7, 101.5),
            ]
        )
        for limit in ("daily", "drawdown"):
            with self.subTest(limit=limit):
                result = self.run_strategy(
                    self.bars(values),
                    MomentumBreakoutStrategy(),
                    settings=BacktestSettings(
                        daily_loss_limit_fraction=(
                            0.00001 if limit == "daily" else 0.99
                        ),
                        max_drawdown_fraction=(
                            0.00001 if limit == "drawdown" else 0.99
                        ),
                    ),
                )
                self.assertEqual(result["overall"]["trade_count"], 1)
                self.assertGreaterEqual(result["risk_rejection_count"], 1)

    def test_live_execution_remains_disabled_and_costs_are_explicit(self):
        self.assertIs(PAPER_TRADING_ONLY, True)
        self.assertIs(LIVE_EXECUTION_ENABLED, False)
        settings = BacktestSettings(
            fee_rate_per_side=0.002,
            spread_fraction=0.0004,
            slippage_rate_per_side=0.001,
            latency_seconds=2,
            latency_slippage_per_second=0.0001,
        )
        self.assertAlmostEqual(settings.effective_slippage_rate_per_side, 0.0012)
        self.assertEqual(BacktestSettings().initial_equity, 100.0)

    def test_report_discloses_spot_assumptions_period_and_costs(self):
        bars = self.bars(
            self.flat_bars()
            + [
                (100, 100.5, 99.9, 100.4),
                (100.4, 101.5, 100.3, 101.2),
            ]
        )
        result = self.run_strategy(
            bars,
            MomentumBreakoutStrategy(),
        )

        self.assertEqual(
            result["sample_period"],
            {"start": bars[20].timestamp.isoformat(), "end": bars[-1].timestamp.isoformat()},
        )
        self.assertEqual(result["warmup_candles_excluded_from_performance"], 20)
        assumptions = result["assumptions"]
        self.assertIn("candle-close based", assumptions["intrabar_execution"])
        self.assertIn("stop first", assumptions["same_candle_stop_and_target"])
        self.assertIn("candle-close-derived bid", assumptions["forced_liquidation"])
        self.assertGreater(result["overall"]["total_fees"], 0)
        self.assertGreater(result["overall"]["total_costs"], 0)
        metrics = result["overall"]
        self.assertAlmostEqual(
            metrics["gross_pnl_before_costs"]
            - metrics["total_modeled_transaction_costs_including_fees"],
            result["overall"]["final_equity"] - result["overall"]["initial_equity"],
            places=8,
        )
        self.assertAlmostEqual(
            metrics["total_fees"]
            + metrics["modeled_spread_costs_excluding_fees"]
            + metrics["modeled_slippage_costs_excluding_fees"],
            metrics["total_modeled_transaction_costs_including_fees"],
            places=8,
        )

    def test_force_liquidation_and_market_exposure_are_reported(self):
        bars = self.bars(
            self.flat_bars()
            + [
                (100, 100.5, 99.9, 100.4),
                (100.4, 100.5, 100.3, 100.4),
            ]
        )
        result = self.run_strategy(bars, MomentumBreakoutStrategy())
        metrics = result["overall"]

        self.assertTrue(metrics["position_open_at_period_end"])
        self.assertTrue(metrics["period_boundary_force_liquidation"])
        self.assertEqual(metrics["open_positions_remaining"], 0)
        self.assertEqual(metrics["trade_count"], 1)
        self.assertGreater(metrics["market_exposure_percent"], 0)
        self.assertEqual(metrics["maximum_consecutive_losses"], 1)
        self.assertLess(metrics["average_losing_trade_usdt"], 0)
        self.assertEqual(
            metrics["expectancy_after_costs_usdt"],
            metrics["average_losing_trade_usdt"],
        )
        self.assertIsNotNone(metrics["average_trade_return_fraction"])
        self.assertIn("candle-close", metrics["maximum_drawdown_method"])

    def test_drawdown_uses_peak_to_subsequent_trough(self):
        self.assertAlmostEqual(_drawdown([100, 120, 90, 110]), 0.25)
        self.assertEqual(_drawdown([100, 110, 115]), 0.0)

    def test_trade_metrics_calculate_expectancy_costs_and_loss_streaks(self):
        trades = []
        for net_pnl in (2.0, -1.0, -3.0, 1.0):
            trades.append({
                "direction": "long",
                "net_realized_pnl": net_pnl,
                "gross_pnl": net_pnl + 0.2,
                "entry_fee": 0.1,
                "exit_fee": 0.1,
                "entry_spread_cost_estimate": 0.05,
                "exit_spread_cost_estimate": 0.05,
                "entry_slippage_cost_estimate": 0.1,
                "exit_slippage_cost_estimate": 0.1,
                "total_costs": 0.5,
                "entry_fill_price": 100.0,
                "quantity_btc": 1.0,
            })

        metrics = _metric_set(
            trades,
            100.0,
            99.0,
            equity_curve=[100.0, 102.0, 100.0, 97.0, 99.0],
            exposure_bars=2,
            evaluated_bars=4,
            position_open_at_period_end=False,
            open_positions_remaining=0,
            forced_liquidation=False,
        )

        self.assertEqual(metrics["trade_count"], 4)
        self.assertEqual(metrics["win_rate"], 0.5)
        self.assertAlmostEqual(metrics["profit_factor"], 0.75)
        self.assertEqual(metrics["average_winning_trade_usdt"], 1.5)
        self.assertEqual(metrics["average_losing_trade_usdt"], -2.0)
        self.assertEqual(metrics["expectancy_after_costs_usdt"], -0.25)
        self.assertEqual(metrics["maximum_consecutive_losses"], 2)
        self.assertEqual(metrics["total_fees"], 0.8)
        self.assertEqual(metrics["modeled_spread_costs_excluding_fees"], 0.4)
        self.assertEqual(metrics["modeled_slippage_costs_excluding_fees"], 0.8)
        self.assertAlmostEqual(metrics["total_costs"], 2.0)
        self.assertEqual(metrics["market_exposure_percent"], 50.0)
        self.assertAlmostEqual(
            metrics["maximum_drawdown_fraction"],
            5 / 102,
        )

    def test_higher_fee_and_slippage_reduce_net_result(self):
        bars = self.bars(
            self.flat_bars()
            + [
                (100, 100.5, 99.9, 100.4),
                (100.4, 100.5, 100.3, 100.4),
            ]
        )
        strategy = MomentumBreakoutStrategy(
            StrategySettings(momentum_target_fraction=0.02)
        )
        baseline = self.run_strategy(bars, strategy)
        stressed = self.run_strategy(
            bars,
            strategy,
            settings=BacktestSettings(
                fee_rate_per_side=0.002,
                slippage_rate_per_side=0.001,
            ),
        )

        self.assertLess(
            stressed["overall"]["net_return_fraction"],
            baseline["overall"]["net_return_fraction"],
        )

    def test_comparison_runs_four_frozen_strategies_over_chronological_periods(self):
        bars = self.bars(self.flat_bars(150))
        strategies = (
            MomentumBreakoutStrategy(),
            RangeMeanReversionStrategy(),
            MovingAverageCrossoverStrategy(
                MovingAverageCrossoverSettings(
                    lookback_bars=10,
                    fast_period=5,
                    slow_period=10,
                )
            ),
            VolatilityTrendFollowingStrategy(
                VolatilityTrendSettings(lookback_bars=20, atr_period=14)
            ),
        )
        results = run_strategy_comparison(
            bars,
            strategies,
            BacktestSettings(),
            training_end=bars[59].timestamp,
            validation_end=bars[99].timestamp,
        )

        self.assertEqual(set(results["strategies"]), {
            "momentum_breakout",
            "range_mean_reversion",
            "moving_average_crossover",
            "volatility_trend_following",
        })
        self.assertTrue(results["methodology"]["parameters_frozen_before_validation"])
        self.assertFalse(results["methodology"]["automated_candidate_selection"])
        for strategy_periods in results["strategies"].values():
            self.assertEqual(
                set(strategy_periods),
                {"training", "validation", "out_of_sample"},
            )
            train = strategy_periods["training"]["baseline_costs"]
            validation = strategy_periods["validation"]["baseline_costs"]
            out_of_sample = strategy_periods["out_of_sample"]["baseline_costs"]
            self.assertLessEqual(
                train["evaluation_candles_end"],
                validation["evaluation_candles_start"],
            )
            self.assertLess(
                validation["evaluation_candles_end"],
                out_of_sample["evaluation_candles_start"],
            )
            self.assertIn(
                "higher_cost_sensitivity",
                strategy_periods["out_of_sample"],
            )
            self.assertEqual(
                train["strategy_parameters"],
                validation["strategy_parameters"],
            )
            self.assertEqual(
                validation["strategy_parameters"],
                out_of_sample["strategy_parameters"],
            )

    def test_comparison_rejects_empty_or_insufficient_partitions(self):
        bars = self.bars(self.flat_bars(40))
        with self.assertRaisesRegex(ValueError, "after the strategy warm-up"):
            self.run_strategy(
                self.bars(self.flat_bars(20)),
                MomentumBreakoutStrategy(),
            )
        with self.assertRaisesRegex(ValueError, "all three chronological"):
            run_strategy_comparison(
                bars,
                (MomentumBreakoutStrategy(),),
                BacktestSettings(),
                training_end=bars[20].timestamp,
                validation_end=bars[-1].timestamp,
            )
        with self.assertRaisesRegex(ValueError, "empty"):
            run_strategy_comparison(
                [],
                (MomentumBreakoutStrategy(),),
                BacktestSettings(),
                training_end=self.start,
                validation_end=self.start + timedelta(minutes=1),
            )

    def test_csv_input_requires_explicit_timezone_and_valid_columns(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "candles.csv"
            path.write_text(
                "timestamp,open,high,low,close\n"
                "2026-01-01T00:00:00+00:00,100,101,99,100\n",
                encoding="utf-8",
            )
            self.assertEqual(len(load_candles_csv(path)), 1)
            path.write_text(
                "timestamp,open,high,low,close\n"
                "2026-01-01T00:00:00,100,101,99,100\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_candles_csv(path)

    def test_decision_exit_price_changes_stop_fill_not_entry_fill(self):
        prior = self.flat_bars()
        bars = self.bars(
            prior
            + [
                (100.0, 100.5, 99.9, 100.4),
                (100.4, 100.5, 99.0, 100.35),
            ]
        )
        quote = self.run_strategy(bars, MomentumBreakoutStrategy())
        decision = self.run_strategy(
            bars,
            MomentumBreakoutStrategy(),
            settings=BacktestSettings(exit_fill_mode=EXIT_FILL_DECISION_PRICE),
        )
        self.assertEqual(quote["overall"]["trade_count"], 1)
        self.assertEqual(decision["overall"]["trade_count"], 1)
        self.assertEqual(
            quote["assumptions"]["exit_fill_mode"],
            "executable_quote",
        )
        self.assertEqual(
            decision["assumptions"]["exit_fill_mode"],
            EXIT_FILL_DECISION_PRICE,
        )

        with tempfile.TemporaryDirectory() as directory:
            quote_dir = Path(directory) / "quote"
            decision_dir = Path(directory) / "decision"
            self.run_strategy(
                bars, MomentumBreakoutStrategy(), output_dir=quote_dir
            )
            self.run_strategy(
                bars,
                MomentumBreakoutStrategy(),
                settings=BacktestSettings(exit_fill_mode=EXIT_FILL_DECISION_PRICE),
                output_dir=decision_dir,
            )
            with (quote_dir / "trades.csv").open(encoding="utf-8", newline="") as source:
                quote_trade = next(csv.DictReader(source))
            with (decision_dir / "trades.csv").open(
                encoding="utf-8", newline=""
            ) as source:
                decision_trade = next(csv.DictReader(source))
        self.assertEqual(quote_trade["entry_timestamp"], decision_trade["entry_timestamp"])
        self.assertAlmostEqual(
            float(quote_trade["entry_fill_price"]),
            float(decision_trade["entry_fill_price"]),
        )
        self.assertLess(
            float(decision_trade["exit_fill_price"]),
            float(quote_trade["exit_fill_price"]),
        )
        self.assertIn("Stop", quote_trade["reason"])
        self.assertIn("Stop", decision_trade["reason"])


if __name__ == "__main__":
    unittest.main()
