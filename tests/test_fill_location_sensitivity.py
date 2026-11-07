import tempfile
import unittest
from pathlib import Path

from backtester import BacktestSettings
from fill_location_sensitivity import _classification, compare_trade_ledgers
from paper_broker import EXIT_FILL_DECISION_PRICE, PaperBrokerSettings


class FillLocationSensitivityTests(unittest.TestCase):
    def test_invalid_exit_fill_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exit_fill_mode"):
            BacktestSettings(exit_fill_mode="midpoint")
        with self.assertRaisesRegex(ValueError, "exit_fill_mode"):
            PaperBrokerSettings(exit_fill_mode="midpoint")

    def test_trade_ledger_comparison_counts_classification_changes(self):
        baseline = [
            {
                "entry_timestamp": "2025-10-01T00:00:00+00:00",
                "exit_timestamp": "2025-10-01T01:00:00+00:00",
                "reason": "Target reached.",
                "entry_fill_price": "100",
                "exit_fill_price": "99.5",
                "quantity_btc": "1",
                "net_realized_pnl": "-0.2",
            },
            {
                "entry_timestamp": "2025-10-02T00:00:00+00:00",
                "exit_timestamp": "2025-10-02T01:00:00+00:00",
                "reason": "Stop reached.",
                "entry_fill_price": "100",
                "exit_fill_price": "99.0",
                "quantity_btc": "1",
                "net_realized_pnl": "-1.0",
            },
        ]
        sensitivity = [
            {
                "entry_timestamp": "2025-10-01T00:00:00+00:00",
                "exit_timestamp": "2025-10-01T01:00:00+00:00",
                "reason": "Target reached.",
                "entry_fill_price": "100",
                "exit_fill_price": "100.6",
                "quantity_btc": "1",
                "net_realized_pnl": "0.4",
            },
            {
                "entry_timestamp": "2025-10-03T00:00:00+00:00",
                "exit_timestamp": "2025-10-03T01:00:00+00:00",
                "reason": "Stop reached.",
                "entry_fill_price": "100",
                "exit_fill_price": "99.8",
                "quantity_btc": "1",
                "net_realized_pnl": "-0.3",
            },
        ]
        comparison = compare_trade_ledgers(baseline, sensitivity)
        self.assertEqual(comparison["matched_by_entry_timestamp"], 1)
        self.assertEqual(comparison["classification_changes_among_matches"], 1)
        self.assertEqual(comparison["matched_trades_with_exit_fill_change"], 1)
        self.assertEqual(
            comparison["unmatched_executable_quote_entries"],
            ["2025-10-02T00:00:00+00:00"],
        )
        self.assertEqual(
            comparison["unmatched_decision_price_entries"],
            ["2025-10-03T00:00:00+00:00"],
        )
        self.assertEqual(_classification(0.0), "scratch")
        self.assertEqual(EXIT_FILL_DECISION_PRICE, "decision_exit_price")

    def test_output_directory_must_be_empty(self):
        from fill_location_sensitivity import run_fill_location_sensitivity

        with tempfile.TemporaryDirectory() as directory:
            occupied = Path(directory)
            (occupied / "existing.txt").write_text("no", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not empty"):
                run_fill_location_sensitivity(output_dir=occupied)
