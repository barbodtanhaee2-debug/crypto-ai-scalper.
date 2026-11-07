"""Offline unit tests for Binance Vision historical acquisition (no network)."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from research_lab.historical.checksums import parse_checksum_text, verify_archive
from research_lab.historical.derive import aggregate_1m_to_interval, floor_utc
from research_lab.historical.disk import disk_safety_report, format_bytes
from research_lab.historical.downloader import (
    DownloadResult,
    build_download_plan,
    download_one,
)
from research_lab.historical.ingest_aggtrades import ingest_aggtrade_archive, parse_aggtrade_row
from research_lab.historical.ingest_klines import ingest_kline_archive, parse_kline_row
from research_lab.historical.manifest import DatasetManifest, entry_from_download, write_manifest
from research_lab.historical.planning import plan_archives
from research_lab.historical.timestamps import detect_unit, expected_unit_for_day, to_utc_datetime


def _zip_csv(path: Path, name: str, rows: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(name, "\n".join(rows) + "\n")
    return path


def _checksum_for(path: Path) -> Path:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sidecar = path.with_name(path.name + ".CHECKSUM")
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return sidecar


class FakeResponse:
    def __init__(self, payload: bytes, *, status: int = 200, headers: dict | None = None):
        self._buf = io.BytesIO(payload)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(payload))}

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TimestampTests(unittest.TestCase):
    def test_millisecond_detection_and_utc(self):
        raw = 1_730_678_400_000  # 2024-11-04 00:00:00 UTC
        self.assertEqual(detect_unit(raw), "ms")
        dt = to_utc_datetime(raw)
        self.assertEqual(dt, datetime(2024, 11, 4, tzinfo=timezone.utc))

    def test_microsecond_detection_and_utc(self):
        raw = 1_735_689_600_000_000  # 2025-01-01 00:00:00 UTC
        self.assertEqual(detect_unit(raw), "us")
        dt = to_utc_datetime(raw)
        self.assertEqual(dt, datetime(2025, 1, 1, tzinfo=timezone.utc))

    def test_expected_unit_switch_day(self):
        before = datetime(2024, 12, 31, tzinfo=timezone.utc)
        on = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(expected_unit_for_day(before), "ms")
        self.assertEqual(expected_unit_for_day(on), "us")


class PlanningTests(unittest.TestCase):
    def test_partial_month_uses_daily_full_month_monthly(self):
        start = datetime(2026, 8, 4, tzinfo=timezone.utc)
        end = datetime(2026, 11, 4, tzinfo=timezone.utc)
        specs = plan_archives("aggTrades", start, end)
        granularities = {(item.granularity, item.period) for item in specs}
        self.assertIn(("daily", "2026-08-04"), granularities)
        self.assertIn(("daily", "2026-08-31"), granularities)
        self.assertIn(("monthly", "2026-09"), granularities)
        self.assertIn(("monthly", "2026-10"), granularities)
        self.assertNotIn(("monthly", "2026-08"), granularities)
        # Nov 4 exclusive → daily 2026-11-01..03 only; no monthly November.
        self.assertIn(("daily", "2026-11-01"), granularities)
        self.assertIn(("daily", "2026-11-03"), granularities)
        self.assertNotIn(("daily", "2026-11-04"), granularities)
        self.assertNotIn(("monthly", "2026-11"), granularities)

    def test_klines_boundary_days(self):
        start = datetime(2024, 11, 4, tzinfo=timezone.utc)
        end = datetime(2024, 12, 1, tzinfo=timezone.utc)
        specs = plan_archives("klines_1m", start, end)
        self.assertTrue(all(item.granularity == "daily" for item in specs))
        self.assertEqual(specs[0].period, "2024-11-04")
        self.assertEqual(specs[-1].period, "2024-11-30")


class ChecksumTests(unittest.TestCase):
    def test_parse_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.zip"
            path.write_bytes(b"abc123")
            digest = hashlib.sha256(b"abc123").hexdigest()
            parsed = parse_checksum_text(f"{digest}  sample.zip\n", expected_filename="sample.zip")
            self.assertEqual(parsed, digest)
            self.assertEqual(verify_archive(path, digest), digest)

    def test_corrupt_archive_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.zip"
            path.write_bytes(b"good")
            expected = hashlib.sha256(b"other").hexdigest()
            with self.assertRaises(ValueError):
                verify_archive(path, expected)


class DownloadTests(unittest.TestCase):
    def test_download_one_verifies_and_renames(self):
        payload = b"PK\x03\x04fake-zip-bytes"
        digest = hashlib.sha256(payload).hexdigest()
        checksum_body = f"{digest}  BTCUSDT-1m-2024-11-04.zip\n".encode()

        def opener(request, timeout=0):
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if url.endswith(".CHECKSUM"):
                return FakeResponse(checksum_body)
            return FakeResponse(payload)

        from research_lab.historical.planning import ArchiveSpec

        spec = ArchiveSpec(
            kind="klines_1m",
            granularity="daily",
            period="2024-11-04",
            relative_path="data/spot/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2024-11-04.zip",
            filename="BTCUSDT-1m-2024-11-04.zip",
        )
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            with mock.patch(
                "research_lab.historical.downloader.raw_dir_for",
                return_value=raw,
            ), mock.patch(
                "research_lab.historical.downloader.assert_isolated_path",
                side_effect=lambda path: Path(path),
            ):
                result = download_one(spec, opener=opener, sleep=lambda _: None)
            self.assertEqual(result.status, "downloaded")
            self.assertTrue(Path(result.archive_path).exists())
            self.assertFalse(Path(result.archive_path + ".part").exists())
            self.assertEqual(result.sha256, digest)

    def test_unavailable_404(self):
        def opener(request, timeout=0):
            import urllib.error

            raise urllib.error.HTTPError(
                url="x", code=404, msg="no", hdrs=None, fp=io.BytesIO()
            )

        from research_lab.historical.planning import ArchiveSpec

        spec = ArchiveSpec(
            kind="klines_1m",
            granularity="daily",
            period="2099-01-01",
            relative_path="data/spot/daily/klines/BTCUSDT/1m/x.zip",
            filename="x.zip",
        )
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            with mock.patch(
                "research_lab.historical.downloader.raw_dir_for",
                return_value=raw,
            ), mock.patch(
                "research_lab.historical.downloader.assert_isolated_path",
                side_effect=lambda path: Path(path),
            ):
                result = download_one(spec, opener=opener, sleep=lambda _: None)
            self.assertEqual(result.status, "unavailable")

    def test_interrupted_resume_appends(self):
        full = b"ABCDEFGHIJKLMNOP"
        digest = hashlib.sha256(full).hexdigest()
        checksum_body = f"{digest}  resume.zip\n".encode()
        calls = {"n": 0}

        def opener(request, timeout=0):
            url = getattr(request, "full_url", str(request))
            if url.endswith(".CHECKSUM"):
                return FakeResponse(checksum_body)
            calls["n"] += 1
            headers = getattr(request, "headers", {}) or {}
            # urllib lowercases header keys in some versions; check both.
            range_header = headers.get("Range") or headers.get("range")
            if range_header == "bytes=8-":
                return FakeResponse(full[8:], status=206)
            if calls["n"] == 1:
                # First attempt writes only half then we simulate by returning half.
                return FakeResponse(full[:8])
            return FakeResponse(full)

        from research_lab.historical.planning import ArchiveSpec

        spec = ArchiveSpec(
            kind="klines_1m",
            granularity="daily",
            period="2024-11-05",
            relative_path="data/spot/daily/klines/BTCUSDT/1m/resume.zip",
            filename="resume.zip",
        )
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            raw.mkdir()
            # Pre-seed a partial as if prior attempt died mid-download.
            partial = raw / "resume.zip.part"
            partial.write_bytes(full[:8])
            with mock.patch(
                "research_lab.historical.downloader.raw_dir_for",
                return_value=raw,
            ), mock.patch(
                "research_lab.historical.downloader.assert_isolated_path",
                side_effect=lambda path: Path(path),
            ):
                result = download_one(spec, opener=opener, sleep=lambda _: None)
            self.assertEqual(result.status, "downloaded")
            self.assertEqual(Path(result.archive_path).read_bytes(), full)

    def test_plan_blocks_when_disk_insufficient(self):
        from research_lab.historical.planning import ArchiveSpec

        specs = [
            ArchiveSpec("klines_1m", "daily", "2024-11-04", "rel/a.zip", "a.zip"),
        ]
        plan = build_download_plan(
            specs,
            free_bytes=100,
            reserve_bytes=50,
            size_lookup=lambda _: 200,
        )
        self.assertFalse(plan.safe)


class IngestKlineTests(unittest.TestCase):
    def _row(self, open_ms: int, o=100.0, h=101.0, low=99.0, c=100.5, volume=1.0):
        close_ms = open_ms + 59_999
        return ",".join(
            map(
                str,
                [
                    open_ms,
                    o,
                    h,
                    low,
                    c,
                    volume,
                    close_ms,
                    100.0,
                    10,
                    0.4,
                    40.0,
                    0,
                ],
            )
        )

    def test_parse_ms_and_us_and_ohlc(self):
        ms_row = self._row(1_730_678_400_000).split(",")
        parsed = parse_kline_row(ms_row)
        self.assertEqual(parsed["timestamp_unit"], "ms")
        us_open = 1_735_689_600_000_000
        us_row = self._row(0).split(",")
        us_row[0] = str(us_open)
        us_row[6] = str(us_open + 59_999_000)
        parsed_us = parse_kline_row(us_row)
        self.assertEqual(parsed_us["timestamp_unit"], "us")

    def test_rejects_bad_ohlc_and_duplicates_and_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                self._row(1_730_678_400_000),
                self._row(1_730_678_400_000),  # duplicate
                "1,100,90,99,100,1,59999,1,1,0,0,0",  # bad OHLC high < open
                self._row(1_730_678_520_000),  # +2 minutes → missing one
            ]
            archive = _zip_csv(root / "k.zip", "k.csv", rows)
            out = root / "out.csv"
            with mock.patch(
                "research_lab.historical.ingest_klines.assert_isolated_path",
                side_effect=lambda path: Path(path),
            ):
                report = ingest_kline_archive(archive, output_csv=out)
            self.assertEqual(report.rows_written, 2)
            self.assertGreaterEqual(report.duplicate_open_times, 1)
            self.assertTrue(any("inconsistent" in r["error"] for r in report.rejected))
            self.assertTrue(report.missing_intervals)


class IngestAggTradeTests(unittest.TestCase):
    def test_streaming_rejects_duplicates_and_out_of_order(self):
        rows = [
            "1,100.0,0.1,1,1,1735689600000,true,true",
            "1,100.0,0.1,1,1,1735689600001,true,true",  # dup id
            "3,100.0,0.1,3,3,1735689600000,true,true",  # out of order ts vs prior written
            "2,100.0,0.2,2,2,1735689600002,false,true",
        ]
        # After first row, id=1 ts=...000. Dup id rejected. Then id=3 with earlier/equal ts
        # may be accepted or rejected depending on previous_ts; force earlier:
        rows[2] = "3,100.0,0.1,3,3,1735689599999,true,true"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _zip_csv(root / "a.zip", "a.csv", rows)
            out = root / "out.csv"
            with mock.patch(
                "research_lab.historical.ingest_aggtrades.assert_isolated_path",
                side_effect=lambda path: Path(path),
            ):
                report = ingest_aggtrade_archive(archive, output_csv=out)
            self.assertEqual(report.rows_written, 2)
            self.assertGreaterEqual(report.duplicate_ids, 1)
            self.assertGreaterEqual(report.out_of_order, 1)

    def test_parse_schema(self):
        parsed = parse_aggtrade_row(
            ["10", "65000.5", "0.01", "100", "105", "1735689600000", "false", "true"]
        )
        self.assertEqual(parsed["agg_trade_id"], 10)
        self.assertEqual(parsed["timestamp_unit"], "ms")


class DeriveTests(unittest.TestCase):
    def test_utc_boundary_and_incomplete(self):
        base = datetime(2024, 11, 4, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(floor_utc(base.replace(minute=7), 5), base.replace(minute=5))
        rows = []
        for minute in range(5):
            open_dt = base.replace(minute=minute)
            rows.append(
                {
                    "open_time_utc": open_dt.isoformat(),
                    "open": str(100 + minute),
                    "high": str(101 + minute),
                    "low": str(99 + minute),
                    "close": str(100.5 + minute),
                    "volume": "1",
                    "quote_volume": "100",
                    "trades": "1",
                }
            )
        # Only 5 minutes → one complete 5m bar; next incomplete skipped.
        bars, report = aggregate_1m_to_interval(iter(rows), interval_minutes=5)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].open, 100.0)
        self.assertEqual(bars[0].close, 104.5)
        self.assertEqual(bars[0].volume, 5.0)
        self.assertEqual(report.incomplete_bars, 0)

        # 3 minutes only → incomplete skipped when require_complete.
        short = rows[:3]
        bars2, report2 = aggregate_1m_to_interval(
            iter(short), interval_minutes=5, require_complete=True
        )
        self.assertEqual(bars2, [])
        self.assertEqual(report2.skipped_incomplete, 1)


class ManifestAndDiskTests(unittest.TestCase):
    def test_manifest_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            manifest = DatasetManifest(
                klines_window={"start": "2024-11-04", "end_exclusive": "2026-11-04"},
            )
            manifest.archives.append(
                entry_from_download(
                    kind="klines_1m",
                    granularity="daily",
                    period="2024-11-04",
                    filename="x.zip",
                    relative_path="rel/x.zip",
                    status="downloaded",
                    archive_path=None,
                    checksum_path=None,
                    sha256="abc",
                    content_length=10,
                    error=None,
                    record_count=1440,
                )
            )
            with mock.patch(
                "research_lab.historical.manifest.assert_isolated_path",
                side_effect=lambda path: Path(path),
            ):
                written = write_manifest(manifest, path)
            payload = json.loads(written.read_text(encoding="utf-8"))
            self.assertEqual(payload["totals"]["total_records"], 1440)
            self.assertEqual(payload["provenance"], "BINANCE_SPOT_HISTORICAL")

    def test_disk_safety_and_format(self):
        self.assertIn("GiB", format_bytes(20 * 1024**3))
        report = disk_safety_report(
            estimated_download_bytes=100,
            free=50,
            reserve_bytes=20,
        )
        self.assertFalse(report["safe"])


class MissingCorruptArchiveTests(unittest.TestCase):
    def test_missing_csv_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.zip"
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr("readme.txt", "no csv")
            with mock.patch(
                "research_lab.historical.ingest_klines.assert_isolated_path",
                side_effect=lambda path: Path(path),
            ):
                with self.assertRaises(ValueError):
                    ingest_kline_archive(path, output_csv=Path(tmp) / "out.csv")


if __name__ == "__main__":
    unittest.main()
