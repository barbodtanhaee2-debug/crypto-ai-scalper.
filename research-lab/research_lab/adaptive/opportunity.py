"""Cost-adjusted opportunity representation (research filter contract).

Reuses BacktestSettings default cost *field meanings* (fee / spread / slip).
Does not change PaperBroker economics. Gross proxies are not forecasts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from research_lab.adaptive.enums import OpportunityAssessmentStatus
from research_lab.adaptive.immutability import freeze_mapping, mapping_to_dict
from research_lab.adaptive.versioning import CANDIDATE_CONTRACT_VERSION

# Documented defaults aligned with BacktestSettings (not a second fee model).
DEFAULT_FEE_RATE_PER_SIDE = 0.001
DEFAULT_SPREAD_FRACTION = 0.0002
DEFAULT_SLIPPAGE_RATE_PER_SIDE = 0.0005


def _require_bool(value: Any, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a boolean, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class CostAssumptions:
    fee_rate_per_side: float = DEFAULT_FEE_RATE_PER_SIDE
    spread_fraction: float = DEFAULT_SPREAD_FRACTION
    slippage_rate_per_side: float = DEFAULT_SLIPPAGE_RATE_PER_SIDE
    costs_complete: bool = True
    source: str = "BacktestSettings_defaults"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "costs_complete",
            _require_bool(self.costs_complete, field_name="costs_complete"),
        )
        for name in (
            "fee_rate_per_side",
            "spread_fraction",
            "slippage_rate_per_side",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError(f"{name} must be a finite non-negative number")

    def round_trip_cost_fraction(self) -> float | None:
        if not self.costs_complete:
            return None
        return (
            2.0 * float(self.fee_rate_per_side)
            + float(self.spread_fraction)
            + 2.0 * float(self.slippage_rate_per_side)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fee_rate_per_side": self.fee_rate_per_side,
            "spread_fraction": self.spread_fraction,
            "slippage_rate_per_side": self.slippage_rate_per_side,
            "costs_complete": self.costs_complete,
            "source": self.source,
            "round_trip_cost_fraction": self.round_trip_cost_fraction(),
        }


@dataclass(frozen=True)
class CostAdjustedOpportunity:
    """Separates gross proxy, cost components, net estimate, and completeness."""

    status: OpportunityAssessmentStatus
    gross_move_fraction: float | None
    fee_cost_fraction: float | None
    spread_cost_fraction: float | None
    slippage_cost_fraction: float | None
    total_cost_fraction: float | None
    net_move_fraction: float | None
    cost_assumptions: CostAssumptions = field(default_factory=CostAssumptions)
    gross_proxy_label: str = "unspecified_proxy"
    is_forecast: bool = False
    uses_historical_mfe_as_guarantee: bool = False
    reason: str = ""
    extensions: Mapping[str, Any] = field(default_factory=dict)
    contract_version: str = CANDIDATE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "extensions", freeze_mapping(self.extensions))
        object.__setattr__(
            self,
            "is_forecast",
            _require_bool(self.is_forecast, field_name="is_forecast"),
        )
        object.__setattr__(
            self,
            "uses_historical_mfe_as_guarantee",
            _require_bool(
                self.uses_historical_mfe_as_guarantee,
                field_name="uses_historical_mfe_as_guarantee",
            ),
        )
        from research_lab.adaptive.validation import validate_opportunity_dict

        errors = validate_opportunity_dict(self.to_dict())
        if errors:
            raise ValueError("; ".join(errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "gross_move_fraction": self.gross_move_fraction,
            "fee_cost_fraction": self.fee_cost_fraction,
            "spread_cost_fraction": self.spread_cost_fraction,
            "slippage_cost_fraction": self.slippage_cost_fraction,
            "total_cost_fraction": self.total_cost_fraction,
            "net_move_fraction": self.net_move_fraction,
            "cost_assumptions": self.cost_assumptions.to_dict(),
            "gross_proxy_label": self.gross_proxy_label,
            "is_forecast": self.is_forecast,
            "uses_historical_mfe_as_guarantee": self.uses_historical_mfe_as_guarantee,
            "reason": self.reason,
            "extensions": mapping_to_dict(self.extensions),
        }
