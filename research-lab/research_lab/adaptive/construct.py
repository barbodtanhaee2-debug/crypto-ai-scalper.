"""Validated public construction helpers for adaptive contracts.

Direct dataclass constructors also validate via ``__post_init__``. These helpers
are the preferred dictionary-origin entry points and mirror loader semantics.
"""

from __future__ import annotations

from typing import Any, Mapping

from research_lab.adaptive.candidate import StrategyCandidate
from research_lab.adaptive.decision_record import AdaptiveDecisionRecord
from research_lab.adaptive.opportunity import CostAdjustedOpportunity
from research_lab.adaptive.recommendation import NoTradeDecision, StrategyRecommendation
from research_lab.adaptive.serialize import (
    candidate_from_dict,
    decision_record_from_dict,
    no_trade_from_dict,
    opportunity_from_dict,
    recommendation_from_dict,
)


def create_opportunity(payload: Mapping[str, Any]) -> CostAdjustedOpportunity:
    return opportunity_from_dict(payload)


def create_candidate(payload: Mapping[str, Any]) -> StrategyCandidate:
    return candidate_from_dict(payload)


def create_recommendation(payload: Mapping[str, Any]) -> StrategyRecommendation:
    return recommendation_from_dict(payload)


def create_no_trade(payload: Mapping[str, Any]) -> NoTradeDecision:
    return no_trade_from_dict(payload)


def create_decision_record(payload: Mapping[str, Any]) -> AdaptiveDecisionRecord:
    return decision_record_from_dict(payload)
