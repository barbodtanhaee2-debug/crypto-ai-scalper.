"""Versioned adaptive decision-record schema (Stage 3.3.2.2 / 3.3.2.4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from research_lab.adaptive.candidate import StrategyCandidate
from research_lab.adaptive.enums import DecisionOutcomeKind
from research_lab.adaptive.evidence import CorrelatedEvidenceGroup
from research_lab.adaptive.immutability import freeze_mapping, mapping_to_dict
from research_lab.adaptive.recommendation import (
    NoTradeDecision,
    PositionStateSnapshot,
    StrategyRecommendation,
)
from research_lab.adaptive.timeutil import require_utc, utc_iso
from research_lab.adaptive.validation import build_evidence_by_family_from_candidates
from research_lab.adaptive.versioning import (
    DECISION_RECORD_SCHEMA_VERSION,
    SCHEMA_VERSION,
)


@dataclass(frozen=True)
class AdaptiveDecisionRecord:
    """Serializable decision record for offline reconstruction.

    Storage proposal: JSONL under research-lab/experiments/runs/ (not frozen).
    Does not implement controller logic or broker I/O.

    ``evidence_by_family`` is emitted on serialization as a deterministic,
    lossless index of candidate evidence grouped by family (no scoring).
    """

    decision_id: str
    decision_time: datetime
    outcome_kind: DecisionOutcomeKind
    schema_version: str = DECISION_RECORD_SCHEMA_VERSION
    interface_version: str = SCHEMA_VERSION
    market_data_ref: str | None = None
    characterization_ref: str | None = None
    data_quality_status: str | None = None
    primary_regime: str | None = None
    secondary_characteristics: Mapping[str, str] = field(default_factory=dict)
    eligible_engines: tuple[str, ...] = ()
    ineligible_engines: Mapping[str, str] = field(default_factory=dict)
    candidates: tuple[StrategyCandidate, ...] = ()
    correlated_evidence_groups: tuple[CorrelatedEvidenceGroup, ...] = ()
    evidence_by_family: Mapping[str, tuple[Mapping[str, Any], ...]] | None = None
    recommendation: StrategyRecommendation | None = None
    no_trade: NoTradeDecision | None = None
    position_state: PositionStateSnapshot | None = None
    controller_config_version: str | None = None
    reason: str = ""
    extensions: Mapping[str, Any] = field(default_factory=dict)
    deprecated_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decision_time", require_utc(self.decision_time, field="decision_time")
        )
        object.__setattr__(
            self,
            "secondary_characteristics",
            freeze_mapping(self.secondary_characteristics),
        )
        object.__setattr__(
            self, "ineligible_engines", freeze_mapping(self.ineligible_engines)
        )
        object.__setattr__(self, "extensions", freeze_mapping(self.extensions))
        object.__setattr__(
            self, "deprecated_fields", freeze_mapping(self.deprecated_fields)
        )
        if self.evidence_by_family is not None:
            frozen_ebf: dict[str, Any] = {}
            for family, items in self.evidence_by_family.items():
                frozen_ebf[str(family)] = tuple(freeze_mapping(dict(item)) for item in items)
            object.__setattr__(self, "evidence_by_family", freeze_mapping(frozen_ebf))
        from research_lab.adaptive.validation import validate_decision_record_dict

        errors = validate_decision_record_dict(self.to_dict())
        if errors:
            raise ValueError("; ".join(errors))

    def resolved_evidence_by_family(self) -> dict[str, list[dict[str, Any]]]:
        """Return family → evidence payloads (explicit or reconstructed)."""

        payload = self.to_dict()
        explicit = payload.get("evidence_by_family")
        if isinstance(explicit, dict):
            return {
                str(family): [dict(item) for item in items]
                for family, items in explicit.items()
            }
        return build_evidence_by_family_from_candidates(payload.get("candidates") or [])

    def to_dict(self) -> dict[str, Any]:
        candidates = [c.to_dict() for c in self.candidates]
        if self.evidence_by_family is None:
            evidence_by_family = build_evidence_by_family_from_candidates(candidates)
        else:
            evidence_by_family = {
                str(family): [mapping_to_dict(item) for item in items]
                for family, items in self.evidence_by_family.items()
            }
        return {
            "schema_version": self.schema_version,
            "interface_version": self.interface_version,
            "decision_id": self.decision_id,
            "decision_time": utc_iso(self.decision_time),
            "outcome_kind": self.outcome_kind.value,
            "market_data_ref": self.market_data_ref,
            "characterization_ref": self.characterization_ref,
            "data_quality_status": self.data_quality_status,
            "primary_regime": self.primary_regime,
            "secondary_characteristics": mapping_to_dict(self.secondary_characteristics),
            "eligible_engines": list(self.eligible_engines),
            "ineligible_engines": mapping_to_dict(self.ineligible_engines),
            "candidates": candidates,
            "correlated_evidence_groups": [
                g.to_dict() for g in self.correlated_evidence_groups
            ],
            "evidence_by_family": evidence_by_family,
            "recommendation": None
            if self.recommendation is None
            else self.recommendation.to_dict(),
            "no_trade": None if self.no_trade is None else self.no_trade.to_dict(),
            "position_state": None
            if self.position_state is None
            else self.position_state.to_dict(),
            "controller_config_version": self.controller_config_version,
            "reason": self.reason,
            "extensions": mapping_to_dict(self.extensions),
            "deprecated_fields": mapping_to_dict(self.deprecated_fields),
        }
