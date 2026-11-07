import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from historical_data import (
    EXPECTED_BTCUSDT_1H_2025_COUNT,
    RawKline,
    download_binance_spot_klines,
    historical_kline_interval_label,
    parse_binance_kline_row,
    validate_historical_klines,
    write_dataset,
)
from market_data import BINANCE_KLINE_REST_URL, binance_kline_interval_label


def _row(open_ms: int, close: str = "100", volume: str = "1") -> list:
    interval_ms = 3_600_000
    return [
        open_ms,
        "100",
        "101",
        "99",
        close,
        volume,
        open_ms + interval_ms - 1,
        "1000",
        12,
        "0",
        "0",
        "0",
    ]


class HistoricalDataTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.end = datetime(2025, 1, 1, 3, tzinfo=timezone.utc)
        self.interval = 3600

    def test_historical_1h_does_not_enable_live_runtime_interval(self):
        self.assertEqual(historical_kline_interval_label(3600), "1h")
        with self.assertRaisesRegex(ValueError, "Unsupported Binance"):
            binance_kline_interval_label(3600)

    def test_parse_rejects_zero_volume_and_bad_boundaries(self):
        open_ms = int(self.start.timestamp() * 1000)
        with self.assertRaisesRegex(ValueError, "volume"):
            parse_binance_kline_row(_row(open_ms, volume="0"), interval_seconds=3600)
        bad = _row(open_ms)
        bad[6] = open_ms + 3_600_000
        with self.assertRaisesRegex(ValueError, "interval boundaries"):
            parse_binance_kline_row(bad, interval_seconds=3600)

    def test_validate_requires_continuity_and_range_edges(self):
        opens = [
            int(self.start.timestamp() * 1000),
            int(self.start.timestamp() * 1000) + 3_600_000,
            int(self.start.timestamp() * 1000) + 7_200_000,
        ]
        rows = [
            parse_binance_kline_row(_row(open_ms), interval_seconds=3600)
            for open_ms in opens
        ]
        validated = validate_historical_klines(
            rows,
            interval_seconds=3600,
            start=self.start,
            end_exclusive=self.end,
            expected_count=3,
        )
        self.assertEqual(len(validated), 3)
        gapped = rows[:1] + rows[2:]
        with self.assertRaisesRegex(ValueError, "Missing or extra"):
            validate_historical_klines(
                gapped,
                interval_seconds=3600,
                start=self.start,
                end_exclusive=self.end,
            )

    def test_download_paginates_public_klines_only(self):
        first = int(self.start.timestamp() * 1000)
        payloads = [
            [_row(first), _row(first + 3_600_000)],
            [_row(first + 7_200_000)],
        ]

        def requester(query, *, timeout):
            self.assertEqual(query["symbol"], "BTCUSDT")
            self.assertEqual(query["interval"], "1h")
            self.assertEqual(query["limit"], 1000)
            self.assertIn("startTime", query)
            return payloads.pop(0)

        with patch("historical_data.time.sleep"):
            rows = download_binance_spot_klines(
                start=self.start,
                end_exclusive=self.end,
                interval_seconds=3600,
                pause_seconds=0,
                requester=requester,
            )
        self.assertEqual(len(rows), 3)
        self.assertEqual(payloads, [])

    def test_write_dataset_refuses_overwrite_and_keeps_raw_on_invalid(self):
        open_ms = int(self.start.timestamp() * 1000)
        row = parse_binance_kline_row(_row(open_ms), interval_seconds=3600)
        retrieved = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "dataset"
            write_dataset(
                [row],
                destination,
                interval_seconds=3600,
                start=self.start,
                end_exclusive=self.end,
                expected_count=8760,
                retrieved_at=retrieved,
                source_url=BINANCE_KLINE_REST_URL,
                valid=False,
                error="incomplete",
            )
            manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
            self.assertFalse(manifest["valid"])
            self.assertFalse(manifest["api_keys_used"])
            self.assertEqual(manifest["source_endpoint"], BINANCE_KLINE_REST_URL)
            self.assertFalse((destination / "candles.csv").exists())
            self.assertTrue((destination / "raw_klines.jsonl").exists())
            with self.assertRaises(FileExistsError):
                write_dataset(
                    [row],
                    destination,
                    interval_seconds=3600,
                    start=self.start,
                    end_exclusive=self.end,
                    expected_count=EXPECTED_BTCUSDT_1H_2025_COUNT,
                    retrieved_at=retrieved,
                    source_url=BINANCE_KLINE_REST_URL,
                    valid=True,
                )


if __name__ == "__main__":
    unittest.main()
