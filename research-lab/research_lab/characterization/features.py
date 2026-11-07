"""Deterministic feature calculation at candle close (no look-ahead).

Feature definitions (Stage 3.3.1)
--------------------------------
All features use only bars ``candles[0:i+1]`` when evaluating index ``i``.

### ATR (Average True Range)
- Definition: Wilder-style smoothed true range.
- True range_t = max(high-low, |high-prev_close|, |low-prev_close|)
- ATR_n = Wilder RMA of true range over ``atr_period``.
- Inputs: OHLC; warm-up >= atr_period + 1 closes.
- Units: price.
- Missing: None if insufficient history or non-finite inputs.

### Normalized ATR
- ATR / close. Units: fraction of price.

### Realized volatility
- StdDev of log returns over ``realized_vol_period``, annualization omitted
  (bar-scale std). Units: dimensionless log-return std.
- Formula: std(ln(c_t/c_{t-1})) for the last N returns.

### Directional efficiency
- |close - close_N| / sum(|delta close|) over ``efficiency_period`` steps.
- Units: [0, 1]. High => persistent direction; low => choppy path.

### EMA fast / slow
- Standard EMA: alpha = 2/(period+1), seeded with SMA of first ``period`` closes.
- ema_separation = |ema_fast - ema_slow| / close.

### Rolling high/low / range width
- max(high), min(low) over ``range_lookback`` bars including current.
- range_width = (high-low) / mid, mid = (high+low)/2.

### Volume deviation (optional)
- volume / SMA(volume, volume_lookback) - 1 when ``use_volume`` and volumes present.
"""

from __future__ import annotations

import math
from typing import Sequence

from research_lab.characterization.candles import CharacterizationCandle
from research_lab.characterization.config import FeatureConfig
from research_lab.characterization.models import FeatureSnapshot


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _true_ranges(candles: Sequence[CharacterizationCandle]) -> list[float | None]:
    out: list[float | None] = []
    prev_close: float | None = None
    for candle in candles:
        high = _finite(candle.high)
        low = _finite(candle.low)
        close = _finite(candle.close)
        if high is None or low is None or close is None or high < low:
            out.append(None)
            prev_close = close
            continue
        ranges = [high - low]
        if prev_close is not None:
            ranges.append(abs(high - prev_close))
            ranges.append(abs(low - prev_close))
        out.append(max(ranges))
        prev_close = close
    return out


