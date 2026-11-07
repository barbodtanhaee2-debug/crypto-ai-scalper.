"""Explicit data-quality checks for characterization inputs.

Does not repair or fabricate candles. Reports issues for the caller to fail closed.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from research_lab.characterization.candles import CandleSeries, CharacterizationCandle
from research_lab.characterization.config import CharacterizationConfig
from research_lab.characterization.models import DataQualityReport, DataQualityStatus


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def validate_candle_series(
    candles: CandleSeries,
    *,
    config: CharacterizationConfig,
    require_warmup: bool = True,
    require_volume: bool | None = None,
) -> DataQualityReport:
    """Validate a closed-candle series ending at the decision bar.

    Checks: emptiness, timestamps, monotonicity, duplicates, OHLC validity,
    non-positive prices, gaps, optional volume, warm-up length.
    """

    issues: list[str] = []
    gap_intervals = 0
    interval = config.candle_interval_seconds
    need_volume = config.features.use_volume if require_volume is None else require_volume

    if not candles:
        return DataQualityReport(
            status=DataQualityStatus.INSUFFICIENT_DATA,
            issues=("empty candle series",),
        )

    previous: CharacterizationCandle | None = None
    previous_ts: datetime | None = None
    seen: set[datetime] = set()

    for index, candle in enumerate(candles):
        if not isinstance(candle, CharacterizationCandle):
            issues.append(f"index {index}: not a CharacterizationCandle")
            continue
        ts = _aware_utc(candle.timestamp)
        if ts is None:
            issues.append(f"index {index}: missing or naive timestamp")
            continue
        if ts in seen:
            issues.append(f"index {index}: duplicate timestamp {ts.isoformat()}")
        seen.add(ts)

        values = (candle.open, candle.high, candle.low, candle.close)
        if not all(
            isinstance(v, (int, float))
            and not isinstance(v, bool)
            and math.isfinite(v)
            and v > 0
            for v in values
        ):
            issues.append(f"index {index}: non-positive or non-finite OHLC")
        elif (
            candle.low > min(candle.open, candle.close)
            or candle.high < max(candle.open, candle.close)
            or candle.high < candle.low
        ):
            issues.append(f"index {index}: invalid OHLC relationship")

        if need_volume:
            vol = candle.volume
            if vol is None or not isinstance(vol, (int, float)) or isinstance(vol, bool):
                issues.append(f"index {index}: missing volume")
            elif not math.isfinite(vol) or vol < 0:
                issues.append(f"index {index}: invalid volume")

        if previous_ts is not None:
            elapsed = (ts - previous_ts).total_seconds()
            if elapsed <= 0:
                issues.append(
                    f"index {index}: non-monotonic timestamp "
                    f"{ts.isoformat()} after {previous_ts.isoformat()}"
                )
            else:
                steps = elapsed / interval
                if abs(steps - round(steps)) > 1e-9:
                    issues.append(
                        f"index {index}: irregular interval "
                        f"{elapsed}s (expected multiple of {interval}s)"
                    )
                else:
                    gap = int(round(steps)) - 1
                    if gap > 0:
                        gap_intervals = max(gap_intervals, gap)
                        issues.append(
                            f"index {index}: missing {gap} candle interval(s) "
                            f"before {ts.isoformat()}"
                        )
        previous = candle
        previous_ts = ts

    if require_warmup and len(candles) < config.features.warmup_bars:
        issues.append(
            f"insufficient warm-up: have {len(candles)} bars, "
            f"need {config.features.warmup_bars}"
        )

    if any("invalid OHLC" in i or "non-positive" in i or "duplicate" in i or "non-monotonic" in i or "naive" in i or "not a CharacterizationCandle" in i or "irregular" in i for i in issues):
        status = DataQualityStatus.INVALID
    elif any("missing" in i and "candle interval" in i for i in issues):
        status = DataQualityStatus.GAP
    elif any("warm-up" in i or "empty" in i for i in issues):
        status = DataQualityStatus.INSUFFICIENT_DATA
    elif issues:
        status = DataQualityStatus.INVALID
    else:
        status = DataQualityStatus.OK

    return DataQualityReport(
        status=status,
        issues=tuple(issues),
        gap_intervals=gap_intervals,
    )
