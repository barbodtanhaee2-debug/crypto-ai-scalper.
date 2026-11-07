"""Stage 3.3.2.2 / 3.3.2.4 — Shared adaptive contracts (interfaces & decision records).

Research-lab only. Not an execution engine. Not a strategy controller.
"""

from __future__ import annotations

from research_lab.adaptive.candidate import StrategyCandidate
from research_lab.adaptive.construct import (
    create_candidate,
    create_decision_record,
    create_no_trade,
    create_opportunity,
    create_recommendation,
)
from research_lab.adaptive.decision_record import AdaptiveDecisionRecord
from research_lab.adaptive.enums import (
    DecisionOutcomeKind,
    EngineId,
    EvidenceFamily,
    EvidenceKind,
    EvidenceStatus,
    NoTradeReasonCode,
    OpportunityAssessmentStatus,
    SignalStatus,
    TradeDirection,
)
from research_lab.adaptive.evidence import ConfidenceEvidence, CorrelatedEvidenceGroup
from research_lab.adaptive.opportunity import CostAdjustedOpportunity, CostAssumptions
from research_lab.adaptive.recommendation import (
    NoTradeDecision,
    PositionStateSnapshot,
    StrategyRecommendation,
)
from research_lab.adaptive.serialize import (
    candidate_from_dict,
    decision_record_from_dict,
    dumps_canonical,
    loads_object,
    no_trade_from_dict,
    recommendation_from_dict,
)
from research_lab.adaptive.validation import (
    build_evidence_by_family_from_candidates,
    validate_candidate_dict,
    validate_decision_record_dict,
    validate_no_trade_dict,
    validate_opportunity_dict,
    validate_recommendation_dict,
    validate_signal_status,
)
from research_lab.adaptive.versioning import (
    CANDIDATE_CONTRACT_VERSION,
    DECISION_RECORD_SCHEMA_VERSION,
    RECOMMENDATION_CONTRACT_VERSION,
    SCHEMA_VERSION,
    classify_version,
    is_compatible,
    parse_semver,
)

__all__ = [
    "SCHEMA_VERSION",
    "CANDIDATE_CONTRACT_VERSION",
    "RECOMMENDATION_CONTRACT_VERSION",
    "DECISION_RECORD_SCHEMA_VERSION",
    "EngineId",
    "TradeDirection",
    "SignalStatus",
    "EvidenceKind",
    "EvidenceFamily",
    "EvidenceStatus",
    "OpportunityAssessmentStatus",
    "DecisionOutcomeKind",
    "NoTradeReasonCode",
    "ConfidenceEvidence",
    "CorrelatedEvidenceGroup",
    "CostAssumptions",
    "CostAdjustedOpportunity",
    "StrategyCandidate",
    "StrategyRecommendation",
    "NoTradeDecision",
    "PositionStateSnapshot",
    "AdaptiveDecisionRecord",
    "dumps_canonical",
    "loads_object",
    "candidate_from_dict",
    "recommendation_from_dict",
    "no_trade_from_dict",
    "decision_record_from_dict",
    "create_candidate",
    "create_recommendation",
    "create_no_trade",
    "create_opportunity",
    "create_decision_record",
    "validate_candidate_dict",
    "validate_recommendation_dict",
    "validate_no_trade_dict",
    "validate_decision_record_dict",
    "validate_opportunity_dict",
    "validate_signal_status",
    "build_evidence_by_family_from_candidates",
    "is_compatible",
    "parse_semver",
    "classify_version",
]
