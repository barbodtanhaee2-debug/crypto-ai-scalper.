"""StrategyCandidate shared contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from research_lab.adaptive.enums import EngineId, SignalStatus, TradeDirection
from research_lab.adaptive.evidence import ConfidenceEvidence, CorrelatedEvidenceGroup
from research_lab.adaptive.immutability import freeze_mapping, mapping_to_dict
from research_lab.adaptive.opportunity import CostAdjustedOpportunity
from research_lab.adaptive.timeutil import require_utc, utc_iso
from research_lab.adaptive.versioning import CANDIDATE_CONTRACT_VERSION


@dataclass(frozen=True)
class StrategyCandidate:
    """Engine proposal for controller evaluation — not an execution permit.

    Public construction validates the serialized form. Prefer
    ``candidate_from_dict`` / ``create_candidate`` for dictionary-origin data.
    """

    candidate_id: str
    engine_id: EngineId
    direction: TradeDirection
    signal_status: SignalStatus
    decision_time: datetime
    contract_version: str = CANDIDATE_CONTRACT_VERSION
    market_data_ref: str | None = None
    characterization_ref: str | None = None
    eligible: bool | None = None
    eligibility_reason: str | None = None
    evidence: tuple[ConfidenceEvidence, ...] = ()
    correlated_groups: tuple[CorrelatedEvidenceGroup, ...] = ()
    opportunity: CostAdjustedOpportunity | None = None
    rejection_reason: str | None = None
    required_market_characteristics: Mapping[str, str] = field(default_factory=dict)
    entry_reference: str | None = None
    invalidation_reference: str | None = None
    opportunity_expiry: str | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decision_time", require_utc(self.decision_time, field="decision_time")
        )
        if self.eligible is not None and not isinstance(self.eligible, bool):
            raise TypeError("eligible must be a boolean or null")
        object.__setattr__(
            self,
            "required_market_characteristics",
            freeze_mapping(self.required_market_characteristics),
        )
        object.__setattr__(self, "extensions", freeze_mapping(self.extensions))
        from research_lab.adaptive.validation import validate_candidate_dict

        errors = validate_candidate_dict(self.to_dict())
        if errors:
            raise ValueError("; ".join(errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "candidate_id": self.candidate_id,
            "engine_id": self.engine_id.value,
            "direction": self.direction.value,
            "signal_status": self.signal_status.value,
            "decision_time": utc_iso(self.decision_time),
            "market_data_ref": self.market_data_ref,
            "characterization_ref": self.characterization_ref,
            "eligible": self.eligible,
            "eligibility_reason": self.eligibility_reason,
            "evidence": [item.to_dict() for item in self.evidence],
            "correlated_groups": [item.to_dict() for item in self.correlated_groups],
            "opportunity": None
            if self.opportunity is None
            else self.opportunity.to_dict(),
            "rejection_reason": self.rejection_reason,
            "required_market_characteristics": mapping_to_dict(
                self.required_market_characteristics
            ),
            "entry_reference": self.entry_reference,
            "invalidation_reference": self.invalidation_reference,
            "opportunity_expiry": self.opportunity_expiry,
            "extensions": mapping_to_dict(self.extensions),
        }
