"""Secondary overlapping characteristics (volatility, momentum, strength)."""

from __future__ import annotations

from research_lab.characterization.config import SecondaryConfig
from research_lab.characterization.models import (
    DirectionalStrength,
    FeatureSnapshot,
    MomentumLevel,
    SecondaryClassification,
    VolatilityLevel,
)


def classify_secondary(
    features: FeatureSnapshot | None,
    *,
    config: SecondaryConfig | None = None,
) -> SecondaryClassification:
    """Independent secondary labels; may coexist with any primary regime."""

    cfg = config or SecondaryConfig()
    reasons: list[str] = []

    if features is None or features.non_finite_detected:
        return SecondaryClassification(
            volatility=VolatilityLevel.UNKNOWN,
            momentum=MomentumLevel.UNKNOWN,
            directional_strength=DirectionalStrength.UNKNOWN,
            reasons=("missing_or_non_finite_features",),
        )

    natr = features.normalized_atr
    rvol = features.realized_volatility
    efficiency = features.directional_efficiency
    close = features.close
    ema_fast = features.ema_fast

    # Volatility: NATR is primary; realized vol confirms or conflicts.
    # True conflict = NATR high while RVOL compressed, or NATR low while RVOL expanded.
    vol = VolatilityLevel.UNKNOWN
    if natr is not None and rvol is not None:
        high_n = natr >= cfg.high_natr_min
        low_n = natr <= cfg.low_natr_max
        high_r = rvol >= cfg.high_rvol_min
        low_r = rvol <= cfg.low_rvol_max
        if high_n and low_r:
            vol = VolatilityLevel.UNKNOWN
            reasons.append("conflicting_volatility_evidence")
        elif low_n and high_r:
            vol = VolatilityLevel.UNKNOWN
            reasons.append("conflicting_volatility_evidence")
        elif high_n:
            vol = VolatilityLevel.HIGH_VOLATILITY
            reasons.append("natr_high")
        elif low_n:
            vol = VolatilityLevel.LOW_VOLATILITY
            reasons.append("natr_low")
        elif high_r:
            vol = VolatilityLevel.HIGH_VOLATILITY
            reasons.append("rvol_high_natr_normal")
        elif low_r:
            vol = VolatilityLevel.LOW_VOLATILITY
            reasons.append("rvol_low_natr_normal")
        else:
            vol = VolatilityLevel.NORMAL_VOLATILITY
            reasons.append("natr_rvol_intermediate")
    elif natr is not None:
        if natr >= cfg.high_natr_min:
            vol = VolatilityLevel.HIGH_VOLATILITY
            reasons.append("natr_high_only")
        elif natr <= cfg.low_natr_max:
            vol = VolatilityLevel.LOW_VOLATILITY
            reasons.append("natr_low_only")
        else:
            vol = VolatilityLevel.NORMAL_VOLATILITY
            reasons.append("natr_normal_only")
    else:
        reasons.append("volatility_unavailable")

    # Momentum from close vs ema_fast.
    momentum = MomentumLevel.UNKNOWN
    if close is not None and ema_fast is not None and close > 0:
        rel = (close - ema_fast) / close
        if rel >= cfg.strong_momentum_min:
            momentum = MomentumLevel.STRONG_UP
            reasons.append("close_above_ema_fast")
        elif rel <= -cfg.strong_momentum_min:
            momentum = MomentumLevel.STRONG_DOWN
            reasons.append("close_below_ema_fast")
        elif abs(rel) <= cfg.weak_momentum_max:
            momentum = MomentumLevel.WEAK
            reasons.append("close_near_ema_fast")
        else:
            momentum = MomentumLevel.WEAK
            reasons.append("moderate_momentum_bucketed_as_weak")
    else:
        reasons.append("momentum_unavailable")

    # Directional strength from efficiency.
    strength = DirectionalStrength.UNKNOWN
    if efficiency is not None:
        if efficiency >= 0.45:
            strength = DirectionalStrength.STRONG
        elif efficiency >= 0.25:
            strength = DirectionalStrength.MODERATE
        else:
            strength = DirectionalStrength.WEAK
        reasons.append(f"efficiency={efficiency:.6f}")
    else:
        reasons.append("directional_strength_unavailable")

    return SecondaryClassification(
        volatility=vol,
        momentum=momentum,
        directional_strength=strength,
        reasons=tuple(reasons),
    )
