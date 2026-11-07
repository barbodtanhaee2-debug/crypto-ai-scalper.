import math
import unittest
from datetime import datetime, timedelta, timezone

from market_regime import (
    HIGH_VOLATILITY_RMS_LOG_RETURN_THRESHOLD,
    MINIMUM_COMPLETED_CANDLES,
    MarketRegime,
    detect_market_regime,
)
from strategy import Bar


class MarketRegimeTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def candles(self, closes, *, padding=0.0005):
        return [
            Bar(
                self.start + timedelta(minutes=index + 1) - timedelta(milliseconds=1),
                close,
                close * (1 + padding),
                close * (1 - padding),
                close,
            )
            for index, close in enumerate(closes)
        ]

    def assess(self, candles, *, as_of=None):
        return detect_market_regime(
            candles,
            as_of=as_of or self.start + timedelta(minutes=MINIMUM_COMPLETED_CANDLES + 1),
        )

    def test_insufficient_completed_history_is_uncertain(self):
        result = self.assess(self.candles([100.0] * (MINIMUM_COMPLETED_CANDLES - 1)))

        self.assertEqual(result.regime, MarketRegime.UNCERTAIN)
        self.assertEqual(result.completed_candles, MINIMUM_COMPLETED_CANDLES - 1)
        self.assertIn("Insufficient completed candles", result.reason)

    def test_invalid_non_finite_prices_are_uncertain(self):
        candles = self.candles([100.0] * MINIMUM_COMPLETED_CANDLES)
        candles[10] = Bar(
            candles[10].timestamp,
            100.0,
            math.inf,
            99.0,
            100.0,
        )

        result = self.assess(candles)

        self.assertEqual(result.regime, MarketRegime.UNCERTAIN)
        self.assertIn("invalid", result.reason)

    def test_invalid_naive_timestamp_is_uncertain(self):
        candles = self.candles([100.0] * MINIMUM_COMPLETED_CANDLES)
        candles[0] = Bar(
            candles[0].timestamp.replace(tzinfo=None),
            100.0,
            100.1,
            99.9,
            100.0,
        )

        result = self.assess(candles)

        self.assertEqual(result.regime, MarketRegime.UNCERTAIN)
        self.assertIn("timezone-aware", result.reason)

    def test_trending_up_and_down(self):
        upward = self.assess(
            self.candles([100.0 + index * 0.1 for index in range(21)])
        )
        downward = self.assess(
            self.candles([102.0 - index * 0.1 for index in range(21)])
        )

        self.assertEqual(upward.regime, MarketRegime.TRENDING)
        self.assertEqual(upward.trend_direction, "UP")
        self.assertGreaterEqual(upward.net_return_fraction, 0.01)
        self.assertGreaterEqual(upward.efficiency_ratio, 0.60)
        self.assertEqual(downward.regime, MarketRegime.TRENDING)
        self.assertEqual(downward.trend_direction, "DOWN")

    def test_sideways_range(self):
        closes = [100.0 + (0.02 if index % 2 == 0 else -0.02) for index in range(21)]

        result = self.assess(self.candles(closes, padding=0.0001))

        self.assertEqual(result.regime, MarketRegime.SIDEWAYS)
        self.assertLessEqual(abs(result.net_return_fraction), 0.003)
        self.assertLessEqual(result.high_low_range_fraction, 0.015)
        self.assertLessEqual(result.efficiency_ratio, 0.35)

    def test_elevated_volatility_classification(self):
        closes = [
            100.0 if index % 2 == 0 else 101.0
            for index in range(MINIMUM_COMPLETED_CANDLES)
        ]

        result = self.assess(self.candles(closes, padding=0.0001))

        self.assertEqual(result.regime, MarketRegime.HIGH_VOLATILITY)
        self.assertGreaterEqual(
            result.rms_log_return_volatility,
            HIGH_VOLATILITY_RMS_LOG_RETURN_THRESHOLD,
        )

    def test_high_volatility_precedes_trend_and_preserves_direction(self):
        closes = [100.0]
        for index in range(20):
            closes.append(closes[-1] * (1.004 if index < 18 else 0.995))

        result = self.assess(self.candles(closes, padding=0.0001))

        self.assertEqual(result.regime, MarketRegime.HIGH_VOLATILITY)
        self.assertEqual(result.trend_direction, "UP")
        self.assertGreaterEqual(abs(result.net_return_fraction), 0.01)
        self.assertGreaterEqual(result.efficiency_ratio, 0.60)

    def test_conflicting_evidence_falls_back_to_uncertain(self):
        closes = [100.0]
        for index in range(20):
            closes.append(closes[-1] * (1.002 if index % 2 == 0 else 0.999))

        result = self.assess(self.candles(closes, padding=0.0001))

        self.assertEqual(result.regime, MarketRegime.UNCERTAIN)
        self.assertIn("does not meet", result.reason)
        self.assertIsNone(result.trend_direction)

    def test_identical_input_produces_identical_output(self):
        candles = self.candles([100.0 + index * 0.1 for index in range(21)])
        as_of = self.start + timedelta(minutes=22)

        first = detect_market_regime(candles, as_of=as_of)
        second = detect_market_regime(candles, as_of=as_of)

        self.assertEqual(first, second)

    def test_candle_is_eligible_at_exact_close_time_boundary(self):
        candles = self.candles([100.0 + index * 0.1 for index in range(21)])
        as_of = candles[-1].timestamp + timedelta(milliseconds=1)

        result = self.assess(candles, as_of=as_of)

        self.assertEqual(result.regime, MarketRegime.TRENDING)
        self.assertEqual(result.completed_candles, MINIMUM_COMPLETED_CANDLES)

    def test_unfinished_and_future_candles_are_excluded(self):
        candles = self.candles([100.0 + index * 0.1 for index in range(21)])
        as_of = candles[-1].timestamp + timedelta(milliseconds=1)
        unfinished = Bar(
            as_of,
            100.0,
            10000.0,
            1.0,
            10000.0,
        )

        original = detect_market_regime(candles, as_of=as_of)
        with_unfinished = detect_market_regime(
            [*candles, unfinished],
            as_of=as_of,
        )

        self.assertEqual(with_unfinished, original)
        self.assertEqual(with_unfinished.completed_candles, 21)

    def test_one_millisecond_before_completion_excludes_last_candle(self):
        candles = self.candles([100.0 + index * 0.1 for index in range(21)])
        as_of = candles[-1].timestamp + timedelta(milliseconds=1) - timedelta(
            microseconds=1
        )

        result = self.assess(candles, as_of=as_of)

        self.assertEqual(result.regime, MarketRegime.UNCERTAIN)
        self.assertEqual(result.completed_candles, 20)
        self.assertIn("Insufficient completed candles", result.reason)

    def test_duplicate_out_of_order_and_gapped_history_are_uncertain(self):
        candles = self.candles([100.0 + index * 0.1 for index in range(21)])
        duplicate = [*candles[:-1], candles[-2], candles[-1]]
        out_of_order = [candles[1], candles[0], *candles[2:]]
        gap = [*candles[:10], *candles[11:]]
        as_of = self.start + timedelta(minutes=22)

        for label, malformed in (
            ("duplicate", duplicate),
            ("out_of_order", out_of_order),
            ("gap", gap),
        ):
            with self.subTest(history=label):
                result = detect_market_regime(malformed, as_of=as_of)
                self.assertEqual(result.regime, MarketRegime.UNCERTAIN)
                self.assertIn("invalid", result.reason)

    def test_future_candle_does_not_change_metrics_or_classification(self):
        candles = self.candles([100.0 + index * 0.1 for index in range(21)])
        as_of = candles[-1].timestamp + timedelta(milliseconds=1)
        future = Bar(
            as_of + timedelta(minutes=1),
            100.0,
            100000.0,
            0.01,
            100000.0,
        )

        baseline = detect_market_regime(candles, as_of=as_of)
        result = detect_market_regime([*candles, future], as_of=as_of)

        self.assertEqual(result, baseline)


if __name__ == "__main__":
    unittest.main()