def _wilder_rma(values: Sequence[float | None], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    window: list[float] = []
    rma: float | None = None
    for i, value in enumerate(values):
        if value is None or not math.isfinite(value):
            window = []
            rma = None
            continue
        if rma is None:
            window.append(value)
            if len(window) == period:
                rma = sum(window) / period
                out[i] = rma
            continue
        rma = (rma * (period - 1) + value) / period
        out[i] = rma
    return out


def _ema(closes: Sequence[float | None], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    alpha = 2.0 / (period + 1)
    seed: list[float] = []
    ema: float | None = None
    for i, close in enumerate(closes):
        if close is None or not math.isfinite(close):
            seed = []
            ema = None
            continue
        if ema is None:
            seed.append(close)
            if len(seed) == period:
                ema = sum(seed) / period
                out[i] = ema
            continue
        ema = alpha * close + (1.0 - alpha) * ema
        out[i] = ema
    return out


def _log_returns(closes: Sequence[float | None]) -> list[float | None]:
    out: list[float | None] = [None]
    for i in range(1, len(closes)):
        a = closes[i - 1]
        b = closes[i]
        if a is None or b is None or a <= 0 or b <= 0:
            out.append(None)
            continue
        out.append(math.log(b / a))
    return out


def _rolling_std(values: Sequence[float | None], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(len(values)):
        if i + 1 < period:
            continue
        window = values[i + 1 - period : i + 1]
        if any(v is None or not math.isfinite(v) for v in window):
            continue
        nums = [float(v) for v in window]  # type: ignore[arg-type]
        mean = sum(nums) / period
        var = sum((x - mean) ** 2 for x in nums) / period
        out[i] = math.sqrt(var)
    return out


def _directional_efficiency(
    closes: Sequence[float | None], period: int
) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        if i < period:
            continue
        start = closes[i - period]
        end = closes[i]
        if start is None or end is None or start <= 0 or end <= 0:
            continue
        path = 0.0
        valid = True
        for j in range(i - period + 1, i + 1):
            a = closes[j - 1]
            b = closes[j]
            if a is None or b is None:
                valid = False
                break
            path += abs(b - a)
        if not valid or path <= 0:
            if path == 0 and start == end:
                out[i] = 0.0
            continue
        out[i] = abs(end - start) / path
    return out


def compute_feature_series(
    candles: Sequence[CharacterizationCandle],
    *,
    config: FeatureConfig | None = None,
) -> list[FeatureSnapshot]:
    """Compute feature snapshots for every index (warm-up yields None fields)."""

    cfg = config or FeatureConfig()
    closes = [_finite(c.close) for c in candles]
    highs = [_finite(c.high) for c in candles]
    lows = [_finite(c.low) for c in candles]
    tr = _true_ranges(candles)
    atr = _wilder_rma(tr, cfg.atr_period)
    ema_fast = _ema(closes, cfg.ema_fast)
    ema_slow = _ema(closes, cfg.ema_slow)
    rvol = _rolling_std(_log_returns(closes), cfg.realized_vol_period)
    efficiency = _directional_efficiency(closes, cfg.efficiency_period)

    snapshots: list[FeatureSnapshot] = []
    for i, candle in enumerate(candles):
        non_finite = False
        atr_i = atr[i]
        close_i = closes[i]
        natr = None
        if atr_i is not None and close_i is not None and close_i > 0:
            natr = atr_i / close_i
            if not math.isfinite(natr):
                natr = None
                non_finite = True

        roll_high = None
        roll_low = None
        range_width = None
        if i + 1 >= cfg.range_lookback:
            window_h = highs[i + 1 - cfg.range_lookback : i + 1]
            window_l = lows[i + 1 - cfg.range_lookback : i + 1]
            if all(v is not None for v in window_h) and all(
                v is not None for v in window_l
            ):
                roll_high = max(window_h)  # type: ignore[type-var]
                roll_low = min(window_l)  # type: ignore[type-var]
                mid = (roll_high + roll_low) / 2.0
                if mid > 0 and math.isfinite(mid):
                    range_width = (roll_high - roll_low) / mid
                    if not math.isfinite(range_width):
                        range_width = None
                        non_finite = True

        ema_f = ema_fast[i]
        ema_s = ema_slow[i]
        sep = None
        if (
            ema_f is not None
            and ema_s is not None
            and close_i is not None
            and close_i > 0
        ):
            sep = abs(ema_f - ema_s) / close_i
            if not math.isfinite(sep):
                sep = None
                non_finite = True

        vol_dev = None
        if cfg.use_volume and i + 1 >= cfg.volume_lookback:
            vols = []
            ok = True
            for c in candles[i + 1 - cfg.volume_lookback : i + 1]:
                v = c.volume
                if v is None or not isinstance(v, (int, float)) or not math.isfinite(v):
                    ok = False
                    break
                vols.append(float(v))
            if ok and vols:
                mean_v = sum(vols) / len(vols)
                if mean_v > 0:
                    vol_dev = vols[-1] / mean_v - 1.0
                    if not math.isfinite(vol_dev):
                        vol_dev = None
                        non_finite = True

        for value in (
            atr_i,
            natr,
            rvol[i],
            efficiency[i],
            ema_f,
            ema_s,
            sep,
            roll_high,
            roll_low,
            range_width,
            close_i,
            vol_dev,
        ):
            if value is not None and not math.isfinite(value):
                non_finite = True

        snapshots.append(
            FeatureSnapshot(
                atr=atr_i if atr_i is not None and math.isfinite(atr_i) else None,
                normalized_atr=natr,
                realized_volatility=(
                    rvol[i]
                    if rvol[i] is not None and math.isfinite(rvol[i])  # type: ignore[arg-type]
                    else None
                ),
                directional_efficiency=(
                    efficiency[i]
                    if efficiency[i] is not None
                    and math.isfinite(efficiency[i])  # type: ignore[arg-type]
                    else None
                ),
                ema_fast=ema_f if ema_f is not None and math.isfinite(ema_f) else None,
                ema_slow=ema_s if ema_s is not None and math.isfinite(ema_s) else None,
                ema_separation=sep,
                rolling_high=roll_high,
                rolling_low=roll_low,
                range_width=range_width,
                close=close_i,
                volume_deviation=vol_dev,
                non_finite_detected=non_finite,
            )
        )
    return snapshots


def compute_features_at_close(
    candles: Sequence[CharacterizationCandle],
    *,
    config: FeatureConfig | None = None,
) -> FeatureSnapshot:
    """Features for the last candle only (decision at that close)."""

    series = compute_feature_series(candles, config=config)
    if not series:
        return FeatureSnapshot(
            atr=None,
            normalized_atr=None,
            realized_volatility=None,
            directional_efficiency=None,
            ema_fast=None,
            ema_slow=None,
            ema_separation=None,
            rolling_high=None,
            rolling_low=None,
            range_width=None,
            close=None,
            volume_deviation=None,
            non_finite_detected=False,
        )
    return series[-1]
