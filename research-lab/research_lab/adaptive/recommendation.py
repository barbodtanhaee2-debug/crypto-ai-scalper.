"""StrategyRecommendation and first-class NO_TRADE decision contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from research_lab.adaptive.enums import (
    DecisionOutcomeKind,
    EngineId,
    NoTradeReasonCode,
)
from research_lab.adaptive.immutability import freeze_mapping, mapping_to_dict
from research_lab.adaptive.opportunity import CostAdjustedOpportunity
from research_lab.adaptive.timeutil import require_utc, utc_iso
from research_lab.adaptive.versioning import RECOMMENDATION_CONTRACT_VERSION


@dataclass(frozen=True)
class PositionStateSnapshot:
    """Read-only position-state placeholder for future broker/risk mapping.

    Fields are optional extensible placeholders. Stage A does not fetch broker
    data or invent unresolved API mappings.
    """

    position_open: bool | None = None
    direction: str | None = None
    strategy_tag: str | None = None
    reference: str | None = None
    notes: str | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.position_open is not None and not isinstance(self.position_open, bool):
            raise TypeError("position_open must be a boolean or null")
        object.__setattr__(self, "extensions", freeze_mapping(self.extensions))

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_open": self.position_open,
            "direction": self.direction,
            "strategy_tag": self.strategy_tag,
            "reference": self.reference,
            "notes": self.notes,
            "extensions": mapping_to_dict(self.extensions),
        }


@dataclass(frozen=True)
class StrategyRecommendation:
    """Controller recommendation — does not authorize order execution."""

    recommendation_id: str
    decision_time: datetime
    primary_engine: EngineId
    primary_candidate_id: str
    rationale: str
    contract_version: str = RECOMMENDATION_CONTRACT_VERSION
    confirmation_engine: EngineId | None = None
    confirmation_candidate_id: str | None = None
    supporting_candidate_ids: tuple[str, ...] = ()
    rejected_candidate_ids: tuple[str, ...] = ()
    rejected_reasons: Mapping[str, str] = field(default_factory=dict)
    opportunity: CostAdjustedOpportunity | None = None
    position_state: PositionStateSnapshot | None = None
    position_constraints_applied: tuple[str, ...] = ()
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decision_time", require_utc(self.decision_time, field="decision_time")
        )
        object.__setattr__(self, "rejected_reasons", freeze_mapping(self.rejected_reasons))
        object.__setattr__(self, "extensions", freeze_mapping(self.extensions))
        from research_lab.adaptive.validation import validate_recommendation_dict

        errors = validate_recommendation_dict(self.to_dict())
        if errors:
            raise ValueError("; ".join(errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "outcome_kind": DecisionOutcomeKind.RECOMMENDATION.value,
            "recommendation_id": self.recommendation_id,
            "decision_time": utc_iso(self.decision_time),
            "primary_engine": self.primary_engine.value,
            "primary_candidate_id": self.primary_candidate_id,
            "confirmation_engine": None
            if self.confirmation_engine is None
            else self.confirmation_engine.value,
            "confirmation_candidate_id": self.confirmation_candidate_id,
            "supporting_candidate_ids": list(self.supporting_candidate_ids),
            "rejected_candidate_ids": list(self.rejected_candidate_ids),
            "rejected_reasons": mapping_to_dict(self.rejected_reasons),
            "opportunity": None
            if self.opportunity is None
            else self.opportunity.to_dict(),
            "position_state": None
            if self.position_state is None
            else self.position_state.to_dict(),
            "position_constraints_applied": list(self.position_constraints_applied),
            "rationale": self.rationale,
            "extensions": mapping_to_dict(self.extensions),
        }


@dataclass(frozen=True)
class NoTradeDecision:
    """First-class NO_TRADE outcome — not a missing recommendation."""

    decision_id: str
    decision_time: datetime
    reason_code: NoTradeReasonCode
    reason: str
    contract_version: str = RECOMMENDATION_CONTRACT_VERSION
    considered_candidate_ids: tuple[str, ...] = ()
    ineligible_engines: Mapping[str, str] = field(default_factory=dict)
    position_state: PositionStateSnapshot | None = None
    position_constraints_applied: tuple[str, ...] = ()
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decision_time", require_utc(self.decision_time, field="decision_time")
        )
        object.__setattr__(
            self, "ineligible_engines", freeze_mapping(self.ineligible_engines)
        )
        object.__setattr__(self, "extensions", freeze_mapping(self.extensions))
        from research_lab.adaptive.validation import validate_no_trade_dict

        errors = validate_no_trade_dict(self.to_dict())
        if errors:
            raise ValueError("; ".join(errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "outcome_kind": DecisionOutcomeKind.NO_TRADE.value,
            "decision_id": self.decision_id,
            "decision_time": utc_iso(self.decision_time),
            "reason_code": self.reason_code.value,
            "reason": self.reason,
            "considered_candidate_ids": list(self.considered_candidate_ids),
            "ineligible_engines": mapping_to_dict(self.ineligible_engines),
            "position_state": None
            if self.position_state is None
            else self.position_state.to_dict(),
            "position_constraints_applied": list(self.position_constraints_applied),
            "extensions": mapping_to_dict(self.extensions),
        }


# Type alias for documentation / future controller outputs.
ControllerDecision = StrategyRecommendation | NoTradeDecision
