"""Cost-adjusted opportunity assessment (research filter only).

Reuses paper BacktestSettings cost assumptions:
- fee_rate_per_side (default 0.001)
- spread_fraction (default 0.0002)
- slippage_rate_per_side (default 0.0005)

Gross movement is a **proxy** from ATR * multiplier, not a forecast and not
historical MFE. This module does not generate orders.
"""

from __future__ import annotations

import math

from research_lab.characterization.config import OpportunityConfig
from research_lab.characterization.models import (
    FeatureSnapshot,
    OpportunityAssessment,
    OpportunityStatus,
)


def assess_opportunity(
    features: FeatureSnapshot | None,
    *,
    config: OpportunityConfig | None = None,
    costs_available: bool = True,
) -> OpportunityAssessment:
    """Assess whether a proxied gross move clears estimated round-trip costs."""

    cfg = config or OpportunityConfig()
    assumptions = {
        "fee_rate_per_side": cfg.fee_rate_per_side,
        "spread_fraction": cfg.spread_fraction,
        "slippage_rate_per_side": cfg.slippage_rate_per_side,
        "round_trip_cost_fraction": cfg.round_trip_cost_fraction(),
        "atr_move_multiplier": cfg.atr_move_multiplier,
        "minimum_net_edge_fraction": cfg.minimum_net_edge_fraction,
        "gross_move_is_forecast": False,
        "uses_historical_mfe": False,
        "note": (
            "Gross move = normalized_atr * atr_move_multiplier; "
            "research filter only, not predictive."
        ),
    }

    if not costs_available:
        return OpportunityAssessment(
            status=OpportunityStatus.INCOMPLETE_COST_INFORMATION,
            gross_move_fraction=None,
            fee_cost_fraction=None,
            spread_cost_fraction=None,
            slippage_cost_fraction=None,
            total_cost_fraction=None,
            net_move_fraction=None,
            assumptions=assumptions,
            reason="required_cost_components_unavailable",
        )

    for name, value in (
        ("fee_rate_per_side", cfg.fee_rate_per_side),
        ("spread_fraction", cfg.spread_fraction),
        ("slippage_rate_per_side", cfg.slippage_rate_per_side),
    ):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            return OpportunityAssessment(
                status=OpportunityStatus.INCOMPLETE_COST_INFORMATION,
                gross_move_fraction=None,
                fee_cost_fraction=None,
                spread_cost_fraction=None,
                slippage_cost_fraction=None,
                total_cost_fraction=None,
                net_move_fraction=None,
                assumptions=assumptions,
                reason=f"invalid_cost_component:{name}",
            )

    if features is None or features.normalized_atr is None or features.non_finite_detected:
        return OpportunityAssessment(
            status=OpportunityStatus.UNAVAILABLE,
            gross_move_fraction=None,
            fee_cost_fraction=2.0 * cfg.fee_rate_per_side,
            spread_cost_fraction=cfg.spread_fraction,
            slippage_cost_fraction=2.0 * cfg.slippage_rate_per_side,
            total_cost_fraction=cfg.round_trip_cost_fraction(),
            net_move_fraction=None,
            assumptions=assumptions,
            reason="gross_move_proxy_unavailable",
        )

    gross = features.normalized_atr * cfg.atr_move_multiplier
    if not math.isfinite(gross) or gross < 0:
        return OpportunityAssessment(
            status=OpportunityStatus.UNAVAILABLE,
            gross_move_fraction=None,
            fee_cost_fraction=2.0 * cfg.fee_rate_per_side,
            spread_cost_fraction=cfg.spread_fraction,
            slippage_cost_fraction=2.0 * cfg.slippage_rate_per_side,
            total_cost_fraction=cfg.round_trip_cost_fraction(),
            net_move_fraction=None,
            assumptions=assumptions,
            reason="non_finite_or_negative_gross_move",
        )

    fee = 2.0 * cfg.fee_rate_per_side
    spread = cfg.spread_fraction
    slip = 2.0 * cfg.slippage_rate_per_side
    total = fee + spread + slip
    net = gross - total

    if gross <= 0:
        status = OpportunityStatus.NO_OPPORTUNITY
        reason = "non_positive_gross_move"
    elif net < cfg.minimum_net_edge_fraction:
        if gross <= total:
            status = OpportunityStatus.REJECTED_COSTS_TOO_HIGH
            reason = "gross_move_does_not_cover_estimated_round_trip_costs"
        else:
            status = OpportunityStatus.REJECTED_COSTS_TOO_HIGH
            reason = "net_edge_below_minimum_after_costs"
    else:
        status = OpportunityStatus.OPPORTUNITY
        reason = "proxied_net_edge_clears_minimum_after_costs"

    return OpportunityAssessment(
        status=status,
        gross_move_fraction=gross,
        fee_cost_fraction=fee,
        spread_cost_fraction=spread,
        slippage_cost_fraction=slip,
        total_cost_fraction=total,
        net_move_fraction=net,
        assumptions=assumptions,
        reason=reason,
    )
