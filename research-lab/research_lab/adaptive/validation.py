"""Validation for adaptive shared contracts (fail closed)."""

from __future__ import annotations

import math
from typing import Any, Mapping

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
from research_lab.adaptive.versioning import (
    CANDIDATE_CONTRACT_VERSION,
    DECISION_RECORD_SCHEMA_VERSION,
    RECOMMENDATION_CONTRACT_VERSION,
    SCHEMA_VERSION,
    classify_version,
    parse_semver,
)

# Structural arithmetic tolerance (not a trading threshold).
_ARITH_EPS = 1e-9

_PROHIBITED_CONFIDENCE_KEYS = frozenset(
    {"probability_of_profit", "confidence_percent", "win_probability"}
)

COST_ASSUMPTION_FIELDS = frozenset(
    {
        "fee_rate_per_side",
        "spread_fraction",
        "slippage_rate_per_side",
        "costs_complete",
        "source",
        "round_trip_cost_fraction",  # derived, optional on wire
    }
)

OPPORTUNITY_FIELDS = frozenset(
    {
        "contract_version",
        "status",
        "gross_move_fraction",
        "fee_cost_fraction",
        "spread_cost_fraction",
        "slippage_cost_fraction",
        "total_cost_fraction",
        "net_move_fraction",
        "cost_assumptions",
        "gross_proxy_label",
        "is_forecast",
        "uses_historical_mfe_as_guarantee",
        "reason",
        "extensions",
    }
)

EVIDENCE_FIELDS = frozenset(
    {
        "contract_version",
        "evidence_id",
        "family",
        "kind",
        "status",
        "direction",
        "source_reference",
        "explanation",
        "rejection_reason",
        "measurements",
        "extensions",
    }
)

CORRELATED_GROUP_FIELDS = frozenset(
    {
        "group_id",
        "family",
        "member_evidence_ids",
        "counts_as_single_credit",
        "note",
        "extensions",
    }
)

CANDIDATE_FIELDS = frozenset(
    {
        "contract_version",
        "candidate_id",
        "engine_id",
        "direction",
        "signal_status",
        "decision_time",
        "market_data_ref",
        "characterization_ref",
        "eligible",
        "eligibility_reason",
        "evidence",
        "correlated_groups",
        "opportunity",
        "rejection_reason",
        "required_market_characteristics",
        "entry_reference",
        "invalidation_reference",
        "opportunity_expiry",
        "extensions",
    }
)

POSITION_STATE_FIELDS = frozenset(
    {
        "position_open",
        "direction",
        "strategy_tag",
        "reference",
        "notes",
        "extensions",
    }
)

RECOMMENDATION_FIELDS = frozenset(
    {
        "contract_version",
        "outcome_kind",
        "recommendation_id",
        "decision_time",
        "primary_engine",
        "primary_candidate_id",
        "confirmation_engine",
        "confirmation_candidate_id",
        "supporting_candidate_ids",
        "rejected_candidate_ids",
        "rejected_reasons",
        "opportunity",
        "position_state",
        "position_constraints_applied",
        "rationale",
        "extensions",
    }
)

NO_TRADE_FIELDS = frozenset(
    {
        "contract_version",
        "outcome_kind",
        "decision_id",
        "decision_time",
        "reason_code",
        "reason",
        "considered_candidate_ids",
        "ineligible_engines",
        "position_state",
        "position_constraints_applied",
        "extensions",
    }
)

DECISION_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "interface_version",
        "decision_id",
        "decision_time",
        "outcome_kind",
        "market_data_ref",
        "characterization_ref",
        "data_quality_status",
        "primary_regime",
        "secondary_characteristics",
        "eligible_engines",
        "ineligible_engines",
        "candidates",
        "correlated_evidence_groups",
        "evidence_by_family",
        "recommendation",
        "no_trade",
        "position_state",
        "controller_config_version",
        "reason",
        "extensions",
        "deprecated_fields",
    }
)


