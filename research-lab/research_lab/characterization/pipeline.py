"""Orchestrate characterization at each candle close (deterministic, no look-ahead)."""

from __future__ import annotations

from research_lab.characterization.candles import CandleSeries, CharacterizationCandle
from research_lab.characterization.config import CharacterizationConfig
from research_lab.characterization.data_quality import validate_candle_series
from research_lab.characterization.features import compute_feature_series
from research_lab.characterization.models import (
    DataQualityReport,
    DataQualityStatus,
    MarketCharacterization,
    MomentumLevel,
    OpportunityAssessment,
    OpportunityStatus,
    PrimaryClassification,
    PrimaryRegime,
    SecondaryClassification,
    DirectionalStrength,
    VolatilityLevel,
)
from research_lab.characterization.opportunity import assess_opportunity
from research_lab.characterization.primary import classify_primary_raw
from research_lab.characterization.secondary import classify_secondary
from research_lab.characterization.stability import RegimeStabilityTracker


def _unknown_bundle(
    *,
    timestamp,
    quality: DataQualityReport,
    notes: tuple[str, ...],
    confirmed: PrimaryRegime = PrimaryRegime.UNKNOWN,
    persistence: int = 0,
) -> MarketCharacterization:
    return MarketCharacterization(
        timestamp=timestamp,
        data_quality=quality,
        features=None,
        primary=PrimaryClassification(
            regime=PrimaryRegime.UNKNOWN,
            raw_regime=PrimaryRegime.UNKNOWN,
            reason="data_quality_block",
        ),
        secondary=SecondaryClassification(
            volatility=VolatilityLevel.UNKNOWN,
            momentum=MomentumLevel.UNKNOWN,
            directional_strength=DirectionalStrength.UNKNOWN,
            reasons=("data_quality_block",),
        ),
        opportunity=OpportunityAssessment(
            status=OpportunityStatus.UNAVAILABLE,
            gross_move_fraction=None,
            fee_cost_fraction=None,
            spread_cost_fraction=None,
            slippage_cost_fraction=None,
            total_cost_fraction=None,
            net_move_fraction=None,
            reason="data_quality_block",
        ),
        confirmed_regime=confirmed,
        persistence_count=persistence,
        notes=notes,
    )


class MarketCharacterizationEngine:
    """Sequential evaluator: one output per closed candle, no retrospective edits."""

    def __init__(self, config: CharacterizationConfig | None = None) -> None:
        self.config = config or CharacterizationConfig()
        self.stability = RegimeStabilityTracker(config=self.config.stability)
        self._history: list[MarketCharacterization] = []

    def reset(self) -> None:
        self.stability.reset()
        self._history.clear()

    @property
    def history(self) -> tuple[MarketCharacterization, ...]:
        return tuple(self._history)

    def evaluate_series(
        self,
        candles: CandleSeries,
        *,
        costs_available: bool = True,
    ) -> list[MarketCharacterization]:
        """Evaluate each prefix candles[:k+1] at close k (explicit no-look-ahead)."""

        self.reset()
        results: list[MarketCharacterization] = []
        if not candles:
            return results

        # Precompute features on the full series is OK only if each snapshot at
        # index i uses candles[0:i+1] exclusively — compute_feature_series does.
        feature_series = compute_feature_series(
            candles, config=self.config.features
        )

        for index in range(len(candles)):
            prefix = candles[: index + 1]
            candle = candles[index]
            structural = validate_candle_series(
                prefix,
                config=self.config,
                require_warmup=False,
            )

            if structural.status == DataQualityStatus.INVALID:
                result = _unknown_bundle(
                    timestamp=candle.timestamp,
                    quality=structural,
                    notes=("invalid_input_fail_closed",),
                    confirmed=self.stability.confirmed,
                    persistence=self.stability.persistence_count,
                )
                results.append(result)
                self._history.append(result)
                continue

            if structural.gap_intervals >= self.config.stability.gap_reset_intervals:
                self.stability.on_gap(structural.gap_intervals)

            if index + 1 < self.config.features.warmup_bars:
                insuff = DataQualityReport(
                    status=DataQualityStatus.INSUFFICIENT_DATA,
                    issues=(
                        f"insufficient warm-up: have {index + 1} bars, "
                        f"need {self.config.features.warmup_bars}",
                    ),
                    gap_intervals=structural.gap_intervals,
                )
                result = _unknown_bundle(
                    timestamp=candle.timestamp,
                    quality=insuff,
                    notes=("warmup",),
                    confirmed=self.stability.confirmed,
                    persistence=self.stability.persistence_count,
                )
                results.append(result)
                self._history.append(result)
                continue

            features = feature_series[index]
            if features.non_finite_detected:
                result = _unknown_bundle(
                    timestamp=candle.timestamp,
                    quality=DataQualityReport(
                        status=DataQualityStatus.INVALID,
                        issues=("non_finite_feature_calculation",),
                        gap_intervals=structural.gap_intervals,
                    ),
                    notes=("non_finite_features",),
                    confirmed=self.stability.confirmed,
                    persistence=self.stability.persistence_count,
                )
                results.append(result)
                self._history.append(result)
                continue

            primary_raw = classify_primary_raw(
                features, config=self.config.primary
            )
            confirmed, persistence = self.stability.update(primary_raw.raw_regime)
            if confirmed != PrimaryRegime.UNKNOWN:
                primary = PrimaryClassification(
                    regime=confirmed,
                    raw_regime=primary_raw.raw_regime,
                    reason=primary_raw.reason,
                    conflicting=primary_raw.conflicting,
                )
            elif primary_raw.raw_regime == PrimaryRegime.UNKNOWN:
                primary = primary_raw
            else:
                # Await persistence before adopting TRENDING/RANGING/TRANSITIONING.
                primary = PrimaryClassification(
                    regime=PrimaryRegime.TRANSITIONING,
                    raw_regime=primary_raw.raw_regime,
                    reason="awaiting_persistence_confirmation",
                    conflicting=primary_raw.conflicting,
                )

            secondary = classify_secondary(
                features, config=self.config.secondary
            )
            opportunity = assess_opportunity(
                features,
                config=self.config.opportunity,
                costs_available=costs_available,
            )

            if structural.status == DataQualityStatus.GAP:
                dq = structural
            else:
                dq = DataQualityReport(
                    status=DataQualityStatus.OK,
                    issues=(),
                    gap_intervals=structural.gap_intervals,
                )

            result = MarketCharacterization(
                timestamp=candle.timestamp,
                data_quality=dq,
                features=features,
                primary=primary,
                secondary=secondary,
                opportunity=opportunity,
                confirmed_regime=confirmed,
                persistence_count=persistence,
                notes=(),
            )
            results.append(result)
            self._history.append(result)

        return results

    def evaluate_at_close(
        self,
        candles: CandleSeries,
        *,
        costs_available: bool = True,
    ) -> MarketCharacterization:
        """Characterize the final candle using the full provided history prefix."""

        series = self.evaluate_series(candles, costs_available=costs_available)
        if not series:
            return _unknown_bundle(
                timestamp=None,
                quality=DataQualityReport(
                    status=DataQualityStatus.INSUFFICIENT_DATA,
                    issues=("empty candle series",),
                ),
                notes=("empty",),
            )
        return series[-1]


def characterize(
    candles: CandleSeries,
    *,
    config: CharacterizationConfig | None = None,
    costs_available: bool = True,
) -> list[MarketCharacterization]:
    """Functional helper: fresh engine over the series."""

    return MarketCharacterizationEngine(config=config).evaluate_series(
        candles, costs_available=costs_available
    )
