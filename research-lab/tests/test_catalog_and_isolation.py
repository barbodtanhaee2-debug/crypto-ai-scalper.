import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import fixtures as lab_fixtures
from quote_tape_dataset import load_quote_tape_session
from research_lab.backtesting.splits import chronological_split
from research_lab.datasets.catalog import _missing_minute_intervals, slice_dataset
from research_lab.isolation import assert_isolated_path


class IsolationTests(unittest.TestCase):
    def test_rejects_live_journal_name(self):
        with self.assertRaises(ValueError):
            assert_isolated_path("paper_broker.jsonl")

    def test_rejects_capture_writes(self):
        with self.assertRaises(ValueError):
            assert_isolated_path(
                Path(r"F:\crypto-ai-scalper\quote-tape\sessions\x\quotes.jsonl")
            )


class DatasetSliceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)

    def test_loader_and_missing_intervals_and_slice(self):
        quotes = [lab_fixtures.quote(self.start, index) for index in range(0, 200, 5)]
        candles = [
            lab_fixtures.candle(self.start, 0, close=100),
            lab_fixtures.candle(self.start, 1, close=101),
            lab_fixtures.candle(self.start, 3, close=102),
        ]
        directory = lab_fixtures.write_session(self.root, quotes, candles)
        dataset = load_quote_tape_session(directory)
        missing = _missing_minute_intervals(dataset)
        self.assertTrue(any("->" in item for item in missing))
        sliced = slice_dataset(
            SimpleNamespace(tape=dataset),
            start=self.start,
            end=self.start + timedelta(minutes=2),
        )
        self.assertEqual(len(sliced.candles), 2)

    def test_chronological_split_is_ordered(self):
        quotes = [lab_fixtures.quote(self.start, index) for index in range(0, 400, 5)]
        candles = [
            lab_fixtures.candle(self.start, minute, close=100 + minute)
            for minute in range(12)
        ]
        dataset = load_quote_tape_session(
            lab_fixtures.write_session(self.root, quotes, candles)
        )
        split = chronological_split(dataset, train_fraction=0.5, validation_fraction=0.25)
        self.assertLess(split.train_start, split.train_end)
        self.assertLessEqual(split.train_end, split.validation_start)
        self.assertLess(split.validation_end, split.test_end)
        self.assertEqual(
            split.train_candles + split.validation_candles + split.test_candles,
            12,
        )


class HashHelperTests(unittest.TestCase):
    def test_session_json_hash_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            path.write_text(json.dumps({"status": "stopped"}), encoding="utf-8")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(len(digest), 64)
