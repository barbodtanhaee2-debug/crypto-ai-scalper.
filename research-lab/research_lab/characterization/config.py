"""Provisional configuration for Stage 3.3.1 market characterization.

All classification thresholds are **provisional research defaults**. They are
not empirically optimized against historical results and must not be treated as
tuned strategy parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Reuse frozen BacktestSettings cost defaults (paper research assumptions).
# Documented in backtester.BacktestSettings / Stage 3.2.4 design.
DEFAULT_FEE_RATE_PER_SIDE = 0.001
DEFAULT_SPREAD_FRACTION = 0.0002
DEFAULT_SLIPPAGE_RATE_PER_SIDE = 0.0005


@dataclass(frozen=True)
class FeatureConfig:
    """Lookbacks and warm-up for feature calculation (provisional)."""

    atr_period: int = 14
    realized_vol_period: int = 20
    efficiency_period: int = 20
    ema_fast: int = 8
    ema_slow: int = 21
    range_lookback: int = 20
    # Volume deviation is optional; disabled unless candles carry volume.
    volume_lookback: int = 20
    use_volume: bool = False

    def __post_init__(self) -> None:
        for name in (
            "atr_period",
            "realized_vol_period",
            "efficiency_period",
            "ema_fast",
            "ema_slow",
            "range_lookback",
            "volume_lookback",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 2:
                raise ValueError(f"{name} must be an integer >= 2")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be < ema_slow")

    @property
    def warmup_bars(self) -> int:
        """Minimum closed bars required before features may be finite."""

        return max(
            self.atr_period,
            self.realized_vol_period,
            self.efficiency_period,
            self.ema_slow,
            self.range_lookback,
            self.volume_lookback if self.use_volume else 0,
        ) + 1


@dataclass(frozen=True)
class PrimaryRegimeConfig:
    """Provisional thresholds for primary regime raw labels."""

    # Directional efficiency: |net move| / path length over efficiency_period.
    trend_efficiency_min: float = 0.35
    range_efficiency_max: float = 0.20
    # EMA structure: relative separation |ema_fast - ema_slow| / close.
    ema_separation_min: float = 0.0015
    # Range width (rolling high-low)/mid; ranging prefers bounded width.
    range_width_max: float = 0.025
    trend_range_width_min: float = 0.008


@dataclass(frozen=True)
class SecondaryConfig:
    """Provisional thresholds for secondary characteristics."""

    # Normalized ATR = ATR / close.
    high_natr_min: float = 0.004
    low_natr_max: float = 0.0015
    # Realized vol of log returns (std * sqrt(period)) / approximate scale.
    high_rvol_min: float = 0.012
    low_rvol_max: float = 0.004
    # Momentum: close vs ema_fast relative.
    strong_momentum_min: float = 0.002
    weak_momentum_max: float = 0.0005


@dataclass(frozen=True)
class StabilityConfig:
    """Regime confirmation / hysteresis (provisional)."""

    # Raw label must persist this many consecutive closes before adoption.
    min_persistence_bars: int = 3
    # Extra bars required to switch away from an established primary regime.
    hysteresis_bars: int = 2
    # Gap larger than this (in candle intervals) resets confirmed state.
    gap_reset_intervals: int = 2


@dataclass(frozen=True)
class OpportunityConfig:
    """Cost-adjusted opportunity filter using project cost assumptions."""

    fee_rate_per_side: float = DEFAULT_FEE_RATE_PER_SIDE
    spread_fraction: float = DEFAULT_SPREAD_FRACTION
    slippage_rate_per_side: float = DEFAULT_SLIPPAGE_RATE_PER_SIDE
    # Provisional: require net edge above this fraction of price after costs.
    minimum_net_edge_fraction: float = 0.001
    # Gross movement proxy: ATR * multiplier (not a forecast; research filter).
    atr_move_multiplier: float = 1.0

    def round_trip_cost_fraction(self) -> float:
        """Estimated round-trip cost as a fraction of mid/price.

        Matches paper research assumptions:
        - fees both sides
        - full synthetic spread once per round trip (mid ± half-spread each side)
        - slippage both sides
        """

        return (
            2.0 * self.fee_rate_per_side
            + self.spread_fraction
            + 2.0 * self.slippage_rate_per_side
        )


@dataclass(frozen=True)
class CharacterizationConfig:
    """Aggregate Stage 3.3.1 configuration."""

    candle_interval_seconds: int = 60
    features: FeatureConfig = field(default_factory=FeatureConfig)
    primary: PrimaryRegimeConfig = field(default_factory=PrimaryRegimeConfig)
    secondary: SecondaryConfig = field(default_factory=SecondaryConfig)
    stability: StabilityConfig = field(default_factory=StabilityConfig)
    opportunity: OpportunityConfig = field(default_factory=OpportunityConfig)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.candle_interval_seconds, int)
            or isinstance(self.candle_interval_seconds, bool)
            or self.candle_interval_seconds < 60
            or self.candle_interval_seconds % 60 != 0
        ):
            raise ValueError(
                "candle_interval_seconds must be a positive whole number of minutes"
            )
