"""Stage 3.3.1 — synthetic-fixture tests for market characterization."""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone

from research_lab.characterization import (
    CharacterizationCandle,
    CharacterizationConfig,
    MarketCharacterizationEngine,
    OpportunityStatus,
    PrimaryRegime,
    VolatilityLevel,
    characterize,
)
from research_lab.characterization.config import (
    OpportunityConfig,
    PrimaryRegimeConfig,
    SecondaryConfig,
    StabilityConfig,
)
from research_lab.characterization.data_quality import validate_candle_series
from research_lab.characterization.features import compute_feature_series, compute_features_at_close
from research_lab.characterization.models import DataQualityStatus
from research_lab.characterization.opportunity import assess_opportunity
from research_lab.characterization.stability import RegimeStabilityTracker


def _ts(start: datetime, i: int, *, minutes: int = 1) -> datetime:
    return start + timedelta(minutes=minutes * i)


def _candle(
    start: datetime,
    i: int,
    *,
    o: float,
    h: float,
    l: float,
    c: float,
    volume: float | None = None,
    minutes: int = 1,
) -> CharacterizationCandle:
    return CharacterizationCandle(
        timestamp=_ts(start, i, minutes=minutes),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=volume,
    )


def make_uptrend(n: int = 60, start_px: float = 100.0, step: float = 0.4) -> list[CharacterizationCandle]:
    start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    out = []
    px = start_px
    for i in range(n):
        o = px
        c = px + step
        h = c + 0.05
        l = o - 0.05
        out.append(_candle(start, i, o=o, h=h, l=l, c=c))
        px = c
    return out


def make_downtrend(n: int = 60, start_px: float = 130.0, step: float = 0.4) -> list[CharacterizationCandle]:
    start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    out = []
    px = start_px
    for i in range(n):
        o = px
        c = px - step
        h = o + 0.05
        l = c - 0.05
        out.append(_candle(start, i, o=o, h=h, l=l, c=c))
        px = c
    return out


def make_sideways(n: int = 60, mid: float = 100.0, amp: float = 0.15) -> list[CharacterizationCandle]:
    start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        # Tight oscillation
        delta = amp if (i % 2 == 0) else -amp
        o = mid
        c = mid + delta
        h = max(o, c) + 0.02
        l = min(o, c) - 0.02
        out.append(_candle(start, i, o=o, h=h, l=l, c=c))
    return out


def make_high_vol_trend(n: int = 60) -> list[CharacterizationCandle]:
    start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    out = []
    px = 100.0
    for i in range(n):
        shock = 1.8 if i % 3 == 0 else 0.6
        o = px
        c = px + shock
        h = c + 1.2
        l = o - 1.0
        out.append(_candle(start, i, o=o, h=h, l=l, c=c))
        px = c
    return out


def make_high_vol_range(n: int = 60) -> list[CharacterizationCandle]:
    start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    out = []
    mid = 100.0
    for i in range(n):
        side = 1.0 if (i % 2 == 0) else -1.0
        o = mid
        c = mid + side * 2.5
        h = max(o, c) + 1.5
        l = min(o, c) - 1.5
        out.append(_candle(start, i, o=o, h=h, l=l, c=c))
    return out


def make_compression(n: int = 60) -> list[CharacterizationCandle]:
    start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    out = []
    mid = 100.0
    for i in range(n):
        delta = 0.02 if (i % 2 == 0) else -0.02
        o = mid
        c = mid + delta
        h = max(o, c) + 0.005
        l = min(o, c) - 0.005
        out.append(_candle(start, i, o=o, h=h, l=l, c=c))
    return out


class TrendRangeTests(unittest.TestCase):
    def test_clear_upward_trend(self):
        candles = make_uptrend()
        results = characterize(candles)
        last = results[-1]
        self.assertEqual(last.data_quality.status, DataQualityStatus.OK)
        self.assertIn(
            last.confirmed_regime,
            {PrimaryRegime.TRENDING, PrimaryRegime.TRANSITIONING},
        )
        # Eventually confirmed trending given strong fixture.
        confirmed = [r for r in results if r.confirmed_regime == PrimaryRegime.TRENDING]
        self.assertGreaterEqual(len(confirmed), 1)
        self.assertEqual(last.secondary.momentum.value, "STRONG_UP")

    def test_clear_downward_trend(self):
        candles = make_downtrend()
        results = characterize(candles)
        confirmed = [r for r in results if r.confirmed_regime == PrimaryRegime.TRENDING]
        self.assertGreaterEqual(len(confirmed), 1)
        self.assertEqual(results[-1].secondary.momentum.value, "STRONG_DOWN")

    def test_sideways_market(self):
        candles = make_sideways()
        results = characterize(candles)
        ranging = [r for r in results if r.confirmed_regime == PrimaryRegime.RANGING]
        self.assertGreaterEqual(len(ranging), 1)