def _require_keys(raw: Mapping[str, Any], keys: tuple[str, ...], prefix: str) -> list[str]:
    errors: list[str] = []
    for key in keys:
        if key not in raw:
            errors.append(f"{prefix}: missing required field {key}")
    return errors


def _unknown_keys(raw: Mapping[str, Any], allowed: frozenset[str], prefix: str) -> list[str]:
    return [
        f"{prefix}: unknown field {key!r}"
        for key in raw
        if key not in allowed
    ]


def _enum_value(enum_cls: type, value: Any, *, field: str) -> list[str]:
    if value is None:
        return [f"{field} must not be null"]
    try:
        enum_cls(value)
    except ValueError:
        return [f"{field} has unknown value {value!r}"]
    return []


def _non_empty_str(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return [f"{field} must be a non-empty string"]
    return []


def _require_bool(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, bool):
        return [f"{field} must be a boolean (rejected non-bool {value!r})"]
    return []


def _optional_bool(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    return _require_bool(value, field=field)


def _check_version(
    value: Any,
    *,
    reader_version: str,
    label: str,
    allow_newer_minor: bool = False,
) -> list[str]:
    if not isinstance(value, str):
        return [f"{label}: version must be a string"]
    kind = classify_version(
        value, reader_version=reader_version, allow_newer_minor=allow_newer_minor
    )
    if kind == "malformed":
        return [f"{label}: malformed version {value!r}"]
    if kind == "unsupported":
        return [f"{label}: unsupported version {value!r}"]
    return []


def _finite_number(
    value: Any,
    *,
    field: str,
    allow_null: bool = True,
    allow_negative: bool = True,
) -> list[str]:
    if value is None:
        if allow_null:
            return []
        return [f"{field} must not be null"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return [f"{field} must be a finite number"]
    if not math.isfinite(float(value)):
        return [f"{field} must be finite (rejected non-finite)"]
    if not allow_negative and float(value) < 0:
        return [f"{field} must be non-negative"]
    return []


def _validate_utc_timestamp(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, str):
        return [f"{field} must be ISO-8601 string"]
    from research_lab.adaptive.timeutil import parse_utc_iso

    try:
        parse_utc_iso(value, field=field)
    except (TypeError, ValueError) as error:
        return [str(error)]
    return []


def _scan_prohibited_keys(raw: Any, *, path: str) -> list[str]:
    errors: list[str] = []
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            key_s = str(key)
            if key_s in _PROHIBITED_CONFIDENCE_KEYS:
                errors.append(f"{path}: prohibited key {key_s!r}")
            errors.extend(_scan_prohibited_keys(value, path=f"{path}.{key_s}"))
    elif isinstance(raw, (list, tuple)):
        for index, item in enumerate(raw):
            errors.extend(_scan_prohibited_keys(item, path=f"{path}[{index}]"))
    return errors


def validate_signal_status(value: Any) -> list[str]:
    return _enum_value(SignalStatus, value, field="signal_status")


def validate_cost_assumptions_dict(raw: Mapping[str, Any], *, prefix: str = "cost_assumptions") -> list[str]:
    if not isinstance(raw, Mapping):
        return [f"{prefix} must be an object"]
    errors = _unknown_keys(raw, COST_ASSUMPTION_FIELDS, prefix)
    errors.extend(_require_keys(raw, ("costs_complete",), prefix))
    if "costs_complete" in raw:
        errors.extend(_require_bool(raw.get("costs_complete"), field=f"{prefix}.costs_complete"))
    for key in ("fee_rate_per_side", "spread_fraction", "slippage_rate_per_side"):
        if key in raw and raw.get(key) is not None:
            errors.extend(
                _finite_number(
                    raw.get(key),
                    field=f"{prefix}.{key}",
                    allow_null=False,
                    allow_negative=False,
                )
            )
    if "source" in raw and raw.get("source") is not None:
        if not isinstance(raw.get("source"), str):
            errors.append(f"{prefix}.source must be a string")
    if "round_trip_cost_fraction" in raw and raw.get("round_trip_cost_fraction") is not None:
        errors.extend(
            _finite_number(
                raw.get("round_trip_cost_fraction"),
                field=f"{prefix}.round_trip_cost_fraction",
                allow_null=False,
                allow_negative=False,
            )
        )
    return errors


def validate_opportunity_dict(raw: Mapping[str, Any] | None) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, Mapping):
        return ["opportunity must be an object or null"]
    errors = _unknown_keys(raw, OPPORTUNITY_FIELDS, "opportunity")
    errors.extend(
        _require_keys(
            raw,
            (
                "contract_version",
                "status",
                "gross_move_fraction",
                "fee_cost_fraction",
                "spread_cost_fraction",
                "slippage_cost_fraction",
                "total_cost_fraction",
                "net_move_fraction",
                "cost_assumptions",
            ),
            "opportunity",
        )
    )
    if "contract_version" in raw:
        errors.extend(
            _check_version(
                raw.get("contract_version"),
                reader_version=CANDIDATE_CONTRACT_VERSION,
                label="opportunity.contract_version",
            )
        )
    errors.extend(
        _enum_value(
            OpportunityAssessmentStatus, raw.get("status"), field="opportunity.status"
        )
    )
    for key, allow_negative in (
        ("gross_move_fraction", True),
        ("fee_cost_fraction", False),
        ("spread_cost_fraction", False),
        ("slippage_cost_fraction", False),
        ("total_cost_fraction", False),
        ("net_move_fraction", True),
    ):
        if key in raw:
            errors.extend(
                _finite_number(
                    raw.get(key),
                    field=f"opportunity.{key}",
                    allow_null=True,
                    allow_negative=allow_negative,
                )
            )
    assumptions = raw.get("cost_assumptions")
    if assumptions is not None:
        errors.extend(validate_cost_assumptions_dict(assumptions, prefix="opportunity.cost_assumptions"))
    costs_complete = None
    if isinstance(assumptions, Mapping) and isinstance(assumptions.get("costs_complete"), bool):
        costs_complete = assumptions.get("costs_complete")
    status = raw.get("status")
    if status == OpportunityAssessmentStatus.OPPORTUNITY.value:
        if costs_complete is False:
            errors.append("opportunity: incomplete costs cannot have status OPPORTUNITY")
        if costs_complete is not True:
            # Missing/invalid completeness already errored; also block OPPORTUNITY.
            if costs_complete is None and not any(
                "costs_complete" in e for e in errors
            ):
                errors.append(
                    "opportunity: OPPORTUNITY requires costs_complete=true"
                )
        fraction_keys = (
            "gross_move_fraction",
            "fee_cost_fraction",
            "spread_cost_fraction",
            "slippage_cost_fraction",
            "total_cost_fraction",
            "net_move_fraction",
        )
        if any(raw.get(key) is None for key in fraction_keys):
            errors.append(
                "opportunity: OPPORTUNITY requires all fraction fields to be non-null"
            )
        else:
            # Structural arithmetic consistency (not a trading edge threshold).
            try:
                fee = float(raw["fee_cost_fraction"])
                spread = float(raw["spread_cost_fraction"])
                slip = float(raw["slippage_cost_fraction"])
                total = float(raw["total_cost_fraction"])
                gross = float(raw["gross_move_fraction"])
                net = float(raw["net_move_fraction"])
            except (TypeError, ValueError, KeyError):
                pass
            else:
                expected_total = fee + spread + slip
                if abs(total - expected_total) > _ARITH_EPS:
                    errors.append(
                        "opportunity: total_cost_fraction must equal "
                        "fee+spread+slippage component costs"
                    )
                expected_net = gross - total
                if abs(net - expected_net) > _ARITH_EPS:
                    errors.append(
                        "opportunity: net_move_fraction must equal "
                        "gross_move_fraction - total_cost_fraction"
                    )
    if "is_forecast" in raw and raw.get("is_forecast") is not None:
        errors.extend(_require_bool(raw.get("is_forecast"), field="opportunity.is_forecast"))
        if raw.get("is_forecast") is True:
            errors.append("opportunity: is_forecast must be false for research proxies")
    if "uses_historical_mfe_as_guarantee" in raw and raw.get(
        "uses_historical_mfe_as_guarantee"
    ) is not None:
        errors.extend(
            _require_bool(
                raw.get("uses_historical_mfe_as_guarantee"),
                field="opportunity.uses_historical_mfe_as_guarantee",
            )
        )
        if raw.get("uses_historical_mfe_as_guarantee") is True:
            errors.append(
                "opportunity: uses_historical_mfe_as_guarantee must be false"
            )
    if "extensions" in raw and raw.get("extensions") is not None:
        if not isinstance(raw.get("extensions"), Mapping):
            errors.append("opportunity: extensions must be an object")
        else:
            errors.extend(
                _scan_prohibited_keys(raw.get("extensions"), path="opportunity.extensions")
            )
    return errors


def validate_confidence_evidence_dict(raw: Mapping[str, Any]) -> list[str]:
    if not isinstance(raw, Mapping):
        return ["evidence must be an object"]
    errors = _unknown_keys(raw, EVIDENCE_FIELDS, "evidence")
    errors.extend(
        _require_keys(
            raw,
            ("contract_version", "evidence_id", "family", "kind", "status"),
            "evidence",
        )
    )
    if "contract_version" in raw:
        errors.extend(
            _check_version(
                raw.get("contract_version"),
                reader_version=CANDIDATE_CONTRACT_VERSION,
                label="evidence.contract_version",
            )
        )
    errors.extend(_enum_value(EvidenceFamily, raw.get("family"), field="evidence.family"))
    errors.extend(_enum_value(EvidenceKind, raw.get("kind"), field="evidence.kind"))
    errors.extend(_enum_value(EvidenceStatus, raw.get("status"), field="evidence.status"))
    if raw.get("direction") is not None:
        errors.extend(
            _enum_value(TradeDirection, raw.get("direction"), field="evidence.direction")
        )
    if "evidence_id" in raw:
        errors.extend(_non_empty_str(raw.get("evidence_id"), field="evidence.evidence_id"))
    measurements = raw.get("measurements")
    if measurements is not None and not isinstance(measurements, Mapping):
        errors.append("evidence: measurements must be an object")
    elif isinstance(measurements, Mapping):
        errors.extend(_scan_prohibited_keys(measurements, path="evidence.measurements"))
    if "extensions" in raw and raw.get("extensions") is not None:
        if not isinstance(raw.get("extensions"), Mapping):
            errors.append("evidence: extensions must be an object")
        else:
            errors.extend(
                _scan_prohibited_keys(raw.get("extensions"), path="evidence.extensions")
            )
    return errors


def validate_correlated_group_dict(raw: Mapping[str, Any]) -> list[str]:
    if not isinstance(raw, Mapping):
        return ["correlated_group must be an object"]
    errors = _unknown_keys(raw, CORRELATED_GROUP_FIELDS, "correlated_group")
    errors.extend(
        _require_keys(raw, ("group_id", "family", "member_evidence_ids"), "correlated_group")
    )
    if "group_id" in raw:
        errors.extend(_non_empty_str(raw.get("group_id"), field="correlated_group.group_id"))
    errors.extend(
        _enum_value(EvidenceFamily, raw.get("family"), field="correlated_group.family")
    )
    members = raw.get("member_evidence_ids")
    if members is not None:
        if not isinstance(members, list):
            errors.append("correlated_group: member_evidence_ids must be a list")
        else:
            seen: set[str] = set()
            for index, member in enumerate(members):
                if not isinstance(member, str) or not member.strip():
                    errors.append(
                        f"correlated_group: member_evidence_ids[{index}] must be "
                        "a non-empty string"
                    )
                    continue
                if member in seen:
                    errors.append(
                        f"correlated_group: duplicate member_evidence_id {member!r}"
                    )
                seen.add(member)
    if "counts_as_single_credit" in raw and raw.get("counts_as_single_credit") is not None:
        errors.extend(
            _require_bool(
                raw.get("counts_as_single_credit"),
                field="correlated_group.counts_as_single_credit",
            )
        )
    if "extensions" in raw and raw.get("extensions") is not None:
        if not isinstance(raw.get("extensions"), Mapping):
            errors.append("correlated_group: extensions must be an object")
        else:
            errors.extend(
                _scan_prohibited_keys(
                    raw.get("extensions"), path="correlated_group.extensions"
                )
            )
    return errors


def _candidate_structural_invariants(raw: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    status = raw.get("signal_status")
    eligible = raw.get("eligible")
    if eligible is not None:
        errors.extend(_optional_bool(eligible, field="candidate.eligible"))
    if status == SignalStatus.INELIGIBLE.value and eligible is True:
        errors.append("candidate: INELIGIBLE cannot have eligible=true")
    if status in {
        SignalStatus.VALID_CANDIDATE.value,
        SignalStatus.CONFIRMED_SIGNAL.value,
    } and eligible is False:
        errors.append(
            f"candidate: {status} cannot have eligible=false"
        )

    evidence = raw.get("evidence") or []
    if isinstance(evidence, list):
        ids: list[str] = []
        for item in evidence:
            if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str):
                ids.append(item["evidence_id"])
        if len(ids) != len(set(ids)):
            errors.append("candidate: duplicate evidence_id values")

        if status in {
            SignalStatus.VALID_CANDIDATE.value,
            SignalStatus.CONFIRMED_SIGNAL.value,
        } and evidence:
            only_conflict = True
            for item in evidence:
                if not isinstance(item, Mapping):
                    only_conflict = False
                    break
                kind = item.get("kind")
                estatus = item.get("status")
                if kind != EvidenceKind.CONFLICTING.value and estatus != EvidenceStatus.CONFLICTING.value:
                    only_conflict = False
                    break
            if only_conflict:
                errors.append(
                    "candidate: VALID_CANDIDATE/CONFIRMED_SIGNAL cannot have "
                    "only conflicting evidence"
                )

        id_set = set(ids)
        groups = raw.get("correlated_groups") or []
        if isinstance(groups, list):
            for index, group in enumerate(groups):
                if not isinstance(group, Mapping):
                    continue
                members = group.get("member_evidence_ids") or []
                if not isinstance(members, list):
                    continue
                for member in members:
                    if isinstance(member, str) and member not in id_set:
                        errors.append(
                            f"candidate: correlated_groups[{index}] references "
                            f"unknown evidence_id {member!r}"
                        )
    return errors


def validate_candidate_dict(raw: Mapping[str, Any]) -> list[str]:
    if not isinstance(raw, Mapping):
        return ["candidate must be an object"]
    errors = _unknown_keys(raw, CANDIDATE_FIELDS, "candidate")
    errors.extend(
        _require_keys(
            raw,
            (
                "contract_version",
                "candidate_id",
                "engine_id",
                "direction",
                "signal_status",
                "decision_time",
            ),
            "candidate",
        )
    )
    if "contract_version" in raw:
        errors.extend(
            _check_version(
                raw.get("contract_version"),
                reader_version=CANDIDATE_CONTRACT_VERSION,
                label="candidate.contract_version",
            )
        )
    if "candidate_id" in raw:
        errors.extend(_non_empty_str(raw.get("candidate_id"), field="candidate.candidate_id"))
    errors.extend(_enum_value(EngineId, raw.get("engine_id"), field="candidate.engine_id"))
    errors.extend(
        _enum_value(TradeDirection, raw.get("direction"), field="candidate.direction")
    )
    status_errors = validate_signal_status(raw.get("signal_status"))
    errors.extend([f"candidate.{e}" for e in status_errors])
    if "decision_time" in raw:
        errors.extend(
            _validate_utc_timestamp(raw.get("decision_time"), field="candidate.decision_time")
        )
    evidence = raw.get("evidence", [])
    if evidence is None:
        evidence = []
    if not isinstance(evidence, list):
        errors.append("candidate: evidence must be a list")
    else:
        for index, item in enumerate(evidence):
            if not isinstance(item, Mapping):
                errors.append(f"candidate: evidence[{index}] must be an object")
                continue
            for problem in validate_confidence_evidence_dict(item):
                errors.append(
                    f"candidate.evidence[{index}].{problem.removeprefix('evidence: ').removeprefix('evidence.')}"
                )
    groups = raw.get("correlated_groups", [])
    if groups is None:
        groups = []
    if not isinstance(groups, list):
        errors.append("candidate: correlated_groups must be a list")
    else:
        for index, item in enumerate(groups):
            if not isinstance(item, Mapping):
                errors.append(f"candidate: correlated_groups[{index}] must be an object")
                continue
            for problem in validate_correlated_group_dict(item):
                errors.append(f"candidate.correlated_groups[{index}]: {problem}")
    if "opportunity" in raw and raw.get("opportunity") is not None:
        errors.extend(validate_opportunity_dict(raw.get("opportunity")))
    if "extensions" in raw and raw.get("extensions") is not None:
        if not isinstance(raw.get("extensions"), Mapping):
            errors.append("candidate: extensions must be an object")
        else:
            errors.extend(
                _scan_prohibited_keys(raw.get("extensions"), path="candidate.extensions")
            )
    errors.extend(_candidate_structural_invariants(raw))
    return errors


def validate_position_state_dict(raw: Mapping[str, Any] | None) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, Mapping):
        return ["position_state must be an object or null"]
    errors = _unknown_keys(raw, POSITION_STATE_FIELDS, "position_state")
    if "position_open" in raw and raw.get("position_open") is not None:
        errors.extend(
            _require_bool(raw.get("position_open"), field="position_state.position_open")
        )
    if "extensions" in raw and raw.get("extensions") is not None:
        if not isinstance(raw.get("extensions"), Mapping):
            errors.append("position_state: extensions must be an object")
        else:
            errors.extend(
                _scan_prohibited_keys(
                    raw.get("extensions"), path="position_state.extensions"
                )
            )
    return errors


def validate_recommendation_dict(raw: Mapping[str, Any]) -> list[str]:
    if not isinstance(raw, Mapping):
        return ["recommendation must be an object"]
    errors = _unknown_keys(raw, RECOMMENDATION_FIELDS, "recommendation")
    errors.extend(
        _require_keys(
            raw,
            (
                "contract_version",
                "outcome_kind",
                "recommendation_id",
                "decision_time",
                "primary_engine",
                "primary_candidate_id",
                "rationale",
            ),
            "recommendation",
        )
    )
    if raw.get("outcome_kind") != DecisionOutcomeKind.RECOMMENDATION.value:
        errors.append("recommendation: outcome_kind must be RECOMMENDATION")
    if "contract_version" in raw:
        errors.extend(
            _check_version(
                raw.get("contract_version"),
                reader_version=RECOMMENDATION_CONTRACT_VERSION,
                label="recommendation.contract_version",
            )
        )
    if "recommendation_id" in raw:
        errors.extend(
            _non_empty_str(raw.get("recommendation_id"), field="recommendation.recommendation_id")
        )
    if "primary_candidate_id" in raw:
        errors.extend(
            _non_empty_str(
                raw.get("primary_candidate_id"),
                field="recommendation.primary_candidate_id",
            )
        )
    if "rationale" in raw:
        errors.extend(
            _non_empty_str(raw.get("rationale"), field="recommendation.rationale")
        )
    errors.extend(
        _enum_value(EngineId, raw.get("primary_engine"), field="recommendation.primary_engine")
    )
    if raw.get("confirmation_engine") is not None:
        errors.extend(
            _enum_value(
                EngineId,
                raw.get("confirmation_engine"),
                field="recommendation.confirmation_engine",
            )
        )
    if "decision_time" in raw:
        errors.extend(
            _validate_utc_timestamp(
                raw.get("decision_time"), field="recommendation.decision_time"
            )
        )
    if "opportunity" in raw and raw.get("opportunity") is not None:
        errors.extend(validate_opportunity_dict(raw.get("opportunity")))
    if "position_state" in raw:
        errors.extend(validate_position_state_dict(raw.get("position_state")))
    if "extensions" in raw and raw.get("extensions") is not None:
        if not isinstance(raw.get("extensions"), Mapping):
            errors.append("recommendation: extensions must be an object")
        else:
            errors.extend(
                _scan_prohibited_keys(
                    raw.get("extensions"), path="recommendation.extensions"
                )
            )
    return errors


def validate_no_trade_dict(raw: Mapping[str, Any]) -> list[str]:
    if not isinstance(raw, Mapping):
        return ["no_trade must be an object"]
    errors = _unknown_keys(raw, NO_TRADE_FIELDS, "no_trade")
    errors.extend(
        _require_keys(
            raw,
            (
                "contract_version",
                "outcome_kind",
                "decision_id",
                "decision_time",
                "reason_code",
                "reason",
            ),
            "no_trade",
        )
    )
    if raw.get("outcome_kind") != DecisionOutcomeKind.NO_TRADE.value:
        errors.append("no_trade: outcome_kind must be NO_TRADE")
    errors.extend(
        _enum_value(NoTradeReasonCode, raw.get("reason_code"), field="no_trade.reason_code")
    )
    if "contract_version" in raw:
        errors.extend(
            _check_version(
                raw.get("contract_version"),
                reader_version=RECOMMENDATION_CONTRACT_VERSION,
                label="no_trade.contract_version",
            )
        )
    if "decision_id" in raw:
        errors.extend(_non_empty_str(raw.get("decision_id"), field="no_trade.decision_id"))
    if "reason" in raw:
        errors.extend(_non_empty_str(raw.get("reason"), field="no_trade.reason"))
    if "decision_time" in raw:
        errors.extend(
            _validate_utc_timestamp(raw.get("decision_time"), field="no_trade.decision_time")
        )
    if "position_state" in raw:
        errors.extend(validate_position_state_dict(raw.get("position_state")))
    if "extensions" in raw and raw.get("extensions") is not None:
        if not isinstance(raw.get("extensions"), Mapping):
            errors.append("no_trade: extensions must be an object")
        else:
            errors.extend(
                _scan_prohibited_keys(raw.get("extensions"), path="no_trade.extensions")
            )
    return errors


def build_evidence_by_family_from_candidates(
    candidates: list[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Deterministic family → evidence payloads reconstruction (no scoring)."""

    by_family: dict[str, list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        for item in candidate.get("evidence") or []:
            if not isinstance(item, Mapping):
                continue
            eid = item.get("evidence_id")
            family = item.get("family")
            if not isinstance(eid, str) or not isinstance(family, str):
                continue
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            by_family.setdefault(family, []).append(dict(item))
    return {family: by_family[family] for family in sorted(by_family)}


def validate_decision_record_dict(raw: Mapping[str, Any]) -> list[str]:
    if not isinstance(raw, Mapping):
        return ["decision_record must be an object"]
    errors = _unknown_keys(raw, DECISION_RECORD_FIELDS, "decision_record")
    errors.extend(
        _require_keys(
            raw,
            (
                "schema_version",
                "interface_version",
                "decision_id",
                "decision_time",
                "outcome_kind",
            ),
            "decision_record",
        )
    )
    if "schema_version" in raw:
        errors.extend(
            _check_version(
                raw.get("schema_version"),
                reader_version=DECISION_RECORD_SCHEMA_VERSION,
                label="decision_record.schema_version",
            )
        )
    if "interface_version" in raw:
        errors.extend(
            _check_version(
                raw.get("interface_version"),
                reader_version=SCHEMA_VERSION,
                label="decision_record.interface_version",
            )
        )
    if "decision_id" in raw:
        errors.extend(
            _non_empty_str(raw.get("decision_id"), field="decision_record.decision_id")
        )
    errors.extend(
        _enum_value(
            DecisionOutcomeKind, raw.get("outcome_kind"), field="decision_record.outcome_kind"
        )
    )
    if "decision_time" in raw:
        errors.extend(
            _validate_utc_timestamp(
                raw.get("decision_time"), field="decision_record.decision_time"
            )
        )
    candidates = raw.get("candidates", [])
    if candidates is None:
        candidates = []
    if not isinstance(candidates, list):
        errors.append("decision_record: candidates must be a list")
        candidates = []
    else:
        for index, item in enumerate(candidates):
            for problem in validate_candidate_dict(item):
                errors.append(f"decision_record.candidates[{index}]: {problem}")
    groups = raw.get("correlated_evidence_groups", [])
    if groups is None:
        groups = []
    if not isinstance(groups, list):
        errors.append("decision_record: correlated_evidence_groups must be a list")
    else:
        for index, item in enumerate(groups):
            if not isinstance(item, Mapping):
                errors.append(
                    f"decision_record: correlated_evidence_groups[{index}] must be an object"
                )
                continue
            for problem in validate_correlated_group_dict(item):
                errors.append(
                    f"decision_record.correlated_evidence_groups[{index}]: {problem}"
                )
    if "evidence_by_family" in raw and raw.get("evidence_by_family") is not None:
        ebf = raw.get("evidence_by_family")
        if not isinstance(ebf, Mapping):
            errors.append("decision_record: evidence_by_family must be an object")
        else:
            expected = build_evidence_by_family_from_candidates(
                [c for c in candidates if isinstance(c, Mapping)]
            )
            # Compare family keys and evidence_id sets for lossless consistency.
            expected_ids = {
                fam: [item.get("evidence_id") for item in items]
                for fam, items in expected.items()
            }
            actual_ids: dict[str, list[Any]] = {}
            for fam, items in ebf.items():
                if not isinstance(items, list):
                    errors.append(
                        f"decision_record: evidence_by_family[{fam!r}] must be a list"
                    )
                    continue
                actual_ids[str(fam)] = []
                for index, item in enumerate(items):
                    if not isinstance(item, Mapping):
                        errors.append(
                            f"decision_record: evidence_by_family[{fam!r}][{index}] "
                            "must be an object"
                        )
                        continue
                    for problem in validate_confidence_evidence_dict(item):
                        errors.append(
                            f"decision_record.evidence_by_family[{fam!r}][{index}]: "
                            f"{problem}"
                        )
                    actual_ids[str(fam)].append(item.get("evidence_id"))
            if expected_ids != actual_ids:
                errors.append(
                    "decision_record: evidence_by_family inconsistent with candidates"
                )
    outcome = raw.get("outcome_kind")
    if outcome == DecisionOutcomeKind.RECOMMENDATION.value:
        if raw.get("recommendation") is None:
            errors.append("decision_record: RECOMMENDATION requires recommendation object")
        else:
            errors.extend(validate_recommendation_dict(raw["recommendation"]))
        if raw.get("no_trade") is not None:
            errors.append("decision_record: RECOMMENDATION must not include no_trade")
    elif outcome == DecisionOutcomeKind.NO_TRADE.value:
        if raw.get("no_trade") is None:
            errors.append("decision_record: NO_TRADE requires no_trade object")
        else:
            errors.extend(validate_no_trade_dict(raw["no_trade"]))
        if raw.get("recommendation") is not None:
            errors.append("decision_record: NO_TRADE must not include recommendation")
    if "deprecated_fields" in raw and raw["deprecated_fields"] is not None:
        if not isinstance(raw["deprecated_fields"], Mapping):
            errors.append("decision_record: deprecated_fields must be an object")
        else:
            errors.extend(
                _scan_prohibited_keys(
                    raw["deprecated_fields"], path="decision_record.deprecated_fields"
                )
            )
    if "extensions" in raw and raw["extensions"] is not None:
        if not isinstance(raw["extensions"], Mapping):
            errors.append("decision_record: extensions must be an object")
        else:
            errors.extend(
                _scan_prohibited_keys(
                    raw["extensions"], path="decision_record.extensions"
                )
            )
    if "position_state" in raw:
        errors.extend(validate_position_state_dict(raw.get("position_state")))
    return errors


# Silence unused import warning for parse_semver re-export consumers.
_ = parse_semver
