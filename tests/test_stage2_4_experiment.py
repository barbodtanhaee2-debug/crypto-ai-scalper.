"""Isolated Stage 2.4 helpers; does not change production trading."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quote-tape" / "replays" / "stage2_4"))

from run_stage2_4 import match_trades, scaled_settings  # noqa: E402


class Stage24ExperimentTests(unittest.TestCase):
    def test_scaled_settings_do_not_mutate_baseline_defaults(self):
        one = scaled_settings(1.0)
        two = scaled_settings(2.0)
        self.assertEqual(one.fee_rate_per_side, 0.001)
        self.assertEqual(two.fee_rate_per_side, 0.002)
        self.assertEqual(two.slippage_rate_per_side, 0.001)
        self.assertEqual(two.spread_fraction, 0.0004)
        self.assertEqual(scaled_settings(1.0).fee_rate_per_side, 0.001)

    def test_match_requires_nearby_same_direction_entries(self):
        quote = [
            {
                "direction": "long",
                "entry_timestamp": "2026-10-30T20:10:00.020000+00:00",
                "exit_timestamp": "2026-10-30T20:20:00.020000+00:00",
                "entry_fill_price": 100.0,
                "exit_fill_price": 101.0,
                "net_realized_pnl": 0.1,
                "gross_pnl": 0.2,
                "entry_fee": 0.01,
                "exit_fee": 0.01,
                "reason": "Maximum holding time reached.",
                "trigger": "strategy_exit",
            }
        ]
        candle = [
            {
                "direction": "long",
                "entry_timestamp": "2026-10-30T20:09:59.999000+00:00",
                "exit_timestamp": "2026-10-30T20:19:59.999000+00:00",
                "entry_fill_price": 100.2,
                "exit_fill_price": 101.1,
                "net_realized_pnl": 0.05,
                "gross_pnl": 0.15,
                "entry_fee": 0.01,
                "exit_fee": 0.01,
                "reason": "Maximum holding time reached.",
            },
            {
                "direction": "long",
                "entry_timestamp": "2026-10-30T22:00:00+00:00",
                "exit_timestamp": "2026-10-30T22:10:00+00:00",
                "entry_fill_price": 110.0,
                "exit_fill_price": 111.0,
                "net_realized_pnl": 0.02,
                "gross_pnl": 0.03,
                "entry_fee": 0.01,
                "exit_fee": 0.01,
                "reason": "stop",
            },
        ]
        result = match_trades(quote, candle)
        self.assertEqual(len(result["matched"]), 1)
        self.assertEqual(len(result["unmatched_candle"]), 1)
        self.assertEqual(result["unmatched_candle"][0]["entry_timestamp"], "2026-10-30T22:00:00+00:00")
        self.assertLess(result["matched"][0]["entry_time_delta_seconds"], 1.0)

    def test_does_not_force_match_across_large_gap(self):
        quote = [
            {
                "direction": "long",
                "entry_timestamp": "2026-10-30T20:00:00+00:00",
                "exit_timestamp": "2026-10-30T20:10:00+00:00",
                "entry_fill_price": 1,
                "exit_fill_price": 1,
                "net_realized_pnl": 0,
                "gross_pnl": 0,
                "entry_fee": 0,
                "exit_fee": 0,
                "reason": "x",
                "trigger": "x",
            }
        ]
        candle = [
            {
                "direction": "long",
                "entry_timestamp": "2026-10-31T04:00:00+00:00",
                "exit_timestamp": "2026-10-31T04:10:00+00:00",
                "entry_fill_price": 1,
                "exit_fill_price": 1,
                "net_realized_pnl": 0,
                "gross_pnl": 0,
                "entry_fee": 0,
                "exit_fee": 0,
                "reason": "y",
            }
        ]
        result = match_trades(quote, candle)
        self.assertEqual(result["matched"], [])
        self.assertEqual(len(result["unmatched_quote"]), 1)
        self.assertEqual(len(result["unmatched_candle"]), 1)


if __name__ == "__main__":
    unittest.main()