class VolatilityOverlapTests(unittest.TestCase):
    def test_high_volatility_during_trend(self):
        candles = make_high_vol_trend()
        # NATR dominates; keep low_rvol_max tight so steady large bars are not
        # labelled "compressed RVOL" against high NATR.
        config = CharacterizationConfig(
            secondary=SecondaryConfig(
                high_natr_min=0.003, high_rvol_min=0.008, low_rvol_max=0.001
            )
        )
        results = characterize(candles, config=config)
        last = [r for r in results if r.features is not None][-1]
        self.assertIn(
            last.confirmed_regime,
            {PrimaryRegime.TRENDING, PrimaryRegime.TRANSITIONING},
        )
        self.assertEqual(last.secondary.volatility, VolatilityLevel.HIGH_VOLATILITY)

    def test_high_volatility_during_range(self):
        candles = make_high_vol_range()
        config = CharacterizationConfig(
            primary=PrimaryRegimeConfig(
                range_efficiency_max=0.35,
                range_width_max=0.12,
            ),
            secondary=SecondaryConfig(high_natr_min=0.003, high_rvol_min=0.008),
        )
        results = characterize(candles, config=config)
        lasted = [r for r in results if r.features is not None][-1]
        self.assertEqual(lasted.secondary.volatility, VolatilityLevel.HIGH_VOLATILITY)
        # Primary may be ranging or transitioning; must not force exclusive vol regime.
        self.assertNotEqual(lasted.secondary.volatility, VolatilityLevel.UNKNOWN)

    def test_low_volatility_compression(self):
        candles = make_compression()
        config = CharacterizationConfig(
            secondary=SecondaryConfig(low_natr_max=0.002, low_rvol_max=0.006)
        )
        results = characterize(candles, config=config)
        last = [r for r in results if r.features is not None][-1]
        self.assertEqual(last.secondary.volatility, VolatilityLevel.LOW_VOLATILITY)


class TransitionConflictTests(unittest.TestCase):
    def test_transitioning_conditions(self):
        # Shift from range into trend mid-series.
        start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        candles = make_sideways(40)
        px = candles[-1].close
        for i in range(40, 70):
            o = px
            c = px + 0.5
            candles.append(
                _candle(start, i, o=o, h=c + 0.05, l=o - 0.05, c=c)
            )
            px = c
        results = characterize(candles)
        regimes = {r.primary.regime for r in results if r.features is not None}
        self.assertTrue(
            PrimaryRegime.TRANSITIONING in regimes
            or PrimaryRegime.TRENDING in {r.confirmed_regime for r in results}
        )

    def test_conflicting_evidence(self):
        # High efficiency but extremely tight range width → conflict path.
        from research_lab.characterization.models import FeatureSnapshot
        from research_lab.characterization.primary import classify_primary_raw

        features = FeatureSnapshot(
            atr=1.0,
            normalized_atr=0.01,
            realized_volatility=0.01,
            directional_efficiency=0.9,
            ema_fast=101.0,
            ema_slow=100.0,
            ema_separation=0.01,
            rolling_high=100.5,
            rolling_low=100.0,
            range_width=0.001,  # too tight for trend_range_width_min default
            close=100.0,
            volume_deviation=None,
        )
        # Force both trend_like and range_like via custom config.
        cfg = PrimaryRegimeConfig(
            trend_efficiency_min=0.3,
            ema_separation_min=0.001,
            trend_range_width_min=0.0005,
            range_efficiency_max=0.95,
            range_width_max=0.05,
        )
        result = classify_primary_raw(features, config=cfg)
        self.assertTrue(result.conflicting)
        self.assertEqual(result.raw_regime, PrimaryRegime.TRANSITIONING)


