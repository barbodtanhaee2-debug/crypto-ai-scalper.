"""Deterministic JSON serialization for adaptive contracts."""

from __future__ import annotations

import json
from typing import Any, Mapping

from research_lab.adaptive.candidate import StrategyCandidate
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
from research_lab.adaptive.timeutil import parse_utc_iso
from research_lab.adaptive.validation import (
    validate_candidate_dict,
    validate_confidence_evidence_dict,
    validate_correlated_group_dict,
    validate_decision_record_dict,
    validate_no_trade_dict,
    validate_opportunity_dict,
    validate_recommendation_dict,
)


def dumps_canonical(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON object serialization."""

    return json.dumps(payload, separators=(",", ":"), sort_keys=True, allow_nan=False)


def loads_object(text: str) -> dict[str, Any]:
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("JSON payload must be an object")
    return raw


def _position_state_from_dict(raw: Mapping[str, Any] | None) -> PositionStateSnapshot | None:
    if raw is None:
        return None
    return PositionStateSnapshot(
        position_open=raw.get("position_open"),  # type: ignore[arg-type]
        direction=raw.get("direction"),  # type: ignore[arg-type]
        strategy_tag=raw.get("strategy_tag"),  # type: ignore[arg-type]
        reference=raw.get("reference"),  # type: ignore[arg-type]
        notes=raw.get("notes"),  # type: ignore[arg-type]
        extensions=dict(raw.get("extensions") or {}),
    )


def opportunity_from_dict(raw: Mapping[str, Any]) -> CostAdjustedOpportunity:
    errors = validate_opportunity_dict(raw)
    if errors:
        raise ValueError("; ".join(errors))
    assumptions_raw = raw.get("cost_assumptions")
    if not isinstance(assumptions_raw, Mapping):
        raise ValueError("opportunity: cost_assumptions must be an object")
    # costs_complete already verified as bool by validate_opportunity_dict.
    assumptions = CostAssumptions(
        fee_rate_per_side=float(assumptions_raw["fee_rate_per_side"])
        if "fee_rate_per_side" in assumptions_raw
        and assumptions_raw["fee_rate_per_side"] is not None
        else 0.001,
        spread_fraction=float(assumptions_raw["spread_fraction"])
        if "spread_fraction" in assumptions_raw
        and assumptions_raw["spread_fraction"] is not None
        else 0.0002,
        slippage_rate_per_side=float(assumptions_raw["slippage_rate_per_side"])
        if "slippage_rate_per_side" in assumptions_raw
        and assumptions_raw["slippage_rate_per_side"] is not None
        else 0.0005,
        costs_complete=assumptions_raw["costs_complete"],  # type: ignore[arg-type]
        source=str(assumptions_raw.get("source", "BacktestSettings_defaults")),
    )
    return CostAdjustedOpportunity(
        status=OpportunityAssessmentStatus(str(raw["status"])),
        gross_move_fraction=raw.get("gross_move_fraction"),  # type: ignore[arg-type]
        fee_cost_fraction=raw.get("fee_cost_fraction"),  # type: ignore[arg-type]
        spread_cost_fraction=raw.get("spread_cost_fraction"),  # type: ignore[arg-type]
        slippage_cost_fraction=raw.get("slippage_cost_fraction"),  # type: ignore[arg-type]
        total_cost_fraction=raw.get("total_cost_fraction"),  # type: ignore[arg-type]
        net_move_fraction=raw.get("net_move_fraction"),  # type: ignore[arg-type]
        cost_assumptions=assumptions,
        gross_proxy_label=str(raw.get("gross_proxy_label", "unspecified_proxy")),
        is_forecast=False if "is_forecast" not in raw else raw["is_forecast"],
        uses_historical_mfe_as_guarantee=False
        if "uses_historical_mfe_as_guarantee" not in raw
        else raw["uses_historical_mfe_as_guarantee"],
        reason=str(raw.get("reason", "")),
        extensions=dict(raw.get("extensions") or {}),
        contract_version=str(raw["contract_version"]),
    )


def evidence_from_dict(raw: Mapping[str, Any]) -> ConfidenceEvidence:
    errors = validate_confidence_evidence_dict(raw)
    if errors:
        raise ValueError("; ".join(errors))
    direction = raw.get("direction")
    return ConfidenceEvidence(
        evidence_id=str(raw["evidence_id"]),
        family=EvidenceFamily(str(raw["family"])),
        kind=EvidenceKind(str(raw["kind"])),
        status=EvidenceStatus(str(raw["status"])),
        direction=None if direction is None else TradeDirection(str(direction)),
        source_reference=raw.get("source_reference"),  # type: ignore[arg-type]
        explanation=raw.get("explanation"),  # type: ignore[arg-type]
        rejection_reason=raw.get("rejection_reason"),  # type: ignore[arg-type]
        measurements=dict(raw.get("measurements") or {}),
        extensions=dict(raw.get("extensions") or {}),
        contract_version=str(raw["contract_version"]),
    )


def correlated_group_from_dict(raw: Mapping[str, Any]) -> CorrelatedEvidenceGroup:
    errors = validate_correlated_group_dict(raw)
    if errors:
        raise ValueError("; ".join(errors))
    members = raw.get("member_evidence_ids") or []
    return CorrelatedEvidenceGroup(
        group_id=str(raw["group_id"]),
        family=EvidenceFamily(str(raw["family"])),
        member_evidence_ids=tuple(str(item) for item in members),
        counts_as_single_credit=raw.get("counts_as_single_credit", True),  # type: ignore[arg-type]
        note=raw.get("note"),  # type: ignore[arg-type]
        extensions=dict(raw.get("extensions") or {}),
    )


def candidate_from_dict(raw: Mapping[str, Any]) -> StrategyCandidate:
    errors = validate_candidate_dict(raw)
    if errors:
        raise ValueError("; ".join(errors))
    opportunity_raw = raw.get("opportunity")
    evidence_items = tuple(
        evidence_from_dict(item) for item in (raw.get("evidence") or [])
    )
    groups = tuple(
        correlated_group_from_dict(item)
        for item in (raw.get("correlated_groups") or [])
    )
    return StrategyCandidate(
        candidate_id=str(raw["candidate_id"]),
        engine_id=EngineId(str(raw["engine_id"])),
        direction=TradeDirection(str(raw["direction"])),
        signal_status=SignalStatus(str(raw["signal_status"])),
        decision_time=parse_utc_iso(str(raw["decision_time"]), field="decision_time"),
        contract_version=str(raw["contract_version"]),
        market_data_ref=raw.get("market_data_ref"),  # type: ignore[arg-type]
        characterization_ref=raw.get("characterization_ref"),  # type: ignore[arg-type]
        eligible=raw.get("eligible"),  # type: ignore[arg-type]
        eligibility_reason=raw.get("eligibility_reason"),  # type: ignore[arg-type]
        evidence=evidence_items,
        correlated_groups=groups,
        opportunity=None
        if opportunity_raw is None
        else opportunity_from_dict(opportunity_raw),
        rejection_reason=raw.get("rejection_reason"),  # type: ignore[arg-type]
        required_market_characteristics=dict(
            raw.get("required_market_characteristics") or {}
        ),
        entry_reference=raw.get("entry_reference"),  # type: ignore[arg-type]
        invalidation_reference=raw.get("invalidation_reference"),  # type: ignore[arg-type]
        opportunity_expiry=raw.get("opportunity_expiry"),  # type: ignore[arg-type]
        extensions=dict(raw.get("extensions") or {}),
    )


def recommendation_from_dict(raw: Mapping[str, Any]) -> StrategyRecommendation:
    errors = validate_recommendation_dict(raw)
    if errors:
        raise ValueError("; ".join(errors))
    confirmation = raw.get("confirmation_engine")
    opportunity_raw = raw.get("opportunity")
    return StrategyRecommendation(
        recommendation_id=str(raw["recommendation_id"]),
        decision_time=parse_utc_iso(str(raw["decision_time"]), field="decision_time"),
        primary_engine=EngineId(str(raw["primary_engine"])),
        primary_candidate_id=str(raw["primary_candidate_id"]),
        rationale=str(raw["rationale"]),
        contract_version=str(raw["contract_version"]),
        confirmation_engine=None
        if confirmation is None
        else EngineId(str(confirmation)),
        confirmation_candidate_id=raw.get("confirmation_candidate_id"),  # type: ignore[arg-type]
        supporting_candidate_ids=tuple(raw.get("supporting_candidate_ids") or ()),
        rejected_candidate_ids=tuple(raw.get("rejected_candidate_ids") or ()),
        rejected_reasons=dict(raw.get("rejected_reasons") or {}),
        opportunity=None
        if opportunity_raw is None
        else opportunity_from_dict(opportunity_raw),
        position_state=_position_state_from_dict(raw.get("position_state")),
        position_constraints_applied=tuple(
            raw.get("position_constraints_applied") or ()
        ),
        extensions=dict(raw.get("extensions") or {}),
    )


def no_trade_from_dict(raw: Mapping[str, Any]) -> NoTradeDecision:
    errors = validate_no_trade_dict(raw)
    if errors:
        raise ValueError("; ".join(errors))
    return NoTradeDecision(
        decision_id=str(raw["decision_id"]),
        decision_time=parse_utc_iso(str(raw["decision_time"]), field="decision_time"),
        reason_code=NoTradeReasonCode(str(raw["reason_code"])),
        reason=str(raw["reason"]),
        contract_version=str(raw["contract_version"]),
        considered_candidate_ids=tuple(raw.get("considered_candidate_ids") or ()),
        ineligible_engines=dict(raw.get("ineligible_engines") or {}),
        position_state=_position_state_from_dict(raw.get("position_state")),
        position_constraints_applied=tuple(
            raw.get("position_constraints_applied") or ()
        ),
        extensions=dict(raw.get("extensions") or {}),
    )


def decision_record_from_dict(raw: Mapping[str, Any]) -> AdaptiveDecisionRecord:
    errors = validate_decision_record_dict(raw)
    if errors:
        raise ValueError("; ".join(errors))
    candidates = tuple(
        candidate_from_dict(item) for item in (raw.get("candidates") or [])
    )
    groups = tuple(
        correlated_group_from_dict(item)
        for item in (raw.get("correlated_evidence_groups") or [])
    )
    recommendation_raw = raw.get("recommendation")
    no_trade_raw = raw.get("no_trade")
    ebf_raw = raw.get("evidence_by_family")
    evidence_by_family = None
    if isinstance(ebf_raw, Mapping):
        evidence_by_family = {
            str(family): tuple(dict(item) for item in (items or []))
            for family, items in ebf_raw.items()
        }
    return AdaptiveDecisionRecord(
        decision_id=str(raw["decision_id"]),
        decision_time=parse_utc_iso(str(raw["decision_time"]), field="decision_time"),
        outcome_kind=DecisionOutcomeKind(str(raw["outcome_kind"])),
        schema_version=str(raw["schema_version"]),
        interface_version=str(raw["interface_version"]),
        market_data_ref=raw.get("market_data_ref"),  # type: ignore[arg-type]
        characterization_ref=raw.get("characterization_ref"),  # type: ignore[arg-type]
        data_quality_status=raw.get("data_quality_status"),  # type: ignore[arg-type]
        primary_regime=raw.get("primary_regime"),  # type: ignore[arg-type]
        secondary_characteristics=dict(raw.get("secondary_characteristics") or {}),
        eligible_engines=tuple(raw.get("eligible_engines") or ()),
        ineligible_engines=dict(raw.get("ineligible_engines") or {}),
        candidates=candidates,
        correlated_evidence_groups=groups,
        evidence_by_family=evidence_by_family,
        recommendation=None
        if recommendation_raw is None
        else recommendation_from_dict(recommendation_raw),
        no_trade=None if no_trade_raw is None else no_trade_from_dict(no_trade_raw),
        position_state=_position_state_from_dict(raw.get("position_state")),
        controller_config_version=raw.get("controller_config_version"),  # type: ignore[arg-type]
        reason=str(raw.get("reason", "")),
        extensions=dict(raw.get("extensions") or {}),
        deprecated_fields=dict(raw.get("deprecated_fields") or {}),
    )
