import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtester import BacktestSettings
from baseline import (
    FROZEN_BACKTEST_SETTINGS,
    FROZEN_BASELINE_STRATEGY_PARAMETERS,
    FROZEN_TRAINING_END,
    FROZEN_VALIDATION_END,
    frozen_baseline_strategies,
    validate_historical_dataset,
    verify_chronological_splits,
)
from strategy import (
    Bar,
    MomentumBreakoutStrategy,
    MovingAverageCrossoverStrategy,
    RangeMeanReversionStrategy,
    VolatilityTrendFollowingStrategy,
)


class FrozenBaselineTests(unittest.TestCase):
    def test_frozen_candidates_match_lockfile_defaults(self):
        strategies = frozen_baseline_strategies()
        names = [strategy.name for strategy in strategies]
        self.assertEqual(
            names,
            [
                "momentum_breakout",
                "range_mean_reversion",
                "moving_average_crossover",
                "volatility_trend_following",
            ],
        )
        self.assertIsInstance(strategies[0], MomentumBreakoutStrategy)
        self.assertIsInstance(strategies[1], RangeMeanReversionStrategy)
        self.assertIsInstance(strategies[2], MovingAverageCrossoverStrategy)
        self.assertIsInstance(strategies[3], VolatilityTrendFollowingStrategy)
        for strategy in strategies:
            self.assertEqual(
                asdict(strategy.settings),
                FROZEN_BASELINE_STRATEGY_PARAMETERS[strategy.name],
            )
        self.assertEqual(
            FROZEN_BACKTEST_SETTINGS,
            BacktestSettings(candle_interval_seconds=3600),
        )
        self.assertLess(FROZEN_TRAINING_END, FROZEN_VALIDATION_END)

    def test_dataset_and_splits_reject_invalid_history(self):
        start = datetime(2025, 1, 1, 0, 59, 59, 999000, tzinfo=timezone.utc)
        rows = [
            Bar(start + timedelta(hours=index), 100, 101, 99, 100)
            for index in range(6)
        ]
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "candles.csv"
            csv_path.write_text(
                "timestamp,open,high,low,close\n"
                + "".join(
                    f"{bar.timestamp.isoformat()},{bar.open},{bar.high},{bar.low},{bar.close}\n"
                    for bar in rows
                ),
                encoding="utf-8",
            )
            dataset = validate_historical_dataset(
                csv_path,
                interval_seconds=3600,
                expected_count=6,
                expected_first=rows[0].timestamp,
                expected_last=rows[-1].timestamp,
            )
            self.assertTrue(dataset["valid"])
            splits = verify_chronological_splits(
                dataset["candles"],
                training_end=rows[1].timestamp,
                validation_end=rows[3].timestamp,
            )
            self.assertTrue(splits["partitions_are_chronological"])
            self.assertFalse(splits["out_of_sample_used_for_tuning"])
            self.assertFalse(splits["automated_candidate_selection"])
            with self.assertRaisesRegex(ValueError, "row count"):
                validate_historical_dataset(
                    csv_path,
                    interval_seconds=3600,
                    expected_count=5,
                )
            with self.assertRaisesRegex(ValueError, "precede"):
                verify_chronological_splits(
                    dataset["candles"],
                    training_end=rows[3].timestamp,
                    validation_end=rows[1].timestamp,
                )