class DataQualityTests(unittest.TestCase):
    def test_insufficient_warmup(self):
        candles = make_uptrend(5)
        results = characterize(candles)
        self.assertTrue(
            all(r.primary.regime == PrimaryRegime.UNKNOWN for r in results)
        )
        self.assertEqual(
            results[-1].data_quality.status, DataQualityStatus.INSUFFICIENT_DATA
        )

    def test_missing_candles(self):
        start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        candles = [
            _candle(start, 0, o=100, h=100.1, l=99.9, c=100.0),
            _candle(start, 2, o=100, h=100.1, l=99.9, c=100.0),  # gap of 1
        ]
        report = validate_candle_series(
            candles,
            config=CharacterizationConfig(),
            require_warmup=False,
        )
        self.assertEqual(report.status, DataQualityStatus.GAP)
        self.assertGreaterEqual(report.gap_intervals, 1)

    def test_duplicate_candles(self):
        start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        c = _candle(start, 0, o=100, h=100.1, l=99.9, c=100.0)
        report = validate_candle_series(
            [c, c],
            config=CharacterizationConfig(),
            require_warmup=False,
        )
        self.assertEqual(report.status, DataQualityStatus.INVALID)
        self.assertTrue(any("duplicate" in i for i in report.issues))

    def test_invalid_ohlc(self):
        start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        bad = CharacterizationCandle(
            timestamp=start, open=100, high=99, low=101, close=100
        )
        report = validate_candle_series(
            [bad], config=CharacterizationConfig(), require_warmup=False
        )
        self.assertEqual(report.status, DataQualityStatus.INVALID)

    def test_non_monotonic_timestamps(self):
        start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        candles = [
            _candle(start, 1, o=100, h=100.1, l=99.9, c=100.0),
            _candle(start, 0, o=100, h=100.1, l=99.9, c=100.0),
        ]
        report = validate_candle_series(
            candles, config=CharacterizationConfig(), require_warmup=False
        )
        self.assertEqual(report.status, DataQualityStatus.INVALID)
        self.assertTrue(any("non-monotonic" in i for i in report.issues))

    def test_non_finite_indicator_calculations(self):
        start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        candles = []
        for i in range(30):
            c = 100.0 if i < 29 else float("nan")
            # Construct via object.__new__ bypass? CharacterizationCandle will store nan.
            candles.append(
                CharacterizationCandle(
                    timestamp=_ts(start, i),
                    open=100.0,
                    high=100.1,
                    low=99.9,
                    close=c,
                )
            )
        # validate should catch non-finite close
        report = validate_candle_series(
            candles, config=CharacterizationConfig(), require_warmup=False
        )
        self.assertEqual(report.status, DataQualityStatus.INVALID)


class StabilityTests(unittest.TestCase):
    def test_regime_hysteresis(self):
        cfg = StabilityConfig(min_persistence_bars=3, hysteresis_bars=2)
        tracker = RegimeStabilityTracker(config=cfg)
        for _ in range(3):
            confirmed, _ = tracker.update(PrimaryRegime.TRENDING)
        self.assertEqual(confirmed, PrimaryRegime.TRENDING)
        # One opposing label must not flip.
        confirmed, _ = tracker.update(PrimaryRegime.RANGING)
        self.assertEqual(confirmed, PrimaryRegime.TRENDING)
        # Need min_persistence + hysteresis = 5 ranging labels total.
        for _ in range(3):
            confirmed, _ = tracker.update(PrimaryRegime.RANGING)
            self.assertEqual(confirmed, PrimaryRegime.TRENDING)
        confirmed, _ = tracker.update(PrimaryRegime.RANGING)
        self.assertEqual(confirmed, PrimaryRegime.RANGING)

    def test_regime_persistence(self):
        cfg = StabilityConfig(min_persistence_bars=3, hysteresis_bars=0)
        tracker = RegimeStabilityTracker(config=cfg)
        self.assertEqual(tracker.update(PrimaryRegime.RANGING)[0], PrimaryRegime.UNKNOWN)
        self.assertEqual(tracker.update(PrimaryRegime.RANGING)[0], PrimaryRegime.UNKNOWN)
        confirmed, count = tracker.update(PrimaryRegime.RANGING)
        self.assertEqual(confirmed, PrimaryRegime.RANGING)
        self.assertEqual(count, 1)
        confirmed, count = tracker.update(PrimaryRegime.RANGING)
        self.assertEqual(count, 2)

    def test_data_gap_reset_behavior(self):
        cfg = StabilityConfig(min_persistence_bars=2, hysteresis_bars=0, gap_reset_intervals=2)
        tracker = RegimeStabilityTracker(config=cfg)
        tracker.update(PrimaryRegime.TRENDING)
        tracker.update(PrimaryRegime.TRENDING)
        self.assertEqual(tracker.confirmed, PrimaryRegime.TRENDING)
        tracker.on_gap(2)
        self.assertEqual(tracker.confirmed, PrimaryRegime.UNKNOWN)


