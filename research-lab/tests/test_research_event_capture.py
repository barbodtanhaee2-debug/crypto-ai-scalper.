"""Stage 3.2.8 / 3.2.8.2 — bounded research event capture offline tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO

from backtester import BacktestSettings
from strategy import SimulatedPosition, StrategyDecision, StrategySettings

from research_lab.backtesting.historical_replay import HistoricalCandleReplay
from research_lab.datasets.historical_catalog import HistoricalCandle
from research_lab.events.schema import EVENT_TYPES, SCHEMA_VERSION, make_event_id
from research_lab.events.validator import validate_event_stream
from research_lab.events.writer import (
    ResearchEventWriter,
    fingerprint_event_file,
)
from research_lab.historical.constants import PROVENANCE, RESEARCH_CUTOFF
from research_lab.strategies.adapters import adapt_production


def _candle(start: datetime, minute: int, *, o: float, h: float, l: float, c: float) -> HistoricalCandle:
    open_time = start + timedelta(minutes=minute)
    return HistoricalCandle(
        open_time=open_time,
        close_time=open_time + timedelta(milliseconds=59_999),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=1.0,
        quote_volume=100.0,
        trades=10,
        provenance=PROVENANCE,
    )


class _Scripted:
    name = "fixture_scripted"
    settings = StrategySettings()

    def __init__(self, script: list[str]) -> None:
        self._script = list(script)

    def evaluate(self, bars, *, decision_time, market_health, equity, position=None):
        action = self._script.pop(0) if self._script else "no_trade"
        if action == "entry" and position is None:
            ask = float(market_health["ask"])
            return StrategyDecision(
                "entry",
                self.name,
                "long",
                decision_time,
                "fixture entry",
                {},
                SimulatedPosition(
                    self.name, "long", ask, 0.001, ask * 0.99, ask * 1.02, decision_time
                ),
            )
        if action == "exit" and position is not None:
            return StrategyDecision(
                "exit",
                self.name,
                "long",
                decision_time,
                "fixture exit",
                {"exit_price": float(market_health["bid"])},
                None,
            )
        if action == "exit_bad" and position is not None:
            # Missing exit_price → broker reject (used for exit_rejected coverage).
            return StrategyDecision(
                "exit",
                self.name,
                "long",
                decision_time,
                "fixture bad exit",
                {},
                None,
            )
        if action == "entry_tight" and position is None:
            ask = float(market_health["ask"])
            return StrategyDecision(
                "entry",
                self.name,
                "long",
                decision_time,
                "tight band",
                {},
                SimulatedPosition(
                    self.name, "long", ask, 0.001, ask * 0.9999, ask * 1.0001, decision_time
                ),
            )
        return StrategyDecision("no_trade", self.name, None, decision_time, "flat", {}, None)


class ResearchEventWriterTests(unittest.TestCase):
    def test_buffer_flush_and_no_duplicate_on_repeat_flush(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "e"
            root.mkdir(parents=True)
            path = root / "events.jsonl"
            writer = ResearchEventWriter(
                path=path, run_id="r1", arm="B0", buffer_limit=2
            )
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            writer.emit("strategy_decision", event_time=t0, payload={"i": 1})
            self.assertFalse(path.read_text(encoding="utf-8").strip() or False)
            # second emit triggers auto-flush
            writer.emit("strategy_decision", event_time=t0, payload={"i": 2})
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            writer.flush()
            writer.flush()
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)
            writer.emit("run_completed", event_time=t0, payload={"persistence_complete": True})
            writer.close(mark_complete=True)
            self.assertTrue(writer.persistence_complete)
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 3)

    def test_stable_event_ids_and_monotonic_sequence(self):
        a = make_event_id(run_id="r", arm="B0", sequence=1, event_type="entry_signal")
        b = make_event_id(run_id="r", arm="B0", sequence=1, event_type="entry_signal")
        c = make_event_id(run_id="r", arm="B0", sequence=2, event_type="entry_signal")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_refuse_overwrite_and_require_runs_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.jsonl"
            with self.assertRaises(ValueError):
                ResearchEventWriter(path=outside, run_id="r", arm="B0")
            root = Path(tmp) / "experiments" / "runs" / "e"
            root.mkdir(parents=True)
            path = root / "ev.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                ResearchEventWriter(path=path, run_id="r", arm="B0")


class ResearchEventReplayTests(unittest.TestCase):
    def _runs(self, tmp: str, name: str) -> Path:
        root = Path(tmp) / "experiments" / "runs" / "stage328"
        root.mkdir(parents=True, exist_ok=True)
        return root / name

    def test_instrumented_equals_non_instrumented_accounting(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles = [
            _candle(start, i, o=100, h=100.3, l=99.7, c=100.0 + (0.05 if i == 5 else 0.0))
            for i in range(12)
        ]
        # breakout-ish late bars
        candles[5] = _candle(start, 5, o=100.0, h=101.0, l=99.9, c=100.8)
        candles[6] = _candle(start, 6, o=100.8, h=101.0, l=100.6, c=100.9)
        settings = BacktestSettings(candle_interval_seconds=60, initial_equity=10_000.0)
        with tempfile.TemporaryDirectory() as tmp:
            base = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "base.jsonl"),
                strategy_object=adapt_production("momentum_breakout"),
                settings=settings,
                force_close_at_end=False,
                research_event_capture=None,
            ).run()
            events_path = self._runs(tmp, "B0_events.jsonl")
            writer = ResearchEventWriter(
                path=events_path, run_id="cmp1", arm="B0", buffer_limit=8
            )
            inst = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "inst.jsonl"),
                strategy_object=adapt_production("momentum_breakout"),
                settings=settings,
                force_close_at_end=False,
                research_event_capture=writer,
            ).run()
        for key in (
            "trades",
            "cash",
            "equity",
            "decision_counts",
            "entries_accepted",
            "entries_rejected",
            "fills_cancelled_incompatible",
            "fills_cancelled_missing_next",
            "trade_count",
        ):
            self.assertEqual(base.get(key), inst.get(key), key)
        self.assertTrue(inst["research_event_capture"]["persistence_complete"])

    def test_entry_lifecycle_and_validator(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles = [_candle(start, i, o=100, h=100.2, l=99.8, c=100.0) for i in range(6)]
        candles.append(_candle(start, 6, o=100.0, h=100.2, l=99.8, c=100.0))
        script = ["no_trade"] * 3 + ["entry", "exit", "no_trade", "no_trade"]
        with tempfile.TemporaryDirectory() as tmp:
            ev = self._runs(tmp, "B0_events.jsonl")
            writer = ResearchEventWriter(path=ev, run_id="life1", arm="B0", buffer_limit=4)
            result = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "life.jsonl"),
                strategy_object=_Scripted(script),
                settings=BacktestSettings(candle_interval_seconds=60),
                force_close_at_end=False,
                research_event_capture=writer,
            ).run()
            report = validate_event_stream(ev)
            self.assertTrue(report.ok, report.errors)
            types = [json.loads(line)["event_type"] for line in ev.read_text(encoding="utf-8").splitlines()]
            self.assertIn("strategy_decision", types)
            self.assertIn("entry_signal", types)
            self.assertIn("entry_pending", types)
            self.assertIn("entry_compatibility_check", types)
            self.assertIn("run_completed", types)
            self.assertTrue(result["research_event_capture"]["persistence_complete"])

    def test_incompatible_cancellation_event(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        # Signal on candle 2 close; fill attempted at candle 3 open (far away).
        candles = [
            _candle(start, 0, o=100, h=100.2, l=99.8, c=100.0),
            _candle(start, 1, o=100, h=100.2, l=99.8, c=100.0),
            _candle(start, 2, o=100, h=100.2, l=99.8, c=100.0),
            _candle(start, 3, o=120.0, h=120.5, l=119.5, c=120.0),
            _candle(start, 4, o=120.0, h=120.2, l=119.8, c=120.0),
        ]
        script = ["no_trade", "no_trade", "entry_tight", "no_trade", "no_trade"]
        with tempfile.TemporaryDirectory() as tmp:
            ev = self._runs(tmp, "B0_events.jsonl")
            writer = ResearchEventWriter(path=ev, run_id="inc1", arm="B0", buffer_limit=16)
            result = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "inc.jsonl"),
                strategy_object=_Scripted(script),
                settings=BacktestSettings(candle_interval_seconds=60),
                research_event_capture=writer,
            ).run()
            self.assertGreaterEqual(result["fills_cancelled_incompatible"], 1)
            types = [json.loads(line)["event_type"] for line in ev.read_text(encoding="utf-8").splitlines()]
            self.assertIn("entry_cancelled_incompatible", types)
            self.assertIn("entry_compatibility_check", types)
            report = validate_event_stream(ev)
            self.assertTrue(report.ok, report.errors)

    def test_b0_h1_separate_streams_and_determinism(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles = [_candle(start, i, o=100, h=100.2, l=99.8, c=100.0) for i in range(8)]
        settings = BacktestSettings(candle_interval_seconds=60)
        with tempfile.TemporaryDirectory() as tmp:
            fps = []
            for arm, replica in (("B0", "a"), ("B0", "b"), ("H1", "a"), ("H1", "b")):
                ev = self._runs(tmp, f"{arm}_{replica}_events.jsonl")
                writer = ResearchEventWriter(
                    path=ev, run_id="det1", arm=arm, buffer_limit=32
                )
                strategy = (
                    adapt_production("momentum_breakout")
                    if arm == "B0"
                    else adapt_production("momentum_breakout")
                )
                # H1 arm label only for stream separation in this fixture
                HistoricalCandleReplay(
                    candles=candles,
                    journal_path=self._runs(tmp, f"{arm}_{replica}.jsonl"),
                    strategy_object=strategy,
                    settings=settings,
                    research_event_capture=writer,
                ).run()
                fps.append(fingerprint_event_file(ev))
                self.assertTrue(validate_event_stream(ev).ok)
            self.assertEqual(fps[0], fps[1])
            self.assertEqual(fps[2], fps[3])
            # Different arm labels => different event_id namespace / fingerprint
            self.assertNotEqual(fps[0], fps[2])

    def test_missing_sequence_and_duplicate_id_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "x"
            root.mkdir(parents=True)
            path = root / "bad.jsonl"
            good = {
                "schema_version": SCHEMA_VERSION,
                "run_id": "r",
                "arm": "B0",
                "event_id": "id-1",
                "sequence": 1,
                "event_type": "strategy_decision",
                "event_time": "2026-10-30T12:00:00+00:00",
                "candle_open_time": None,
                "signal_time": None,
                "signal_price": None,
                "symbol": "BTCUSDT",
                "provenance": PROVENANCE,
                "payload": {},
            }
            skip = dict(good, sequence=3, event_id="id-3")
            path.write_text(
                json.dumps(good) + "\n" + json.dumps(skip) + "\n", encoding="utf-8"
            )
            report = validate_event_stream(path)
            self.assertFalse(report.ok)
            self.assertTrue(any("sequence" in e for e in report.errors))

            dup = dict(good)
            path.write_text(
                json.dumps(good) + "\n" + json.dumps(dup) + "\n", encoding="utf-8"
            )
            report2 = validate_event_stream(path)
            self.assertFalse(report2.ok)
            self.assertTrue(any("duplicate" in e for e in report2.errors))

    def test_invalid_schema_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "x"
            root.mkdir(parents=True)
            path = root / "bad.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "0.0.0",
                        "run_id": "r",
                        "arm": "B0",
                        "event_id": "x",
                        "sequence": 1,
                        "event_type": "not_a_real_type",
                        "event_time": None,
                        "candle_open_time": None,
                        "signal_time": None,
                        "signal_price": None,
                        "symbol": "BTCUSDT",
                        "provenance": PROVENANCE,
                        "payload": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            report = validate_event_stream(path)
            self.assertFalse(report.ok)

    def test_event_type_vocabulary_complete(self):
        required = {
            "strategy_decision",
            "entry_signal",
            "entry_pending",
            "entry_compatibility_check",
            "entry_cancelled_incompatible",
            "entry_cancelled_missing_candle",
            "entry_rejected",
            "entry_filled",
            "position_exit_signal",
            "exit_rejected",
            "exit_cancelled_missing_candle",
            "position_closed",
            "risk_limit_rejected",
            "run_completed",
            "run_failed",
        }
        self.assertTrue(required.issubset(EVENT_TYPES))
        self.assertEqual(SCHEMA_VERSION, "1.1.0")

    def test_exception_path_emits_run_failed(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        # Include a candle after cutoff to force failure after some work.
        candles = [
            _candle(start, 0, o=100, h=100.2, l=99.8, c=100.0),
            HistoricalCandle(
                open_time=RESEARCH_CUTOFF,
                close_time=RESEARCH_CUTOFF + timedelta(milliseconds=59_999),
                open=100.0,
                high=100.1,
                low=99.9,
                close=100.0,
                volume=1.0,
                quote_volume=100.0,
                trades=1,
                provenance=PROVENANCE,
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ev = self._runs(tmp, "B0_events.jsonl")
            writer = ResearchEventWriter(path=ev, run_id="fail1", arm="B0", buffer_limit=2)
            with self.assertRaises(ValueError):
                HistoricalCandleReplay(
                    candles=candles,
                    journal_path=self._runs(tmp, "fail.jsonl"),
                    strategy_object=_Scripted(["no_trade", "no_trade"]),
                    settings=BacktestSettings(candle_interval_seconds=60),
                    research_event_capture=writer,
                ).run()
            types = [
                json.loads(line)["event_type"]
                for line in ev.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("run_failed", types)
            self.assertNotIn("run_completed", types)


class SmallFixtureBenchmarkTests(unittest.TestCase):
    def test_small_fixture_event_write_budget(self):
        import time

        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        n = 500
        candles = [_candle(start, i, o=100, h=100.2, l=99.8, c=100.0) for i in range(n)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "bench"
            root.mkdir(parents=True)
            writer = ResearchEventWriter(
                path=root / "B0_events.jsonl",
                run_id="bench",
                arm="B0",
                buffer_limit=256,
            )
            t0 = time.perf_counter()
            HistoricalCandleReplay(
                candles=candles,
                journal_path=root / "j.jsonl",
                strategy_object=_Scripted(["no_trade"] * n),
                settings=BacktestSettings(candle_interval_seconds=60),
                research_event_capture=writer,
            ).run()
            elapsed = time.perf_counter() - t0
        # Loose budget: instrumentation must remain practical on small fixtures.
        self.assertLess(elapsed, 15.0)
        self.assertGreater(n / max(elapsed, 1e-9), 20.0)


class FaultInjectionFlushTests(unittest.TestCase):
    """Stage 3.2.8.2 — partial-write safety via fault_injector."""

    def _writer(self, root: Path, **kwargs) -> ResearchEventWriter:
        return ResearchEventWriter(
            path=root / "events.jsonl",
            run_id="fault",
            arm="B0",
            buffer_limit=100,
            **kwargs,
        )

    def test_failure_before_any_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f"
            root.mkdir(parents=True)

            def boom(payload: str, handle: TextIO) -> None:
                raise OSError("simulated pre-write failure")

            writer = self._writer(root, fault_injector=boom)
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            writer.emit("strategy_decision", event_time=t0, payload={"i": 1})
            with self.assertRaises(OSError):
                writer.flush()
            self.assertEqual(writer.path.read_bytes(), b"")
            self.assertEqual(len(writer._buffer), 1)
            self.assertFalse(writer.persistence_uncertain)
            # Safe retry after truncate recovery (nothing was written).
            writer.fault_injector = None
            writer.flush()
            self.assertEqual(len(writer.path.read_text(encoding="utf-8").splitlines()), 1)
            writer.close(mark_complete=True)
            self.assertTrue(writer.persistence_complete)

    def test_failure_after_complete_jsonl_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f"
            root.mkdir(parents=True)
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            calls = {"n": 0}

            def after_full_lines(payload: str, handle: TextIO) -> None:
                calls["n"] += 1
                if calls["n"] == 1:
                    # Write both complete lines then fail before flush returns.
                    handle.write(payload)
                    raise OSError("simulated post-line failure")
                handle.write(payload)

            writer = self._writer(root, fault_injector=after_full_lines)
            writer.emit("strategy_decision", event_time=t0, payload={"i": 1})
            writer.emit("strategy_decision", event_time=t0, payload={"i": 2})
            with self.assertRaises(OSError):
                writer.flush()
            # Truncate recovered → file empty, buffer retained (no duplicate risk).
            self.assertEqual(writer.path.read_bytes(), b"")
            self.assertEqual(len(writer._buffer), 2)
            writer.flush()
            lines = writer.path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            seqs = [json.loads(line)["sequence"] for line in lines]
            self.assertEqual(seqs, [1, 2])
            writer.close()

    def test_failure_partway_through_jsonl_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f"
            root.mkdir(parents=True)
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            calls = {"n": 0}

            def mid_line(payload: str, handle: TextIO) -> None:
                calls["n"] += 1
                if calls["n"] == 1:
                    handle.write(payload[: max(1, len(payload) // 3)])
                    raise OSError("simulated mid-line failure")
                handle.write(payload)

            writer = self._writer(root, fault_injector=mid_line)
            writer.emit("strategy_decision", event_time=t0, payload={"i": 1})
            with self.assertRaises(OSError):
                writer.flush()
            self.assertEqual(writer.path.read_bytes(), b"")
            self.assertFalse(writer.persistence_uncertain)
            writer.flush()
            line = writer.path.read_text(encoding="utf-8").splitlines()[0]
            json.loads(line)  # complete JSONL
            writer.close()

    def test_retry_after_partial_write_no_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f"
            root.mkdir(parents=True)
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            calls = {"n": 0}

            def partial_then_ok(payload: str, handle: TextIO) -> None:
                calls["n"] += 1
                if calls["n"] == 1:
                    handle.write(payload[:10])
                    raise OSError("partial")
                handle.write(payload)

            writer = self._writer(root, fault_injector=partial_then_ok)
            for i in range(3):
                writer.emit("strategy_decision", event_time=t0, payload={"i": i})
            with self.assertRaises(OSError):
                writer.flush()
            writer.flush()
            lines = writer.path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(
                [json.loads(line)["sequence"] for line in lines], [1, 2, 3]
            )
            ids = [json.loads(line)["event_id"] for line in lines]
            self.assertEqual(len(ids), len(set(ids)))
            writer.close(mark_complete=True)
            self.assertTrue(writer.persistence_complete)

    def test_repeated_flush_and_close_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f"
            root.mkdir(parents=True)
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            writer = self._writer(root)
            writer.emit("strategy_decision", event_time=t0, payload={})
            writer.flush()
            writer.flush()
            writer.flush()
            writer.close(mark_complete=True)
            writer.close(mark_complete=True)
            writer.close()
            self.assertEqual(len(writer.path.read_text(encoding="utf-8").splitlines()), 1)
            self.assertTrue(writer.persistence_complete)

    def test_failure_during_final_completion_recoverable_keeps_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f"
            root.mkdir(parents=True)
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)

            def always_fail(payload: str, handle: TextIO) -> None:
                raise OSError("final completion blocked")

            writer = self._writer(root, fault_injector=always_fail)
            writer.emit(
                "run_completed",
                event_time=t0,
                payload={"persistence_complete": True},
            )
            with self.assertRaises(OSError):
                writer.close(mark_complete=True)
            self.assertFalse(writer.persistence_complete)
            # Recoverable truncate: handle stays open for retry (F-04).
            self.assertFalse(writer.closed)
            self.assertTrue(writer.retry_supported)
            self.assertGreater(writer.buffer_len, 0)
            self.assertEqual(writer.path.read_bytes(), b"")
            writer.fault_injector = None
            writer.close(mark_complete=True)
            self.assertTrue(writer.persistence_complete)
            self.assertTrue(writer.closed)


class TruncateFailureFaultTests(unittest.TestCase):
    """Stage 3.2.8.4 F-02 — tell/seek/truncate failure fail-closed paths."""

    def _writer(self, root: Path, **kwargs) -> ResearchEventWriter:
        return ResearchEventWriter(
            path=root / "events.jsonl",
            run_id="f02",
            arm="B0",
            buffer_limit=100,
            **kwargs,
        )

    def test_tell_failure_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f02"
            root.mkdir(parents=True)

            def boom_tell(stage: str) -> None:
                if stage == "tell":
                    raise OSError("tell failed")

            writer = self._writer(root, io_stage_fault=boom_tell)
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            writer.emit("strategy_decision", event_time=t0, payload={"i": 1})
            with self.assertRaises(OSError):
                writer.flush()
            self.assertTrue(writer.persistence_uncertain)
            self.assertEqual(writer.buffer_len, 1)
            with self.assertRaises(RuntimeError):
                writer.emit("strategy_decision", event_time=t0, payload={"i": 2})
            with self.assertRaises(RuntimeError):
                writer.flush()
            writer.close(mark_complete=True)
            self.assertTrue(writer.closed)
            self.assertFalse(writer.persistence_complete)
            self.assertEqual(writer.buffer_len, 1)  # not discarded
            report = validate_event_stream(writer.path)
            self.assertFalse(report.ok)
            self.assertFalse(report.has_run_completed)

    def test_seek_failure_during_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f02"
            root.mkdir(parents=True)

            def boom_seek(stage: str) -> None:
                if stage == "seek":
                    raise OSError("seek failed")

            def write_partial(payload: str, handle: TextIO) -> None:
                handle.write(payload[:8])
                raise OSError("partial write")

            writer = self._writer(
                root, fault_injector=write_partial, io_stage_fault=boom_seek
            )
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            writer.emit("strategy_decision", event_time=t0, payload={"i": 1})
            with self.assertRaises(OSError):
                writer.flush()
            self.assertTrue(writer.persistence_uncertain)
            self.assertEqual(writer.buffer_len, 1)
            writer.close(mark_complete=True)
            self.assertFalse(writer.persistence_complete)
            self.assertFalse(writer.retry_supported)

    def test_truncate_failure_during_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f02"
            root.mkdir(parents=True)

            def boom_truncate(stage: str) -> None:
                if stage == "truncate":
                    raise OSError("truncate failed")

            def write_partial(payload: str, handle: TextIO) -> None:
                handle.write(payload[:12])
                raise OSError("partial write")

            writer = self._writer(
                root, fault_injector=write_partial, io_stage_fault=boom_truncate
            )
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            writer.emit("strategy_decision", event_time=t0, payload={"i": 1})
            with self.assertRaises(OSError):
                writer.flush()
            self.assertTrue(writer.persistence_uncertain)
            self.assertGreaterEqual(writer.buffer_len, 1)
            with self.assertRaises(RuntimeError):
                writer.flush()
            writer.close(mark_complete=True)
            self.assertTrue(writer.closed)
            self.assertFalse(writer.persistence_complete)
            report = validate_event_stream(writer.path)
            self.assertFalse(report.has_run_completed)

    def test_partial_write_then_failed_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f02"
            root.mkdir(parents=True)

            def boom_truncate(stage: str) -> None:
                if stage == "truncate":
                    raise OSError("truncate failed after partial")

            def write_partial(payload: str, handle: TextIO) -> None:
                handle.write(payload[: max(1, len(payload) // 2)])
                raise OSError("partial")

            writer = self._writer(
                root, fault_injector=write_partial, io_stage_fault=boom_truncate
            )
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            writer.emit("strategy_decision", event_time=t0, payload={"i": 1})
            writer.emit("strategy_decision", event_time=t0, payload={"i": 2})
            with self.assertRaises(OSError):
                writer.flush()
            self.assertTrue(writer.persistence_uncertain)
            buffered = writer.buffer_len
            self.assertEqual(buffered, 2)
            with self.assertRaises(RuntimeError):
                writer.emit("run_completed", event_time=t0, payload={"persistence_complete": True})
            writer.close(mark_complete=True)
            self.assertEqual(writer.buffer_len, buffered)
            self.assertFalse(writer.persistence_complete)

    def test_repeated_ops_after_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f02"
            root.mkdir(parents=True)

            def boom_tell(stage: str) -> None:
                if stage == "tell":
                    raise OSError("tell failed")

            writer = self._writer(root, io_stage_fault=boom_tell)
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            writer.emit("strategy_decision", event_time=t0, payload={})
            with self.assertRaises(OSError):
                writer.flush()
            self.assertTrue(writer.persistence_uncertain)
            for _ in range(3):
                with self.assertRaises(RuntimeError):
                    writer.emit("strategy_decision", event_time=t0, payload={})
                with self.assertRaises(RuntimeError):
                    writer.flush()
            writer.close(mark_complete=True)
            writer.close(mark_complete=True)
            self.assertFalse(writer.persistence_complete)

    def test_completion_refused_after_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f02"
            root.mkdir(parents=True)

            def boom_tell(stage: str) -> None:
                if stage == "tell":
                    raise OSError("tell failed")

            writer = self._writer(root, io_stage_fault=boom_tell)
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            writer.emit("strategy_decision", event_time=t0, payload={})
            with self.assertRaises(OSError):
                writer.flush()
            writer.close(mark_complete=True)
            self.assertFalse(writer.persistence_complete)
            self.assertTrue(writer.persistence_uncertain)
            report = validate_event_stream(writer.path)
            self.assertFalse(report.ok or report.has_run_completed)


class CloseRetryLifecycleTests(unittest.TestCase):
    """Stage 3.2.8.4 F-04 — close-time orphaning / retry policy."""

    def _writer(self, root: Path, **kwargs) -> ResearchEventWriter:
        return ResearchEventWriter(
            path=root / "events.jsonl",
            run_id="f04",
            arm="B0",
            buffer_limit=100,
            **kwargs,
        )

    def test_close_after_successful_flush(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f04"
            root.mkdir(parents=True)
            writer = self._writer(root)
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            writer.emit("strategy_decision", event_time=t0, payload={})
            writer.flush()
            writer.close(mark_complete=True)
            self.assertTrue(writer.persistence_complete)
            self.assertTrue(writer.closed)
            self.assertEqual(writer.closed_reason, "normal")
            self.assertEqual(writer.buffer_len, 0)

    def test_close_after_recoverable_flush_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f04"
            root.mkdir(parents=True)
            calls = {"n": 0}

            def fail_once(payload: str, handle: TextIO) -> None:
                calls["n"] += 1
                if calls["n"] == 1:
                    handle.write(payload[:5])
                    raise OSError("recoverable")
                handle.write(payload)

            writer = self._writer(root, fault_injector=fail_once)
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            writer.emit("strategy_decision", event_time=t0, payload={"i": 1})
            with self.assertRaises(OSError):
                writer.close(mark_complete=True)
            self.assertFalse(writer.closed)
            self.assertTrue(writer.retry_supported)
            self.assertGreater(writer.buffer_len, 0)
            self.assertFalse(writer.persistence_complete)
            # Retry supported: subsequent close succeeds.
            writer.close(mark_complete=True)
            self.assertTrue(writer.closed)
            self.assertTrue(writer.persistence_complete)
            self.assertEqual(len(writer.path.read_text(encoding="utf-8").splitlines()), 1)

    def test_close_after_unrecoverable_flush_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f04"
            root.mkdir(parents=True)

            def boom_truncate(stage: str) -> None:
                if stage == "truncate":
                    raise OSError("truncate failed")

            def write_partial(payload: str, handle: TextIO) -> None:
                handle.write(payload[:6])
                raise OSError("partial")

            writer = self._writer(
                root, fault_injector=write_partial, io_stage_fault=boom_truncate
            )
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            writer.emit("strategy_decision", event_time=t0, payload={})
            with self.assertRaises(OSError):
                writer.close(mark_complete=True)
            self.assertTrue(writer.closed)
            self.assertEqual(writer.closed_reason, "unrecoverable_flush")
            self.assertTrue(writer.persistence_uncertain)
            self.assertFalse(writer.retry_supported)
            self.assertGreater(writer.buffer_len, 0)  # retained, not discarded
            self.assertFalse(writer.persistence_complete)
            with self.assertRaises(RuntimeError):
                writer.flush()

    def test_repeated_close_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f04"
            root.mkdir(parents=True)
            writer = self._writer(root)
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            writer.emit("strategy_decision", event_time=t0, payload={})
            writer.close(mark_complete=True)
            writer.close(mark_complete=True)
            writer.close()
            self.assertTrue(writer.persistence_complete)

    def test_abandon_seals_without_flush(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "experiments" / "runs" / "f04"
            root.mkdir(parents=True)
            writer = self._writer(root)
            t0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
            writer.emit("strategy_decision", event_time=t0, payload={})
            writer.abandon()
            self.assertTrue(writer.closed)
            self.assertEqual(writer.closed_reason, "abandoned")
            self.assertEqual(writer.buffer_len, 1)
            self.assertFalse(writer.persistence_complete)
            self.assertEqual(writer.path.read_bytes(), b"")


class CompletionMarkerTests(unittest.TestCase):
    """Stage 3.2.8.4 F-09 — run_completed durability before seal."""

    def _runs(self, tmp: str, name: str) -> Path:
        root = Path(tmp) / "experiments" / "runs" / "f09"
        root.mkdir(parents=True, exist_ok=True)
        return root / name

    def test_successful_completion_flushes_marker_before_seal(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles = [_candle(start, i, o=100, h=100.2, l=99.8, c=100.0) for i in range(4)]
        with tempfile.TemporaryDirectory() as tmp:
            ev = self._runs(tmp, "B0_events.jsonl")
            writer = ResearchEventWriter(path=ev, run_id="f09ok", arm="B0", buffer_limit=64)
            result = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "j.jsonl"),
                strategy_object=_Scripted(["no_trade"] * 4),
                settings=BacktestSettings(candle_interval_seconds=60),
                research_event_capture=writer,
            ).run()
            self.assertTrue(result["research_event_capture"]["persistence_complete"])
            self.assertTrue(writer.closed)
            report = validate_event_stream(ev)
            self.assertTrue(report.ok, report.errors)
            self.assertTrue(report.has_run_completed)
            types = [
                json.loads(line)["event_type"]
                for line in ev.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(types[-1], "run_completed")
            payload = json.loads(ev.read_text(encoding="utf-8").splitlines()[-1])["payload"]
            self.assertIs(payload["persistence_complete"], True)

    def test_failed_final_persistence_refuses_complete_run(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles = [_candle(start, i, o=100, h=100.2, l=99.8, c=100.0) for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            ev = self._runs(tmp, "B0_events.jsonl")
            calls = {"n": 0}

            def fail_terminal(payload: str, handle: TextIO) -> None:
                # Allow early auto-flushes; fail when run_completed is in the payload.
                if "run_completed" in payload:
                    calls["n"] += 1
                    handle.write(payload[:10])
                    raise OSError("terminal flush blocked")
                handle.write(payload)

            writer = ResearchEventWriter(
                path=ev,
                run_id="f09fail",
                arm="B0",
                buffer_limit=1000,
                fault_injector=fail_terminal,
            )
            with self.assertRaises((OSError, RuntimeError)):
                HistoricalCandleReplay(
                    candles=candles,
                    journal_path=self._runs(tmp, "j.jsonl"),
                    strategy_object=_Scripted(["no_trade"] * 3),
                    settings=BacktestSettings(candle_interval_seconds=60),
                    research_event_capture=writer,
                ).run()
            self.assertFalse(writer.persistence_complete)
            self.assertTrue(writer.closed)
            report = validate_event_stream(ev)
            # Must not accept as a complete durable stream.
            self.assertFalse(report.has_run_completed and report.ok)


class LifecycleValidationTests(unittest.TestCase):
    def _write(self, path: Path, events: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(e, separators=(",", ":"), sort_keys=True) for e in events)
            + "\n",
            encoding="utf-8",
        )

    def _base(self, sequence: int, event_type: str, **payload_extra) -> dict:
        run_id, arm = "life", "B0"
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "arm": arm,
            "event_id": make_event_id(
                run_id=run_id, arm=arm, sequence=sequence, event_type=event_type
            ),
            "sequence": sequence,
            "event_type": event_type,
            "event_time": "2026-10-30T12:00:00+00:00",
            "candle_open_time": None,
            "signal_time": None,
            "signal_price": None,
            "symbol": "BTCUSDT",
            "provenance": PROVENANCE,
            "payload": payload_extra,
        }

    def test_event_id_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "experiments" / "runs" / "x" / "bad.jsonl"
            ev = self._base(1, "strategy_decision")
            ev["event_id"] = "tampered"
            self._write(path, [ev])
            report = validate_event_stream(path)
            self.assertFalse(report.ok)
            self.assertTrue(any("event_id mismatch" in e for e in report.errors))

    def test_fill_after_cancel_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "experiments" / "runs" / "x" / "bad.jsonl"
            lid = "B0:entry:1"
            events = [
                self._base(1, "entry_pending", lifecycle_id=lid),
                self._base(2, "entry_cancelled_incompatible", lifecycle_id=lid),
                self._base(3, "entry_filled", lifecycle_id=lid),
            ]
            self._write(path, events)
            report = validate_event_stream(path)
            self.assertFalse(report.ok)
            self.assertTrue(any("fill-after-cancel" in e for e in report.errors))

    def test_close_before_open_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "experiments" / "runs" / "x" / "bad.jsonl"
            events = [self._base(1, "position_closed", lifecycle_id="x")]
            self._write(path, events)
            report = validate_event_stream(path)
            self.assertFalse(report.ok)
            self.assertTrue(any("close-before-open" in e for e in report.errors))

    def test_missing_lifecycle_id_is_error_not_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "experiments" / "runs" / "x" / "bad.jsonl"
            events = [self._base(1, "entry_pending")]  # no lifecycle_id
            self._write(path, events)
            report = validate_event_stream(path)
            self.assertFalse(report.ok)
            self.assertTrue(any("missing required lifecycle_id" in e for e in report.errors))

    def test_unavailable_null_lifecycle_on_decision_is_not_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "experiments" / "runs" / "x" / "ok.jsonl"
            ev = self._base(1, "strategy_decision", lifecycle_id=None)
            events = [
                ev,
                self._base(2, "run_completed", persistence_complete=True),
            ]
            self._write(path, events)
            report = validate_event_stream(path)
            self.assertTrue(report.ok, report.errors)
            self.assertTrue(report.unavailable_evidence)


class ExitEventCoverageTests(unittest.TestCase):
    def _runs(self, tmp: str, name: str) -> Path:
        root = Path(tmp) / "experiments" / "runs" / "stage3282"
        root.mkdir(parents=True, exist_ok=True)
        return root / name

    def test_exit_rejected_event(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles = [_candle(start, i, o=100, h=100.2, l=99.8, c=100.0) for i in range(8)]
        # warm → entry → hold → bad exit → hold
        script = ["no_trade", "no_trade", "entry", "exit_bad", "no_trade", "no_trade", "no_trade", "no_trade"]
        with tempfile.TemporaryDirectory() as tmp:
            ev = self._runs(tmp, "B0_events.jsonl")
            writer = ResearchEventWriter(path=ev, run_id="exrej", arm="B0", buffer_limit=16)
            result = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "exrej.jsonl"),
                strategy_object=_Scripted(script),
                settings=BacktestSettings(candle_interval_seconds=60),
                force_close_at_end=False,
                research_event_capture=writer,
            ).run()
            types = [
                json.loads(line)["event_type"]
                for line in ev.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("position_exit_signal", types)
            self.assertIn("exit_rejected", types)
            self.assertTrue(validate_event_stream(ev).ok, validate_event_stream(ev).errors)
            self.assertTrue(result["research_event_capture"]["persistence_complete"])

    def test_exit_cancelled_missing_candle(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles = [
            _candle(start, 0, o=100, h=100.2, l=99.8, c=100.0),
            _candle(start, 1, o=100, h=100.2, l=99.8, c=100.0),
            _candle(start, 2, o=100, h=100.2, l=99.8, c=100.0),
            _candle(start, 3, o=100, h=100.2, l=99.8, c=100.0),
            # Gap after exit signal on candle 3 close → cancel at candle 10 open
            _candle(start, 10, o=100, h=100.2, l=99.8, c=100.0),
        ]
        script = ["no_trade", "no_trade", "entry", "exit", "no_trade"]
        with tempfile.TemporaryDirectory() as tmp:
            ev = self._runs(tmp, "B0_events.jsonl")
            writer = ResearchEventWriter(path=ev, run_id="exgap", arm="B0", buffer_limit=16)
            result = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "exgap.jsonl"),
                strategy_object=_Scripted(script),
                settings=BacktestSettings(candle_interval_seconds=60),
                force_close_at_end=False,
                research_event_capture=writer,
            ).run()
            self.assertGreaterEqual(result["fills_cancelled_missing_next"], 1)
            types = [
                json.loads(line)["event_type"]
                for line in ev.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("exit_cancelled_missing_candle", types)
            self.assertTrue(validate_event_stream(ev).ok, validate_event_stream(ev).errors)


class ExpandedEquivalenceTests(unittest.TestCase):
    """Capture-on vs capture-off behavioral equivalence (synthetic only)."""

    def _runs(self, tmp: str, name: str) -> Path:
        root = Path(tmp) / "experiments" / "runs" / "equiv"
        root.mkdir(parents=True, exist_ok=True)
        return root / name

    def _compare(self, base: dict, inst: dict) -> None:
        for key in (
            "trades",
            "cash",
            "equity",
            "decision_counts",
            "rejection_reasons",
            "entries_accepted",
            "entries_rejected",
            "fills_cancelled_incompatible",
            "fills_cancelled_missing_next",
            "fills_deferred",
            "fills_executed",
            "trade_count",
            "missing_intervals",
            "open_position",
            "unresolved_positions",
        ):
            self.assertEqual(base.get(key), inst.get(key), key)
        # Trade ordering and cost fields
        for b_trade, i_trade in zip(base["trades"], inst["trades"]):
            for field in (
                "entry_timestamp",
                "exit_timestamp",
                "entry_fill_price",
                "exit_fill_price",
                "gross_pnl",
                "net_realized_pnl",
                "total_costs",
                "fees",
                "slippage",
                "direction",
                "reason",
            ):
                if field in b_trade or field in i_trade:
                    self.assertEqual(b_trade.get(field), i_trade.get(field), field)

    def test_scripted_roundtrip_equivalence(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles = [_candle(start, i, o=100, h=100.3, l=99.7, c=100.0) for i in range(10)]
        script = ["no_trade", "no_trade", "entry", "no_trade", "exit", "no_trade"] + [
            "no_trade"
        ] * 4
        settings = BacktestSettings(candle_interval_seconds=60, initial_equity=10_000.0)
        with tempfile.TemporaryDirectory() as tmp:
            base = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "base.jsonl"),
                strategy_object=_Scripted(list(script)),
                settings=settings,
                force_close_at_end=False,
                research_event_capture=None,
            ).run()
            writer = ResearchEventWriter(
                path=self._runs(tmp, "B0_events.jsonl"),
                run_id="eq1",
                arm="B0",
                buffer_limit=4,
            )
            inst = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "inst.jsonl"),
                strategy_object=_Scripted(list(script)),
                settings=settings,
                force_close_at_end=False,
                research_event_capture=writer,
            ).run()
        self._compare(base, inst)
        self.assertGreaterEqual(base["trade_count"], 1)

    def test_missing_candle_equivalence(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        # Entry on candle 1 close; gap before candle 8 cancels the pending fill.
        candles = [
            _candle(start, 0, o=100, h=100.2, l=99.8, c=100.0),
            _candle(start, 1, o=100, h=100.2, l=99.8, c=100.0),
            _candle(start, 8, o=100, h=100.2, l=99.8, c=100.0),
            _candle(start, 9, o=100, h=100.2, l=99.8, c=100.0),
        ]
        script = ["no_trade", "entry", "no_trade", "no_trade"]
        settings = BacktestSettings(candle_interval_seconds=60)
        with tempfile.TemporaryDirectory() as tmp:
            base = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "base.jsonl"),
                strategy_object=_Scripted(list(script)),
                settings=settings,
                research_event_capture=None,
            ).run()
            writer = ResearchEventWriter(
                path=self._runs(tmp, "B0_events.jsonl"),
                run_id="eqgap",
                arm="B0",
            )
            inst = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "inst.jsonl"),
                strategy_object=_Scripted(list(script)),
                settings=settings,
                research_event_capture=writer,
            ).run()
        self._compare(base, inst)
        self.assertGreaterEqual(base["fills_cancelled_missing_next"], 1)

    def test_force_close_equivalence(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles = [_candle(start, i, o=100, h=100.2, l=99.8, c=100.0) for i in range(6)]
        script = ["no_trade", "no_trade", "entry", "no_trade", "no_trade", "no_trade"]
        settings = BacktestSettings(candle_interval_seconds=60)
        with tempfile.TemporaryDirectory() as tmp:
            base = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "base.jsonl"),
                strategy_object=_Scripted(list(script)),
                settings=settings,
                force_close_at_end=True,
                research_event_capture=None,
            ).run()
            writer = ResearchEventWriter(
                path=self._runs(tmp, "B0_events.jsonl"),
                run_id="eqfc",
                arm="B0",
            )
            inst = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "inst.jsonl"),
                strategy_object=_Scripted(list(script)),
                settings=settings,
                force_close_at_end=True,
                research_event_capture=writer,
            ).run()
        self._compare(base, inst)
        self.assertGreaterEqual(base["trade_count"], 1)

    def test_incompatible_and_b0_accounting_equivalence(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles = [
            _candle(start, 0, o=100, h=100.2, l=99.8, c=100.0),
            _candle(start, 1, o=100, h=100.2, l=99.8, c=100.0),
            _candle(start, 2, o=100, h=100.2, l=99.8, c=100.0),
            _candle(start, 3, o=120.0, h=120.5, l=119.5, c=120.0),
            _candle(start, 4, o=120.0, h=120.2, l=119.8, c=120.0),
        ]
        script = ["no_trade", "no_trade", "entry_tight", "no_trade", "no_trade"]
        settings = BacktestSettings(candle_interval_seconds=60, initial_equity=10_000.0)
        with tempfile.TemporaryDirectory() as tmp:
            base = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "base.jsonl"),
                strategy_object=_Scripted(list(script)),
                settings=settings,
                research_event_capture=None,
            ).run()
            writer = ResearchEventWriter(
                path=self._runs(tmp, "B0_events.jsonl"),
                run_id="eqinc",
                arm="B0",
            )
            inst = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "inst.jsonl"),
                strategy_object=_Scripted(list(script)),
                settings=settings,
                research_event_capture=writer,
            ).run()
            # Production adapter arm (B0-like) also stays equivalent.
            base_b0 = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "base_b0.jsonl"),
                strategy_object=adapt_production("momentum_breakout"),
                settings=settings,
                research_event_capture=None,
            ).run()
            writer_b0 = ResearchEventWriter(
                path=self._runs(tmp, "B0_prod_events.jsonl"),
                run_id="eqb0",
                arm="B0",
            )
            inst_b0 = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._runs(tmp, "inst_b0.jsonl"),
                strategy_object=adapt_production("momentum_breakout"),
                settings=settings,
                research_event_capture=writer_b0,
            ).run()
        self._compare(base, inst)
        self._compare(base_b0, inst_b0)
        self.assertGreaterEqual(base["fills_cancelled_incompatible"], 1)


if __name__ == "__main__":
    unittest.main()

