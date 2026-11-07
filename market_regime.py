"""Informational BTC/USDT market-regime classification from completed candles."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Sequence

from backtester import validate_candles
from strategy import Bar

REGIME_LOOKBACK_RETURNS = 20
MINIMUM_COMPLETED_CANDLES = REGIME_LOOKBACK_RETURNS + 1
DEFAULT_INTERVAL_SECONDS = 60
CANDLE_CLOSE_TIME_MILLISECONDS = 1

HIGH_VOLATILITY_RMS_LOG_RETURN_THRESHOLD = 0.0035
TREND_NET_RETURN_THRESHOLD = 0.01
TREND_EFFICIENCY_RATIO_THRESHOLD = 0.60
SIDEWAYS_NET_RETURN_MAXIMUM = 0.003
SIDEWAYS_RANGE_FRACTION_MAXIMUM = 0.015
SIDEWAYS_EFFICIENCY_RATIO_MAXIMUM = 0.35


class MarketRegime(str, Enum):
    TRENDING = "TRENDING"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class RegimeAssessment:
    regime: MarketRegime
    reason: str
    completed_candles: int
    trend_direction: str | None = None
    net_return_fraction: float | None = None
    efficiency_ratio: float | None = None
    high_low_range_fraction: float | None = None
    rms_log_return_volatility: float | None = None


def _uncertain(reason: str, completed_candles: int = 0) -> RegimeAssessment:
    return RegimeAssessment(
        regime=MarketRegime.UNCERTAIN,
        reason=reason,
        completed_candles=completed_candles,
    )


def detect_market_regime(
    candles: Sequence[Bar],
    *,
    as_of: datetime,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
) -> RegimeAssessment:
    """Classify completed candles without influencing trading decisions.

    Bar timestamps are candle close-time timestamps. A candle is available only
    when its timestamp plus Binance's 1 ms close-time unit is at or before
    ``as_of``. Future and unfinished bars are excluded before candle validation.
    """
    if (
        not isinstance(as_of, datetime)
        or as_of.tzinfo is None
        or as_of.utcoffset() is None
    ):
        return _uncertain("as_of must be a timezone-aware timestamp.")
    observed_at = as_of.astimezone(timezone.utc)

    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, int)
        or interval_seconds < 60
        or interval_seconds % 60 != 0
    ):
        return _uncertain(
            "interval_seconds must be a positive whole number of minutes."
        )

    try:
        supplied = tuple(candles)
    except TypeError:
        return _uncertain("Candles must be a sequence of completed Bar values.")

    completed: list[Bar] = []
    close_time_unit = timedelta(milliseconds=CANDLE_CLOSE_TIME_MILLISECONDS)
    for index, candle in enumerate(supplied):
        if not isinstance(candle, Bar):
            return _uncertain(f"Candle {index} is not a Bar.", len(completed))
        timestamp = candle.timestamp
        if (
            not isinstance(timestamp, datetime)
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            return _uncertain(
                f"Candle {index} has a missing or invalid timezone-aware timestamp.",
                len(completed),
            )
        close_time = timestamp.astimezone(timezone.utc)
        if close_time + close_time_unit <= observed_at:
            completed.append(candle)

    if not completed:
        return _uncertain(
            "Insufficient completed candles: 0 available; "
            f"{MINIMUM_COMPLETED_CANDLES} required."
        )

    try:
        validated = validate_candles(
            completed,
            interval_seconds=interval_seconds,
        )
    except (TypeError, ValueError) as error:
        return _uncertain(
            f"Completed candle history is invalid: {error}",
            len(completed),
        )
    if len(validated) < MINIMUM_COMPLETED_CANDLES:
        return _uncertain(
            "Insufficient completed candles: "
            f"{len(validated)} available; {MINIMUM_COMPLETED_CANDLES} required.",
            len(validated),
        )

    closes = [candle.close for candle in validated[-MINIMUM_COMPLETED_CANDLES:]]
    net_return = closes[-1] / closes[0] - 1
    close_changes = [
        abs(current - previous)
        for previous, current in zip(closes, closes[1:])
    ]
    total_close_travel = sum(close_changes)
    efficiency_ratio = (
        abs(closes[-1] - closes[0]) / total_close_travel
        if total_close_travel > 0
        else 0.0
    )
    high_low_range = (
        max(candle.high for candle in validated[-MINIMUM_COMPLETED_CANDLES:])
        - min(candle.low for candle in validated[-MINIMUM_COMPLETED_CANDLES:])
    ) / closes[-1]
    log_returns = [
        math.log(current / previous)
        for previous, current in zip(closes, closes[1:])
    ]
    volatility = math.sqrt(
        sum(value * value for value in log_returns) / len(log_returns)
    )

    trend_qualified = (
        abs(net_return) >= TREND_NET_RETURN_THRESHOLD
        and efficiency_ratio >= TREND_EFFICIENCY_RATIO_THRESHOLD
    )
    trend_direction = (
        "UP" if net_return > 0 else "DOWN"
    ) if trend_qualified else None
    diagnostics = {
        "completed_candles": len(completed),
        "net_return_fraction": net_return,
        "efficiency_ratio": efficiency_ratio,
        "high_low_range_fraction": high_low_range,
        "rms_log_return_volatility": volatility,
    }

    if volatility >= HIGH_VOLATILITY_RMS_LOG_RETURN_THRESHOLD:
        regime = MarketRegime.HIGH_VOLATILITY
        reason = "RMS log-return volatility meets the high-volatility threshold."
    elif trend_qualified:
        regime = MarketRegime.TRENDING
        reason = (
            f"{trend_direction.lower()} trend meets the net-return and "
            "efficiency thresholds."
        )
    elif (
        abs(net_return) <= SIDEWAYS_NET_RETURN_MAXIMUM
        and high_low_range <= SIDEWAYS_RANGE_FRACTION_MAXIMUM
        and efficiency_ratio <= SIDEWAYS_EFFICIENCY_RATIO_MAXIMUM
    ):
        regime = MarketRegime.SIDEWAYS
        reason = "Net return, range, and efficiency meet sideways thresholds."
    else:
        regime = MarketRegime.UNCERTAIN
        reason = "Evidence does not meet a confident regime classification."

    return RegimeAssessment(
        regime=regime,
        reason=reason,
        trend_direction=trend_direction,
        **diagnostics,
    )
