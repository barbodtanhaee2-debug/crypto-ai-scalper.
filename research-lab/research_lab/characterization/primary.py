"""Primary regime classification (TRENDING / RANGING / TRANSITIONING / UNKNOWN).

Volatility expansion/compression is intentionally **not** decided here.
"""

from __future__ import annotations

from research_lab.characterization.config import PrimaryRegimeConfig
from research_lab.characterization.models import (
    FeatureSnapshot,
    PrimaryClassification,
    PrimaryRegime,
)


def classify_primary_raw(
    features: FeatureSnapshot | None,
    *,
    config: PrimaryRegimeConfig | None = None,
) -> PrimaryClassification:
    """Deterministic raw primary label from features at the current close."""

    cfg = config or PrimaryRegimeConfig()
    if features is None or features.non_finite_detected:
        return PrimaryClassification(
            regime=PrimaryRegime.UNKNOWN,
            raw_regime=PrimaryRegime.UNKNOWN,
            reason="missing_or_non_finite_features",
        )

    efficiency = features.directional_efficiency
    sep = features.ema_separation
    width = features.range_width
    ema_fast = features.ema_fast
    ema_slow = features.ema_slow

    if efficiency is None or sep is None or width is None:
        return PrimaryClassification(
            regime=PrimaryRegime.UNKNOWN,
            raw_regime=PrimaryRegime.UNKNOWN,
            reason="insufficient_feature_coverage",
        )

    trend_like = (
        efficiency >= cfg.trend_efficiency_min
        and sep >= cfg.ema_separation_min
        and width >= cfg.trend_range_width_min
        and ema_fast is not None
        and ema_slow is not None
        and ema_fast != ema_slow
    )
    range_like = (
        efficiency <= cfg.range_efficiency_max
        and width <= cfg.range_width_max
    )

    if trend_like and range_like:
        return PrimaryClassification(
            regime=PrimaryRegime.TRANSITIONING,
            raw_regime=PrimaryRegime.TRANSITIONING,
            reason="conflicting_trend_and_range_evidence",
            conflicting=True,
        )
    if trend_like:
        return PrimaryClassification(
            regime=PrimaryRegime.TRENDING,
            raw_regime=PrimaryRegime.TRENDING,
            reason="efficiency_and_ema_structure_support_trend",
        )
    if range_like:
        return PrimaryClassification(
            regime=PrimaryRegime.RANGING,
            raw_regime=PrimaryRegime.RANGING,
            reason="low_efficiency_and_bounded_range_width",
        )

    # Intermediate: neither clearly trending nor ranging.
    return PrimaryClassification(
        regime=PrimaryRegime.TRANSITIONING,
        raw_regime=PrimaryRegime.TRANSITIONING,
        reason="intermediate_or_unstable_structure",
    )
