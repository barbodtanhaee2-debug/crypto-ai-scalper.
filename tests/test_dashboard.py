import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

from streamlit.testing.v1 import AppTest

from dashboard import (
    DEFAULT_LIVE_REFRESH_SECONDS,
    LIVE_REFRESH_INTERVALS,
)


class DashboardRefreshTests(unittest.TestCase):
    def test_live_refresh_interval_defaults_to_one_second_and_has_expected_options(self):
        dashboard_path = Path(__file__).resolve().parents[1] / "dashboard.py"
        app = AppTest.from_file(dashboard_path).run(timeout=30)

        self.assertFalse(app.exception)
        refresh_selector = next(
            item for item in app.selectbox
            if item.label == "Live data refresh (seconds)"
        )
        self.assertEqual(
            refresh_selector.options,
            [str(value) for value in LIVE_REFRESH_INTERVALS],
        )
        self.assertEqual(refresh_selector.value, DEFAULT_LIVE_REFRESH_SECONDS)
        self.assertEqual(LIVE_REFRESH_INTERVALS, (1, 2, 3, 5, 10, 30))
        self.assertEqual(DEFAULT_LIVE_REFRESH_SECONDS, 1)
        self.assertIn(
            "Binance Spot long-only paper performance",
            [item.value for item in app.header],
        )
        self.assertTrue(any(
            "denominated in USDT" in item.value
            for item in app.caption
        ))
        self.assertTrue(any(
            "Historical candle simulations; results are not live performance."
            in item.value
            for item in app.caption
        ))

    def test_dashboard_shows_exit_pending_and_blocks_permission(self):
        dashboard_path = Path(__file__).resolve().parents[1] / "dashboard.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal_path = root / "paper.jsonl"
            status_path = root / "runtime_status.json"
            now = datetime.now(timezone.utc)
            journal_path.write_text(
                json.dumps({
                    "event": "account_initialized",
                    "timestamp": now.isoformat(),
                    "details": {},
                    "state": {
                        "initial_cash": 100,
                        "cash": 100,
                        "realized_pnl": 0,
                        "position": None,
                        "peak_equity": 100,
                        "day_key": now.date().isoformat(),
                        "day_start_equity": 100,
                        "last_bid": 99.99,
                        "last_ask": 100.01,
                    },
                }) + "\n{incomplete",
                encoding="utf-8",
            )
            status_path.write_text(json.dumps({
                "process_running": True,
                "process_heartbeat": now.isoformat(),
                "connection_status": "connected",
                "market_data_healthy": True,
                "latest_valid_market_data_timestamp": now.isoformat(),
                "market_data_age_seconds": 0.0,
                "market_data_stale_limit_seconds": 3.0,
                "spread": 0.02,
                "entry_permission": True,
                "paper_only": True,
                "risk_lockout": False,
                "open_position": True,
                "journal_ok": True,
                "status_write_ok": True,
                "candle_fresh": True,
                "candle_connection_status": "connected",
                "candle_data_healthy": True,
                "candle_interval": "1m",
                "entry_diagnostics": {
                    "completed_candle_count": 21,
                    "required_completed_candle_count": 21,
                    "candle_history_contiguous": True,
                    "strategy_warmup_ready": True,
                    "warmup_candles_remaining": 0,
                },
                "exit_pending": {
                    "trigger": "stop_loss",
                    "reason": "Awaiting a fresh executable bid.",
                    "awaiting": "fresh quote",
                },
            }), encoding="utf-8")

            app = AppTest.from_file(dashboard_path).run(timeout=30)
            next(
                item for item in app.text_input
                if item.label == "Broker journal"
            ).set_value(str(journal_path))
            next(
                item for item in app.text_input
                if item.label == "Runtime status"
            ).set_value(str(status_path))
            app.run(timeout=30)

            self.assertFalse(app.exception)
            self.assertTrue(any(
                "protective exit is pending" in item.value.lower()
                for item in app.warning
            ))
            self.assertFalse(any(
                "Entry permission" in item.label and item.value == "Yes"
                for item in app.metric
            ))
            self.assertTrue(any(
                "stop_loss" in item.value
                for item in app.json
            ))
            self.assertTrue(any(
                "incomplete or invalid records" in item.value
                for item in app.warning
            ))
            self.assertIn("100.0000", [item.value for item in app.metric])
            self.assertTrue(journal_path.read_text(encoding="utf-8").endswith("{incomplete"))
            self.assertTrue(status_path.read_text(encoding="utf-8").endswith("}"))

    def test_missing_and_malformed_status_render_unavailable(self):
        dashboard_path = Path(__file__).resolve().parents[1] / "dashboard.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing_path = root / "missing.json"
            app = AppTest.from_file(dashboard_path).run(timeout=30)
            next(
                item for item in app.text_input
                if item.label == "Runtime status"
            ).set_value(str(missing_path))
            app.run(timeout=30)
            self.assertTrue(any(
                item.value == "Paper runtime: Status unavailable."
                for item in app.info
            ))

            missing_path.write_text("{invalid", encoding="utf-8")
            app.run(timeout=30)
            self.assertTrue(any(
                item.value == "Paper runtime: Status unavailable."
                for item in app.info
            ))
            self.assertFalse(any(
                "Paper runtime: Healthy" in item.value
                for item in app.success
            ))


if __name__ == "__main__":
    unittest.main()
