"""Shared result models for Stage 3.3.1 market characterization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class PrimaryRegime(str, Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    TRANSITIONING = "TRANSITIONING"
    UNKNOWN = "UNKNOWN"


class VolatilityLevel(str, Enum):
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    NORMAL_VOLATILITY = "NORMAL_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    UNKNOWN = "UNKNOWN"


class MomentumLevel(str, Enum):
    STRONG_UP = "STRONG_UP"
    STRONG_DOWN = "STRONG_DOWN"
    WEAK = "WEAK"
    UNKNOWN = "UNKNOWN"


class DirectionalStrength(str, Enum):
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    UNKNOWN = "UNKNOWN"


class DataQualityStatus(str, Enum):
    OK = "OK"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    INVALID = "INVALID"
    GAP = "GAP"


class OpportunityStatus(str, Enum):
    OPPORTUNITY = "OPPORTUNITY"
    NO_OPPORTUNITY = "NO_OPPORTUNITY"
    REJECTED_COSTS_TOO_HIGH = "REJECTED_COSTS_TOO_HIGH"
    INCOMPLETE_COST_INFORMATION = "INCOMPLETE_COST_INFORMATION"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class FeatureSnapshot:
    """Feature values at a single candle close. None means unavailable."""

    atr: float | None
    normalized_atr: float | None
    realized_volatility: float | None
    directional_efficiency: float | None
    ema_fast: float | None
    ema_slow: float | None
    ema_separation: float | None
    rolling_high: float | None
    rolling_low: float | None
    range_width: float | None
    close: float | None
    volume_deviation: float | None
    non_finite_detected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "atr": self.atr,
            "normalized_atr": self.normalized_atr,
            "realized_volatility": self.realized_volatility,
            "directional_efficiency": self.directional_efficiency,
            "ema_fast": self.ema_fast,
            "ema_slow": self.ema_slow,
            "ema_separation": self.ema_separation,
            "rolling_high": self.rolling_high,
            "rolling_low": self.rolling_low,
            "range_width": self.range_width,
            "close": self.close,
            "volume_deviation": self.volume_deviation,
            "non_finite_detected": self.non_finite_detected,
        }


@dataclass(frozen=True)
class DataQualityReport:
    status: DataQualityStatus
    issues: tuple[str, ...] = ()
    gap_intervals: int = 0

    @property
    def ok(self) -> bool:
        return self.status == DataQualityStatus.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "issues": list(self.issues),
            "gap_intervals": self.gap_intervals,
        }


@dataclass(frozen=True)
class PrimaryClassification:
    regime: PrimaryRegime
    raw_regime: PrimaryRegime
    reason: str
    conflicting: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime.value,
            "raw_regime": self.raw_regime.value,
            "reason": self.reason,
            "conflicting": self.conflicting,
        }


@dataclass(frozen=True)
class SecondaryClassification:
    volatility: VolatilityLevel
    momentum: MomentumLevel
    directional_strength: DirectionalStrength
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "volatility": self.volatility.value,
            "momentum": self.momentum.value,
            "directional_strength": self.directional_strength.value,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class OpportunityAssessment:
    status: OpportunityStatus
    gross_move_fraction: float | None
    fee_cost_fraction: float | None
    spread_cost_fraction: float | None
    slippage_cost_fraction: float | None
    total_cost_fraction: float | None
    net_move_fraction: float | None
    assumptions: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "gross_move_fraction": self.gross_move_fraction,
            "fee_cost_fraction": self.fee_cost_fraction,
            "spread_cost_fraction": self.spread_cost_fraction,
            "slippage_cost_fraction": self.slippage_cost_fraction,
            "total_cost_fraction": self.total_cost_fraction,
            "net_move_fraction": self.net_move_fraction,
            "assumptions": dict(self.assumptions),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MarketCharacterization:
    """Full characterization at one candle close (no look-ahead)."""

    timestamp: datetime | None
    data_quality: DataQualityReport
    features: FeatureSnapshot | None
    primary: PrimaryClassification
    secondary: SecondaryClassification
    opportunity: OpportunityAssessment
    confirmed_regime: PrimaryRegime
    persistence_count: int
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": None if self.timestamp is None else self.timestamp.isoformat(),
            "data_quality": self.data_quality.to_dict(),
            "features": None if self.features is None else self.features.to_dict(),
            "primary": self.primary.to_dict(),
            "secondary": self.secondary.to_dict(),
            "opportunity": self.opportunity.to_dict(),
            "confirmed_regime": self.confirmed_regime.value,
            "persistence_count": self.persistence_count,
            "notes": list(self.notes),
        }