class OpportunityTests(unittest.TestCase):
    def test_cost_adjusted_opportunity_rejection(self):
        from research_lab.characterization.models import FeatureSnapshot

        features = FeatureSnapshot(
            atr=0.05,
            normalized_atr=0.0005,  # tiny move vs ~0.0032 round-trip costs
            realized_volatility=0.001,
            directional_efficiency=0.5,
            ema_fast=100.0,
            ema_slow=99.0,
            ema_separation=0.01,
            rolling_high=101.0,
            rolling_low=99.0,
            range_width=0.02,
            close=100.0,
            volume_deviation=None,
        )
        assessment = assess_opportunity(features)
        self.assertEqual(
            assessment.status, OpportunityStatus.REJECTED_COSTS_TOO_HIGH
        )
        self.assertIsNotNone(assessment.total_cost_fraction)
        self.assertGreater(assessment.total_cost_fraction, assessment.gross_move_fraction)

    def test_incomplete_cost_information(self):
        from research_lab.characterization.models import FeatureSnapshot

        features = FeatureSnapshot(
            atr=1.0,
            normalized_atr=0.02,
            realized_volatility=0.01,
            directional_efficiency=0.5,
            ema_fast=100.0,
            ema_slow=99.0,
            ema_separation=0.01,
            rolling_high=101.0,
            rolling_low=99.0,
            range_width=0.02,
            close=100.0,
            volume_deviation=None,
        )
        assessment = assess_opportunity(features, costs_available=False)
        self.assertEqual(
            assessment.status, OpportunityStatus.INCOMPLETE_COST_INFORMATION
        )
        self.assertIsNone(assessment.net_move_fraction)

    def test_cost_assumptions_match_backtest_defaults(self):
        cfg = OpportunityConfig()
        self.assertEqual(cfg.fee_rate_per_side, 0.001)
        self.assertEqual(cfg.spread_fraction, 0.0002)
        self.assertEqual(cfg.slippage_rate_per_side, 0.0005)
        expected = 2 * 0.001 + 0.0002 + 2 * 0.0005
        self.assertAlmostEqual(cfg.round_trip_cost_fraction(), expected)


class DeterminismLookaheadTests(unittest.TestCase):
    def test_deterministic_output_identical_inputs(self):
        candles = make_uptrend(50)
        a = characterize(candles)
        b = characterize(candles)
        self.assertEqual([x.to_dict() for x in a], [x.to_dict() for x in b])

    def test_no_look_ahead_behavior(self):
        candles = make_uptrend(40)
        full = compute_feature_series(candles)
        for i in range(10, len(candles)):
            prefix = compute_features_at_close(candles[: i + 1])
            self.assertEqual(prefix.to_dict(), full[i].to_dict())

    def test_no_retrospective_classification_changes(self):
        candles = make_uptrend(50)
        engine = MarketCharacterizationEngine()
        series = engine.evaluate_series(candles)
        # History entries are frozen dataclasses; past confirmed labels stay as emitted.
        snapshot = [r.confirmed_regime for r in series]
        # Re-read engine history — identical, never rewritten.
        self.assertEqual(
            [r.confirmed_regime for r in engine.history],
            snapshot,
        )
        # Extending with more bars must not mutate prior history objects.
        more = make_uptrend(60)
        series2 = MarketCharacterizationEngine().evaluate_series(more)
        for i in range(len(series)):
            self.assertEqual(
                series[i].confirmed_regime, series2[i].confirmed_regime
            )
            self.assertEqual(series[i].to_dict(), series2[i].to_dict())


class IndependenceTests(unittest.TestCase):
    def test_primary_and_secondary_independent(self):
        candles = make_high_vol_trend()
        config = CharacterizationConfig(
            secondary=SecondaryConfig(
                high_natr_min=0.003, high_rvol_min=0.008, low_rvol_max=0.001
            )
        )
        last = [r for r in characterize(candles, config=config) if r.features][-1]
        self.assertEqual(last.secondary.volatility, VolatilityLevel.HIGH_VOLATILITY)
        # High vol coexists with trend/transition primary — not a mutually
        # exclusive primary regime bucket.
        self.assertIn(
            last.confirmed_regime,
            {PrimaryRegime.TRENDING, PrimaryRegime.TRANSITIONING},
        )


if __name__ == "__main__":
    unittest.main()
