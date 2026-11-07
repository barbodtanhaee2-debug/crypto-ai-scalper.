"""Stage 3.3.1 — Market characterization and cost-adjusted opportunity detection.

Lab-only research intelligence. Not an execution engine. Not a profitability claim.
"""

from __future__ import annotations

from research_lab.characterization.candles import CharacterizationCandle
from research_lab.characterization.config import (
    CharacterizationConfig,
    FeatureConfig,
    OpportunityConfig,
    PrimaryRegimeConfig,
    SecondaryConfig,
    StabilityConfig,
)
from research_lab.characterization.models import (
    DataQualityStatus,
    MarketCharacterization,
    MomentumLevel,
    OpportunityStatus,
    PrimaryRegime,
    VolatilityLevel,
)
from research_lab.characterization.pipeline import (
    MarketCharacterizationEngine,
    characterize,
)

__all__ = [
    "CharacterizationCandle",
    "CharacterizationConfig",
    "FeatureConfig",
    "OpportunityConfig",
    "PrimaryRegimeConfig",
    "SecondaryConfig",
    "StabilityConfig",
    "DataQualityStatus",
    "MarketCharacterization",
    "MomentumLevel",
    "OpportunityStatus",
    "PrimaryRegime",
    "VolatilityLevel",
    "MarketCharacterizationEngine",
    "characterize",
]
