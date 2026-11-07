"""Confidence evidence and correlated evidence-family contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from research_lab.adaptive.enums import (
    EvidenceFamily,
    EvidenceKind,
    EvidenceStatus,
    TradeDirection,
)
from research_lab.adaptive.immutability import freeze_mapping, mapping_to_dict
from research_lab.adaptive.versioning import CANDIDATE_CONTRACT_VERSION


@dataclass(frozen=True)
class ConfidenceEvidence:
    """Structured evidence item — not a probability of profit."""

    evidence_id: str
    family: EvidenceFamily
    kind: EvidenceKind
    status: EvidenceStatus
    direction: TradeDirection | None = None
    source_reference: str | None = None
    explanation: str | None = None
    rejection_reason: str | None = None
    measurements: Mapping[str, float | int | str | bool | None] = field(
        default_factory=dict
    )
    extensions: Mapping[str, Any] = field(default_factory=dict)
    contract_version: str = CANDIDATE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "measurements", freeze_mapping(self.measurements))
        object.__setattr__(self, "extensions", freeze_mapping(self.extensions))
        from research_lab.adaptive.validation import validate_confidence_evidence_dict

        errors = validate_confidence_evidence_dict(self.to_dict())
        if errors:
            raise ValueError("; ".join(errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "evidence_id": self.evidence_id,
            "family": self.family.value,
            "kind": self.kind.value,
            "status": self.status.value,
            "direction": None if self.direction is None else self.direction.value,
            "source_reference": self.source_reference,
            "explanation": self.explanation,
            "rejection_reason": self.rejection_reason,
            "measurements": mapping_to_dict(self.measurements),
            "extensions": mapping_to_dict(self.extensions),
        }


@dataclass(frozen=True)
class CorrelatedEvidenceGroup:
    """Groups related evidence so correlated items share one family credit.

    Correlation geometry / thresholds are intentionally not finalized here.
    ``member_evidence_ids`` references ``ConfidenceEvidence.evidence_id`` values.
    """

    group_id: str
    family: EvidenceFamily
    member_evidence_ids: tuple[str, ...]
    counts_as_single_credit: bool = True
    note: str | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extensions", freeze_mapping(self.extensions))
        if not isinstance(self.counts_as_single_credit, bool):
            raise TypeError("counts_as_single_credit must be a boolean")
        from research_lab.adaptive.validation import validate_correlated_group_dict

        errors = validate_correlated_group_dict(self.to_dict())
        if errors:
            raise ValueError("; ".join(errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "family": self.family.value,
            "member_evidence_ids": list(self.member_evidence_ids),
            "counts_as_single_credit": self.counts_as_single_credit,
            "note": self.note,
            "extensions": mapping_to_dict(self.extensions),
        }
