import unittest
from pathlib import Path

from baseline_audit import (
    FROZEN_RESULTS,
    compare_trade_files,
    cost_filter_thresholds,
    reconstruct_fill_math,
    _trade_rows,
)


class BaselineAuditTests(unittest.TestCase):
    def test_two_x_costs_block_the_frozen_zero_point_six_percent_target(self):
        thresholds = cost_filter_thresholds()
        expected_move = thresholds["synthetic_ask_expected_move_from_0_6pct_target"]
        self.assertGreater(
            expected_move,
            thresholds["baseline_cost_plus_min_edge"],
        )
        self.assertLess(
            expected_move,
            thresholds["stressed_cost_plus_min_edge"],
        )
        self.assertAlmostEqual(thresholds["baseline_estimated_round_trip_cost"], 0.0032)
        self.assertAlmostEqual(thresholds["stressed_estimated_round_trip_cost"], 0.0062)

    def test_synthetic_close_quote_matches_first_saved_oos_momentum_fills(self):
        trades_path = (
            FROZEN_RESULTS / "momentum_breakout" / "out_of_sample" / "trades.csv"
        )
        if not trades_path.exists():
            self.skipTest("Frozen baseline OOS trades are not present.")
        trade = _trade_rows(trades_path)[0]
        entry = reconstruct_fill_math(116111.28)
        exit_fill = reconstruct_fill_math(116402.65)
        self.assertAlmostEqual(
            float(trade["entry_fill_price"]),
            entry["entry_fill"],
        )
        self.assertAlmostEqual(
            float(trade["exit_fill_price"]),
            exit_fill["exit_fill_if_same_close"],
        )

    def test_saved_momentum_and_trend_trades_match_in_every_period(self):
        if not FROZEN_RESULTS.exists():
            self.skipTest("Frozen baseline results are not present.")
        for period in ("training", "validation", "out_of_sample"):
            comparison = compare_trade_files(
                FROZEN_RESULTS / "momentum_breakout" / period / "trades.csv",
                FROZEN_RESULTS / "volatility_trend_following" / period / "trades.csv",
            )
            self.assertTrue(
                comparison["equal_except_ignored"],
                comparison,
            )
