import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from streamlit.testing.v1 import AppTest

from historical_replay import PLAYBACK_SPEEDS


class DashboardReplayTests(unittest.TestCase):
    def test_replay_controls_exist_and_speeds_are_fixed(self):
        dashboard_path = Path(__file__).resolve().parents[1] / "dashboard.py"
        app = AppTest.from_file(dashboard_path).run(timeout=30)
        self.assertFalse(app.exception)
        speed = next(item for item in app.selectbox if item.label == "Replay speed")
        self.assertEqual(speed.options, [str(value) for value in PLAYBACK_SPEEDS])
        self.assertTrue(any(item.label == "Play" for item in app.button))
        self.assertTrue(any(item.label == "Pause" for item in app.button))
        self.assertTrue(any(item.label == "Step" for item in app.button))
        self.assertTrue(any(item.label == "Restart" for item in app.button))
        self.assertTrue(
            any(item.label == "Live data refresh (seconds)" for item in app.selectbox)
        )

    def test_replay_load_does_not_write_live_journal(self):
        dashboard_path = Path(__file__).resolve().parents[1] / "dashboard.py"
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live = root / "paper_broker.jsonl"
            live.write_text("live-only\n", encoding="utf-8")
            status = root / "runtime_status.json"
            status.write_text("{}", encoding="utf-8")
            csv_path = root / "candles.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as target:
                writer = csv.DictWriter(
                    target, fieldnames=("timestamp", "open", "high", "low", "close")
                )
                writer.writeheader()
                for index in range(5):
                    stamp = start + timedelta(hours=index)
                    writer.writerow(
                        {
                            "timestamp": stamp.isoformat(),
                            "open": 100,
                            "high": 101,
                            "low": 99,
                            "close": 100,
                        }
                    )
            replay_journal = root / "replay.jsonl"
            app = AppTest.from_file(dashboard_path).run(timeout=30)
            next(item for item in app.text_input if item.label == "Broker journal").set_value(
                str(live)
            )
            next(item for item in app.text_input if item.label == "Runtime status").set_value(
                str(status)
            )
            next(
                item for item in app.text_input if item.label == "Replay candle CSV"
            ).set_value(str(csv_path))
            next(
                item for item in app.text_input if item.label == "Replay journal"
            ).set_value(str(replay_journal))
            app.run(timeout=30)
            next(
                item for item in app.button if item.label == "Load / reset source"
            ).click()
            app.run(timeout=30)
            self.assertFalse(app.exception)
            self.assertEqual(live.read_text(encoding="utf-8"), "live-only\n")
            self.assertTrue(replay_journal.exists())
            next(item for item in app.button if item.label == "Step").click()
            app.run(timeout=30)
            self.assertEqual(live.read_text(encoding="utf-8"), "live-only\n")
            self.assertIn("account_initialized", replay_journal.read_text(encoding="utf-8"))
