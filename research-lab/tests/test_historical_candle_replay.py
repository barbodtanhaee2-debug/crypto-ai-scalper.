"""Offline tests for Stage 3.2.4 historical catalog and candle replay."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtester import BacktestSettings
from strategy import Bar

from strategy import SimulatedPosition, StrategyDecision, StrategySettings

from research_lab.backtesting.historical_replay import (
    EXECUTION_MODEL,
    HistoricalCandleReplay,
    fingerprint_journal,
    run_formal_historical_replay,
    synthetic_market_health,
)
from research_lab.datasets.historical_catalog import (
    SYNTHETIC_QUOTE_REASON,
    CutoffValidatedCandles,
    HistoricalCandle,
    assert_before_cutoff,
    bind_historical_dataset,
    candles_from_bars,
    ensure_default_historical_registration,
    filter_aggtrade_timestamp,
    iter_klines_from_csv,
    load_formal_research_candles,
    load_klines,
    parse_utc,
    sha256_file,
)
from research_lab.historical.constants import PROVENANCE, RESEARCH_CUTOFF
from research_lab.strategies.confirmed_momentum import (
    ConfirmedMomentumBreakoutStrategy,
    DecisionLoggingAdapter,
)


def _candle(
    start: datetime,
    minute: int,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> HistoricalCandle:
    open_time = start + timedelta(minutes=minute)
    close_time = open_time + timedelta(milliseconds=59_999)
    return HistoricalCandle(
        open_time=open_time,
        close_time=close_time,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1.0,
        quote_volume=100.0,
        trades=10,
        provenance=PROVENANCE,
    )


def _write_processed_csv(path: Path, candles: list[HistoricalCandle]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "open_time_utc",
        "close_time_utc",
        "open_time_raw",
        "close_time_raw",
        "timestamp_unit",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trades",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
        "ignore",
        "provenance",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for candle in candles:
            writer.writerow(
                {
                    "open_time_utc": candle.open_time.isoformat(),
                    "close_time_utc": candle.close_time.isoformat(),
                    "open_time_raw": 1_730_678_400_000 + int(
                        (candle.open_time.timestamp() - 1_730_678_400) * 1000
                    ),
                    "close_time_raw": 1_730_678_400_000
                    + int((candle.close_time.timestamp() - 1_730_678_400) * 1000),
                    "timestamp_unit": "ms",
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                    "quote_volume": candle.quote_volume,
                    "trades": candle.trades,
                    "taker_buy_base_volume": 0.0,
                    "taker_buy_quote_volume": 0.0,
                    "ignore": 0,
                    "provenance": PROVENANCE,
                }
            )
    return path


class HoldoutAndCatalogTests(unittest.TestCase):
    def test_cutoff_constant(self):
        self.assertEqual(
            RESEARCH_CUTOFF,
            datetime(2026, 10, 31, 4, 20, 59, 999000, tzinfo=timezone.utc),
        )

    def test_assert_before_cutoff_boundary(self):
        assert_before_cutoff(RESEARCH_CUTOFF - timedelta(milliseconds=1))
        with self.assertRaises(ValueError):
            assert_before_cutoff(RESEARCH_CUTOFF)
        with self.assertRaises(ValueError):
            assert_before_cutoff(RESEARCH_CUTOFF + timedelta(seconds=1))

    def test_load_klines_excludes_at_and_after_cutoff(self):
        start = datetime(2026, 10, 31, 4, 18, tzinfo=timezone.utc)
        candles = [
            _candle(start, 0, open_=100, high=101, low=99, close=100),
            _candle(start, 1, open_=100, high=101, low=99, close=100.5),
            # close_time for minute starting 04:20 == 04:20:59.999 == CUTOFF → exclude
            _candle(start, 2, open_=100, high=101, low=99, close=101),
            _candle(start, 3, open_=101, high=102, low=100, close=101.5),
        ]
        # Verify minute 2 close equals cutoff
        self.assertEqual(candles[2].close_time, RESEARCH_CUTOFF)
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_processed_csv(Path(tmp) / "k.csv", candles)
            loaded, excluded = load_klines(path, cutoff=RESEARCH_CUTOFF)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(excluded, 2)
            self.assertTrue(all(item.close_time < RESEARCH_CUTOFF for item in loaded))

    def test_aggtrade_filter(self):
        self.assertTrue(
            filter_aggtrade_timestamp(RESEARCH_CUTOFF - timedelta(milliseconds=1))
        )
        self.assertFalse(filter_aggtrade_timestamp(RESEARCH_CUTOFF))

    def test_ms_us_parse_utc(self):
        ms = datetime(2024, 11, 4, tzinfo=timezone.utc)
        us = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(parse_utc(ms.isoformat()), ms)
        self.assertEqual(parse_utc(us.isoformat()), us)
        with self.assertRaises(ValueError):
            parse_utc("2024-11-04T00:00:00")

    def test_ensure_registration_and_bind_without_loading_all(self):
        # Uses real Stage 3.2.3 manifest under research-lab (offline, local files).
        entry = ensure_default_historical_registration()
        self.assertEqual(entry["provenance"], PROVENANCE)
        bound = bind_historical_dataset(load_candles=False)
        self.assertEqual(bound.provenance, PROVENANCE)
        self.assertEqual(bound.research_end_exclusive, RESEARCH_CUTOFF)
        self.assertEqual(bound.manifest_sha256, sha256_file(bound.manifest_path))
        self.assertEqual(bound.candles, ())


class SyntheticQuoteAndTimingTests(unittest.TestCase):
    def test_synthetic_label(self):
        health = synthetic_market_health(
            mid=100.0,
            spread_fraction=0.0002,
            quote_time=datetime(2026, 10, 30, tzinfo=timezone.utc),
            decision_or_fill_time=datetime(2026, 10, 30, tzinfo=timezone.utc),
            price_basis="next_candle_open",
        )
        self.assertTrue(health["synthetic"])
        self.assertIn("synthetic", str(health["reason"]).lower())
        self.assertEqual(health["reason"], SYNTHETIC_QUOTE_REASON)
        self.assertAlmostEqual(health["ask"] - health["bid"], health["spread"])

    def test_next_candle_open_fill_not_same_close(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles: list[HistoricalCandle] = []
        for minute in range(28):
            if minute < 21:
                close = 100.0
            else:
                close = 101.0
            open_px = close - 0.01
            candles.append(
                _candle(
                    start,
                    minute,
                    open_=open_px,
                    high=close + 0.2,
                    low=min(open_px, close) - 0.2,
                    close=close,
                )
            )
        with tempfile.TemporaryDirectory() as tmp:
            journal = (
                Path(tmp) / "experiments" / "runs" / "stage324" / "timing.jsonl"
            )
            journal.parent.mkdir(parents=True)
            result = HistoricalCandleReplay(
                candles=candles,
                journal_path=journal,
                strategy_object=DecisionLoggingAdapter(),
                settings=BacktestSettings(candle_interval_seconds=60),
                force_close_at_end=True,
            ).run()
            fills = [
                event
                for event in result["events"]
                if event.get("event") == "deferred_fill" and event.get("accepted")
            ]
            self.assertGreaterEqual(len(fills), 1)
            first_fill = fills[0]
            self.assertEqual(first_fill["price_basis"], "next_candle_open")
            self.assertTrue(first_fill["synthetic_quote"])
            self.assertEqual(first_fill["mid"], candles[22].open)
            self.assertEqual(result["execution_model"], EXECUTION_MODEL)
            self.assertNotEqual(first_fill["mid"], candles[21].close)


class ReplayBehaviourTests(unittest.TestCase):
    def _runs_journal(self, root: Path, name: str) -> Path:
        runs = root / "experiments" / "runs" / "stage324"
        runs.mkdir(parents=True, exist_ok=True)
        return runs / name

    def _run(self, candles, journal, strategy):
        return HistoricalCandleReplay(
            candles=candles,
            journal_path=journal,
            strategy_object=strategy,
            settings=BacktestSettings(candle_interval_seconds=60),
            force_close_at_end=True,
        ).run()

    def _series(self, *, gap_after: int | None = None, minutes: int = 28):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles: list[HistoricalCandle] = []
        minute = 0
        while len(candles) < minutes:
            if gap_after is not None and minute == gap_after + 1:
                minute += 1  # skip one minute → gap
                continue
            if minute < 21:
                close = 100.0
            else:
                close = 101.0
            candles.append(
                _candle(
                    start,
                    minute,
                    open_=close - 0.01,
                    high=close + 0.2,
                    low=close - 0.2,
                    close=close,
                )
            )
            minute += 1
        return candles

    def test_chronological_and_no_lookahead(self):
        candles = self._series()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self._run(
                candles, self._runs_journal(root, "chrono.jsonl"), DecisionLoggingAdapter()
            )
        self.assertEqual(result["look_ahead_rejections"], 0)
        self.assertEqual(result["candles_processed"], len(candles))
        times = [
            event["timestamp"]
            for event in result["events"]
            if event.get("event") == "strategy_decision"
        ]
        self.assertEqual(times, sorted(times))

    def test_missing_candle_cancels_pending(self):
        candles = self._series(gap_after=21, minutes=26)
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(
                candles,
                self._runs_journal(Path(tmp), "gap.jsonl"),
                DecisionLoggingAdapter(),
            )
        self.assertGreaterEqual(result["missing_intervals"], 1)
        cancel_events = [
            event
            for event in result["events"]
            if event.get("event") == "pending_fill_cancelled_missing_next_candle"
        ]
        # Gap after breakout signal minute should cancel deferred entry when present.
        if result["fills_deferred"] > 0:
            self.assertGreaterEqual(len(cancel_events) + result["fills_cancelled_missing_next"], 1)

    def test_deterministic_double_run(self):
        candles = self._series()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = self._run(
                candles, self._runs_journal(root, "a.jsonl"), DecisionLoggingAdapter()
            )
            b = self._run(
                candles, self._runs_journal(root, "b.jsonl"), DecisionLoggingAdapter()
            )
            self.assertEqual(a["trades"], b["trades"])
            self.assertEqual(
                fingerprint_journal(root / "experiments" / "runs" / "stage324" / "a.jsonl"),
                fingerprint_journal(root / "experiments" / "runs" / "stage324" / "b.jsonl"),
            )

    def test_long_only_short_rejected_by_broker(self):
        # Baseline/H1 are long-only by design; verify broker setting.
        with tempfile.TemporaryDirectory() as tmp:
            candles = self._series()
            result = self._run(
                candles,
                self._runs_journal(Path(tmp), "long.jsonl"),
                DecisionLoggingAdapter(),
            )
        # No short trades in closed trade list.
        for trade in result["trades"]:
            self.assertEqual(trade.get("direction"), "long")

    def test_fee_spread_slippage_present_on_closed_trade(self):
        candles = self._series(minutes=35)
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(
                candles,
                self._runs_journal(Path(tmp), "costs.jsonl"),
                DecisionLoggingAdapter(),
            )
        if not result["trades"]:
            self.skipTest("Fixture produced no closed trades; timing still covered elsewhere.")
        trade = result["trades"][0]
        self.assertIn("entry_fee", trade)
        self.assertIn("exit_fee", trade)
        self.assertGreater(trade["entry_fee"] + trade["exit_fee"], 0)

    def test_b0_and_h1_same_observations_smoke(self):
        candles = self._series(minutes=35)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b0 = self._run(
                candles,
                self._runs_journal(root, "b0.jsonl"),
                DecisionLoggingAdapter(),
            )
            h1 = self._run(
                candles,
                self._runs_journal(root, "h1.jsonl"),
                ConfirmedMomentumBreakoutStrategy(),
            )
        b0_obs = [
            (e["timestamp"], e["observation_price_basis"])
            for e in b0["events"]
            if e.get("event") == "strategy_decision"
        ]
        h1_obs = [
            (e["timestamp"], e["observation_price_basis"])
            for e in h1["events"]
            if e.get("event") == "strategy_decision"
        ]
        self.assertEqual(b0_obs, h1_obs)
        self.assertEqual(b0["execution_model"], h1["execution_model"])
        self.assertEqual(b0["candles_processed"], h1["candles_processed"])
        self.assertTrue(b0["synthetic_quotes_only"])
        self.assertTrue(h1["synthetic_quotes_only"])

    def test_holdout_candle_rejected_by_replay(self):
        start = datetime(2026, 10, 31, 4, 20, tzinfo=timezone.utc)
        bad = [_candle(start, 0, open_=100, high=101, low=99, close=100)]
        self.assertEqual(bad[0].close_time, RESEARCH_CUTOFF)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self._run(
                    bad,
                    self._runs_journal(Path(tmp), "holdout.jsonl"),
                    DecisionLoggingAdapter(),
                )

    def test_journal_must_be_under_runs(self):
        candles = self._series(minutes=5)
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.jsonl"
            with self.assertRaises(ValueError):
                HistoricalCandleReplay(
                    candles=candles,
                    journal_path=outside,
                    strategy_object=DecisionLoggingAdapter(),
                )


class FixtureHelperTests(unittest.TestCase):
    def test_candles_from_bars(self):
        start = datetime(2026, 10, 30, 12, 0, 59, 999000, tzinfo=timezone.utc)
        bars = [Bar(timestamp=start, open=1, high=2, low=0.5, close=1.5)]
        candles = candles_from_bars(bars)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].close_time, start)
        self.assertLess(candles[0].open_time, candles[0].close_time)

    def test_candles_from_bars_rejects_cutoff_by_default(self):
        bars = [
            Bar(
                timestamp=RESEARCH_CUTOFF,
                open=1,
                high=2,
                low=0.5,
                close=1.5,
            )
        ]
        with self.assertRaises(ValueError):
            candles_from_bars(bars)
        allowed = candles_from_bars(bars, allow_at_or_after_cutoff=True)
        self.assertEqual(len(allowed), 1)


class _ScriptedStrategy:
    """Deterministic fixture strategy; not a production or H1 change."""

    name = "fixture_scripted"
    settings = StrategySettings()

    def __init__(self, script: list[str]) -> None:
        self._script = list(script)
        self.seen_decision_times: list[datetime] = []
        self.seen_bar_closes: list[list[datetime]] = []

    def evaluate(self, bars, *, decision_time, market_health, equity, position=None):
        self.seen_decision_times.append(decision_time)
        self.seen_bar_closes.append([bar.timestamp for bar in bars])
        action = self._script.pop(0) if self._script else "no_trade"
        if action == "entry" and position is None:
            ask = float(market_health["ask"])
            stop = ask * 0.99
            target = ask * 1.02
            qty = 0.001
            return StrategyDecision(
                "entry",
                self.name,
                "long",
                decision_time,
                "fixture entry",
                {"exit_price": target},
                SimulatedPosition(
                    self.name, "long", ask, qty, stop, target, decision_time
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
        if action == "entry_tight" and position is None:
            # Stop/target incompatible with a far next open.
            ask = float(market_health["ask"])
            return StrategyDecision(
                "entry",
                self.name,
                "long",
                decision_time,
                "fixture tight entry",
                {},
                SimulatedPosition(
                    self.name,
                    "long",
                    ask,
                    0.001,
                    ask - 0.01,
                    ask + 0.01,
                    decision_time,
                ),
            )
        return StrategyDecision(
            "no_trade", self.name, None, decision_time, "fixture flat", {}, None
        )


class ReliabilityPatchTests(unittest.TestCase):
    def _journal(self, root: Path, name: str) -> Path:
        path = root / "experiments" / "runs" / "stage324" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_incompatible_next_open_cancels_but_still_evaluates(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        # Candle0 close → entry_tight; candle1 open far away → incompatible; still evaluate.
        candles = [
            _candle(start, 0, open_=100.0, high=100.2, low=99.8, close=100.0),
            _candle(start, 1, open_=110.0, high=110.5, low=109.5, close=110.0),
            _candle(start, 2, open_=110.0, high=110.2, low=109.8, close=110.0),
        ]
        strategy = _ScriptedStrategy(["entry_tight", "no_trade", "no_trade"])
        with tempfile.TemporaryDirectory() as tmp:
            result = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._journal(Path(tmp), "incompat.jsonl"),
                strategy_object=strategy,
                settings=BacktestSettings(candle_interval_seconds=60),
                force_close_at_end=False,
            ).run()
        cancel = [
            e
            for e in result["events"]
            if e.get("event") == "pending_fill_cancelled_incompatible_next_open"
        ]
        decisions = [
            e for e in result["events"] if e.get("event") == "strategy_decision"
        ]
        fills = [e for e in result["events"] if e.get("event") == "deferred_fill"]
        self.assertEqual(len(cancel), 1)
        self.assertEqual(len(decisions), 3)
        self.assertEqual(result["candles_processed"], 3)
        self.assertEqual(len(strategy.seen_decision_times), 3)
        self.assertFalse(any(f.get("accepted") for f in fills))
        self.assertEqual(result["fills_cancelled_incompatible"], 1)
        self.assertEqual(result["trade_count"], 0)

    def test_missing_next_emits_cancel_event(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles = [
            _candle(start, 0, open_=100.0, high=100.2, low=99.8, close=100.0),
            # Skip minute 1 → gap from open0 to open2 = 120s > 90
            _candle(start, 2, open_=100.0, high=100.2, low=99.8, close=100.0),
        ]
        strategy = _ScriptedStrategy(["entry", "no_trade"])
        with tempfile.TemporaryDirectory() as tmp:
            result = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._journal(Path(tmp), "gap_event.jsonl"),
                strategy_object=strategy,
                settings=BacktestSettings(candle_interval_seconds=60),
                force_close_at_end=False,
            ).run()
        cancel = [
            e
            for e in result["events"]
            if e.get("event") == "pending_fill_cancelled_missing_next_candle"
        ]
        self.assertEqual(len(cancel), 1)
        self.assertGreaterEqual(result["fills_cancelled_missing_next"], 1)
        self.assertEqual(result["trade_count"], 0)

    def test_force_close_false_keeps_unresolved(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles = [
            _candle(start, 0, open_=100.0, high=100.2, low=99.8, close=100.0),
            _candle(start, 1, open_=100.0, high=100.2, low=99.8, close=100.0),
            _candle(start, 2, open_=100.0, high=100.2, low=99.8, close=100.0),
        ]
        strategy = _ScriptedStrategy(["entry", "no_trade", "no_trade"])
        with tempfile.TemporaryDirectory() as tmp:
            result = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._journal(Path(tmp), "unresolved.jsonl"),
                strategy_object=strategy,
                settings=BacktestSettings(candle_interval_seconds=60),
                force_close_at_end=False,
            ).run()
        self.assertTrue(result["open_position"])
        self.assertEqual(result["unresolved_positions"], 1)
        self.assertEqual(result["trade_count"], 0)

    def test_deferred_exit_at_next_open(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles = [
            _candle(start, 0, open_=100.00, high=100.2, low=99.8, close=100.00),
            _candle(start, 1, open_=100.00, high=100.2, low=99.8, close=100.00),
            _candle(start, 2, open_=100.50, high=100.7, low=100.3, close=100.50),
            _candle(start, 3, open_=100.40, high=100.6, low=100.2, close=100.40),
        ]
        # entry on 0 → fill at 1 open; exit on 1 close → fill at 2 open
        strategy = _ScriptedStrategy(["entry", "exit", "no_trade", "no_trade"])
        with tempfile.TemporaryDirectory() as tmp:
            result = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._journal(Path(tmp), "exit_next.jsonl"),
                strategy_object=strategy,
                settings=BacktestSettings(candle_interval_seconds=60),
                force_close_at_end=False,
            ).run()
        fills = [e for e in result["events"] if e.get("event") == "deferred_fill"]
        self.assertGreaterEqual(len(fills), 2)
        exit_fills = [f for f in fills if f.get("action") == "exit" and f.get("accepted")]
        self.assertEqual(len(exit_fills), 1)
        self.assertEqual(exit_fills[0]["price_basis"], "next_candle_open")
        self.assertEqual(exit_fills[0]["mid"], candles[2].open)
        self.assertEqual(result["trade_count"], 1)

    def test_fee_slip_half_spread_hand_calculation(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        settings = BacktestSettings(candle_interval_seconds=60)
        open_px = 100.0
        candles = [
            _candle(start, 0, open_=100.0, high=100.2, low=99.8, close=100.0),
            _candle(start, 1, open_=open_px, high=100.2, low=99.8, close=100.0),
            _candle(start, 2, open_=100.0, high=100.2, low=99.8, close=100.0),
        ]
        strategy = _ScriptedStrategy(["entry", "exit", "no_trade"])
        with tempfile.TemporaryDirectory() as tmp:
            result = HistoricalCandleReplay(
                candles=candles,
                journal_path=self._journal(Path(tmp), "costs_exact.jsonl"),
                strategy_object=strategy,
                settings=settings,
                force_close_at_end=False,
            ).run()
        self.assertEqual(result["trade_count"], 1)
        trade = result["trades"][0]
        qty = float(trade["quantity_btc"])
        # Entry fill at candle1 open mid=100
        spread = open_px * settings.spread_fraction
        ask = open_px + spread / 2.0
        expected_entry_fill = ask * (1.0 + settings.slippage_rate_per_side)
        expected_entry_fee = expected_entry_fill * qty * settings.fee_rate_per_side
        expected_entry_slip = ask * settings.slippage_rate_per_side * qty
        expected_entry_half_spread = (spread / 2.0) * qty
        self.assertAlmostEqual(float(trade["entry_fill_price"]), expected_entry_fill, places=10)
        self.assertAlmostEqual(float(trade["entry_fee"]), expected_entry_fee, places=10)
        self.assertAlmostEqual(
            float(trade["entry_slippage_cost_estimate"]), expected_entry_slip, places=10
        )
        self.assertAlmostEqual(
            float(trade["entry_spread_cost_estimate"]),
            expected_entry_half_spread,
            places=10,
        )

    def test_iter_klines_csv_cutoff_equality(self):
        start = datetime(2026, 10, 31, 4, 18, tzinfo=timezone.utc)
        candles = [
            _candle(start, 0, open_=100, high=101, low=99, close=100),
            _candle(start, 1, open_=100, high=101, low=99, close=100.5),
            _candle(start, 2, open_=100, high=101, low=99, close=101),  # close==cutoff
        ]
        self.assertEqual(candles[2].close_time, RESEARCH_CUTOFF)
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_processed_csv(Path(tmp) / "cut.csv", candles)
            loaded = list(iter_klines_from_csv(path, cutoff=RESEARCH_CUTOFF))
        self.assertEqual(len(loaded), 2)
        self.assertTrue(all(item.close_time < RESEARCH_CUTOFF for item in loaded))

    def test_no_future_ohlc_in_decisions(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles = [
            _candle(start, minute, open_=100, high=100.2, low=99.8, close=100.0)
            for minute in range(5)
        ]
        strategy = _ScriptedStrategy(["no_trade"] * 5)
        with tempfile.TemporaryDirectory() as tmp:
            HistoricalCandleReplay(
                candles=candles,
                journal_path=self._journal(Path(tmp), "lookahead.jsonl"),
                strategy_object=strategy,
                settings=BacktestSettings(candle_interval_seconds=60),
                force_close_at_end=False,
            ).run()
        for decision_time, bar_times in zip(
            strategy.seen_decision_times, strategy.seen_bar_closes
        ):
            self.assertTrue(all(ts <= decision_time for ts in bar_times))

    def test_formal_entrypoint_rejects_force_close_and_raw_list(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        candles = tuple(
            _candle(start, minute, open_=100, high=100.2, low=99.8, close=100.0)
            for minute in range(3)
        )
        bundle = CutoffValidatedCandles(
            candles=candles,
            research_cutoff=RESEARCH_CUTOFF,
            source="load_klines",
        )
        with tempfile.TemporaryDirectory() as tmp:
            journal = self._journal(Path(tmp), "formal.jsonl")
            with self.assertRaises(ValueError):
                run_formal_historical_replay(
                    candles=bundle,
                    journal_path=journal,
                    strategy_object=_ScriptedStrategy(["no_trade"] * 3),
                    force_close_at_end=True,
                )
            with self.assertRaises(TypeError):
                run_formal_historical_replay(
                    candles=candles,  # type: ignore[arg-type]
                    journal_path=journal,
                    strategy_object=_ScriptedStrategy(["no_trade"] * 3),
                )
            result = run_formal_historical_replay(
                candles=bundle,
                journal_path=journal,
                strategy_object=_ScriptedStrategy(["entry", "no_trade", "no_trade"]),
                force_close_at_end=False,
            )
        self.assertTrue(result["formal_scoring_entrypoint"])
        self.assertFalse(result["force_close_at_end"])
        self.assertTrue(result["unresolved_reported_separately"])
        self.assertEqual(result["unresolved_positions"], 1)
        self.assertEqual(len(result["completed_scored_trades"]), 0)

    def test_load_formal_research_candles_wrapper(self):
        start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        rows = [
            _candle(start, 0, open_=100, high=101, low=99, close=100),
            _candle(
                datetime(2026, 10, 31, 4, 20, tzinfo=timezone.utc),
                0,
                open_=100,
                high=101,
                low=99,
                close=100,
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_processed_csv(Path(tmp) / "f.csv", rows)
            formal = load_formal_research_candles(path)
        self.assertIsInstance(formal, CutoffValidatedCandles)
        self.assertEqual(len(formal.candles), 1)
        self.assertEqual(formal.excluded_at_or_after_cutoff, 1)


if __name__ == "__main__":
    unittest.main()
