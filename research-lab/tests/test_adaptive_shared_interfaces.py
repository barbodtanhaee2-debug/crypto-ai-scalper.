"""Stage 3.3.2.2 / 3.3.2.4 — synthetic tests for adaptive shared contracts."""

from __future__ import annotations

import copy
import math
import unittest
from datetime import datetime, timezone
from types import MappingProxyType

from research_lab.adaptive import (
    CANDIDATE_CONTRACT_VERSION,
    DECISION_RECORD_SCHEMA_VERSION,
    RECOMMENDATION_CONTRACT_VERSION,
    SCHEMA_VERSION,
    AdaptiveDecisionRecord,
    ConfidenceEvidence,
    CorrelatedEvidenceGroup,
    CostAdjustedOpportunity,
    CostAssumptions,
    DecisionOutcomeKind,
    EngineId,
    EvidenceFamily,
    EvidenceKind,
    EvidenceStatus,
    NoTradeDecision,
    NoTradeReasonCode,
    OpportunityAssessmentStatus,
    SignalStatus,
    StrategyCandidate,
    StrategyRecommendation,
    TradeDirection,
    build_evidence_by_family_from_candidates,
    candidate_from_dict,
    classify_version,
    create_candidate,
    decision_record_from_dict,
    dumps_canonical,
    is_compatible,
    loads_object,
    no_trade_from_dict,
    parse_semver,
    recommendation_from_dict,
    validate_candidate_dict,
    validate_decision_record_dict,
    validate_no_trade_dict,
    validate_opportunity_dict,
    validate_recommendation_dict,
    validate_signal_status,
)


UTC = timezone.utc
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _opportunity(**overrides) -> CostAdjustedOpportunity:
    base = dict(
        status=OpportunityAssessmentStatus.OPPORTUNITY,
        gross_move_fraction=0.01,
        fee_cost_fraction=0.002,
        spread_cost_fraction=0.0002,
        slippage_cost_fraction=0.001,
        total_cost_fraction=0.0032,
        net_move_fraction=0.0068,
        cost_assumptions=CostAssumptions(),
        gross_proxy_label="atr_baseline_proxy",
        is_forecast=False,
        uses_historical_mfe_as_guarantee=False,
        reason="synthetic",
    )
    base.update(overrides)
    return CostAdjustedOpportunity(**base)  # type: ignore[arg-type]


def _evidence(eid: str = "e1") -> ConfidenceEvidence:
    return ConfidenceEvidence(
        evidence_id=eid,
        family=EvidenceFamily.MOMENTUM,
        kind=EvidenceKind.CORRELATED,
        status=EvidenceStatus.SATISFIED,
        direction=TradeDirection.LONG,
        explanation="synthetic",
    )


def _candidate(**overrides) -> StrategyCandidate:
    base = dict(
        candidate_id="c1",
        engine_id=EngineId.S4,
        direction=TradeDirection.LONG,
        signal_status=SignalStatus.CONFIRMED_SIGNAL,
        decision_time=T0,
        market_data_ref="md:1",
        characterization_ref="ch:1",
        eligible=True,
        evidence=(_evidence(),),
        correlated_groups=(
            CorrelatedEvidenceGroup(
                group_id="g1",
                family=EvidenceFamily.MOMENTUM,
                member_evidence_ids=("e1",),
            ),
        ),
        opportunity=_opportunity(),
    )
    base.update(overrides)
    return StrategyCandidate(**base)  # type: ignore[arg-type]


class VersioningTests(unittest.TestCase):
    def test_schema_versions_defined(self):
        self.assertEqual(SCHEMA_VERSION, "1.0.0")
        self.assertEqual(CANDIDATE_CONTRACT_VERSION, "1.0.0")
        self.assertEqual(RECOMMENDATION_CONTRACT_VERSION, "1.0.0")
        self.assertEqual(DECISION_RECORD_SCHEMA_VERSION, "1.0.0")

    def test_compatible_same_and_older_minor(self):
        self.assertTrue(is_compatible("1.0.0", reader_version="1.0.0"))
        self.assertTrue(is_compatible("1.0.3", reader_version="1.1.0"))
        self.assertFalse(is_compatible("1.2.0", reader_version="1.1.0"))
        self.assertTrue(
            is_compatible("1.2.0", reader_version="1.1.0", allow_newer_minor=True)
        )
        self.assertFalse(is_compatible("2.0.0", reader_version="1.0.0"))

    def test_leading_zeros_rejected(self):
        with self.assertRaises(ValueError):
            parse_semver("01.0.0")
        self.assertEqual(classify_version("01.0.0", reader_version="1.0.0"), "malformed")

    def test_classify_malformed_vs_unsupported(self):
        self.assertEqual(classify_version("nope", reader_version="1.0.0"), "malformed")
        self.assertEqual(classify_version("2.0.0", reader_version="1.0.0"), "unsupported")
        self.assertEqual(classify_version("1.0.0", reader_version="1.0.0"), "ok")


class ContractConstructionTests(unittest.TestCase):
    def test_valid_candidate(self):
        candidate = _candidate()
        payload = candidate.to_dict()
        self.assertEqual(validate_candidate_dict(payload), [])
        restored = candidate_from_dict(payload)
        self.assertEqual(restored.candidate_id, "c1")
        self.assertEqual(restored.engine_id, EngineId.S4)

    def test_missing_required_candidate_fields(self):
        payload = _candidate().to_dict()
        del payload["candidate_id"]
        errors = validate_candidate_dict(payload)
        self.assertTrue(any("candidate_id" in e for e in errors))
        with self.assertRaises(ValueError):
            candidate_from_dict(payload)

    def test_incorrect_field_types(self):
        payload = _candidate().to_dict()
        payload["decision_time"] = 123
        errors = validate_candidate_dict(payload)
        self.assertTrue(any("decision_time" in e for e in errors))

    def test_naive_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            StrategyCandidate(
                candidate_id="c",
                engine_id=EngineId.S1,
                direction=TradeDirection.LONG,
                signal_status=SignalStatus.NO_SIGNAL,
                decision_time=datetime(2026, 10, 30, 12, 0),
            )

    def test_signal_status_validation(self):
        self.assertEqual(validate_signal_status("CONFIRMED_SIGNAL"), [])
        self.assertTrue(validate_signal_status("NOT_A_STATUS"))

    def test_unknown_signal_status_not_silently_valid(self):
        payload = _candidate().to_dict()
        payload["signal_status"] = "TOTALLY_UNKNOWN"
        errors = validate_candidate_dict(payload)
        self.assertTrue(any("unknown value" in e for e in errors))
        with self.assertRaises(ValueError):
            candidate_from_dict(payload)

    def test_confidence_evidence_forbids_pop_keys(self):
        payload = _candidate().to_dict()
        payload["evidence"][0]["measurements"] = {"probability_of_profit": 0.9}
        errors = validate_candidate_dict(payload)
        self.assertTrue(any("prohibited key" in e for e in errors))

    def test_correlation_family_serialization(self):
        group = CorrelatedEvidenceGroup(
            group_id="g",
            family=EvidenceFamily.DIRECTIONAL_STRUCTURE,
            member_evidence_ids=("a", "b"),
            counts_as_single_credit=True,
        )
        self.assertTrue(group.to_dict()["counts_as_single_credit"])

    def test_incomplete_costs_cannot_be_opportunity(self):
        raw = _opportunity().to_dict()
        raw["cost_assumptions"]["costs_complete"] = False
        errors = validate_opportunity_dict(raw)
        self.assertTrue(any("incomplete costs" in e for e in errors))

    def test_invalid_cost_inputs(self):
        raw = _opportunity().to_dict()
        raw["cost_assumptions"]["fee_rate_per_side"] = float("nan")
        errors = validate_opportunity_dict(raw)
        self.assertTrue(any("finite" in e for e in errors))

    def test_recommendation_validation(self):
        rec = StrategyRecommendation(
            recommendation_id="r1",
            decision_time=T0,
            primary_engine=EngineId.S4,
            primary_candidate_id="c1",
            rationale="synthetic",
            opportunity=_opportunity(),
        )
        self.assertEqual(validate_recommendation_dict(rec.to_dict()), [])
        restored = recommendation_from_dict(rec.to_dict())
        self.assertEqual(restored.primary_engine, EngineId.S4)

    def test_no_trade_validation(self):
        decision = NoTradeDecision(
            decision_id="n1",
            decision_time=T0,
            reason_code=NoTradeReasonCode.INSUFFICIENT_EVIDENCE,
            reason="synthetic no trade",
        )
        self.assertEqual(validate_no_trade_dict(decision.to_dict()), [])
        restored = no_trade_from_dict(decision.to_dict())
        self.assertEqual(restored.reason_code, NoTradeReasonCode.INSUFFICIENT_EVIDENCE)
        self.assertEqual(
            restored.to_dict()["outcome_kind"], DecisionOutcomeKind.NO_TRADE.value
        )


class HighFindingRegressionTests(unittest.TestCase):
    """H1 / H2 / H3 dedicated regressions."""

    def test_h1_nested_opportunity_unsupported_major(self):
        raw = _opportunity().to_dict()
        raw["contract_version"] = "2.0.0"
        errors = validate_opportunity_dict(raw)
        self.assertTrue(any("unsupported version" in e for e in errors))
        payload = _candidate().to_dict()
        payload["opportunity"] = raw
        self.assertTrue(validate_candidate_dict(payload))
        with self.assertRaises(ValueError):
            candidate_from_dict(payload)

    def test_h1_nested_opportunity_malformed_version(self):
        raw = _opportunity().to_dict()
        raw["contract_version"] = "not-a-version"
        errors = validate_opportunity_dict(raw)
        self.assertTrue(any("malformed version" in e for e in errors))

    def test_h1_nested_opportunity_newer_minor_rejected(self):
        raw = _opportunity().to_dict()
        raw["contract_version"] = "1.1.0"
        errors = validate_opportunity_dict(raw)
        self.assertTrue(any("unsupported version" in e for e in errors))

    def test_h1_nested_evidence_unsupported_major(self):
        payload = _candidate().to_dict()
        payload["evidence"][0]["contract_version"] = "2.0.0"
        errors = validate_candidate_dict(payload)
        self.assertTrue(any("unsupported version" in e for e in errors))

    def test_h2_costs_complete_boolean_matrix(self):
        cases = [
            (False, True),  # bool false with OPPORTUNITY → invalid
            (True, False),  # bool true → valid
            ("false", True),
            ("true", True),
            (0, True),
            (1, True),
            (None, True),
        ]
        for value, expect_errors in cases:
            raw = _opportunity().to_dict()
            if value is None:
                del raw["cost_assumptions"]["costs_complete"]
            else:
                raw["cost_assumptions"]["costs_complete"] = value
            errors = validate_opportunity_dict(raw)
            if expect_errors:
                self.assertTrue(errors, msg=f"expected errors for costs_complete={value!r}")
            else:
                self.assertEqual(errors, [], msg=f"unexpected errors for {value!r}: {errors}")

        # missing field
        raw = _opportunity().to_dict()
        del raw["cost_assumptions"]["costs_complete"]
        self.assertTrue(any("costs_complete" in e for e in validate_opportunity_dict(raw)))

        # loader cannot coerce string false into valid OPPORTUNITY
        raw = _opportunity().to_dict()
        raw["cost_assumptions"]["costs_complete"] = "false"
        with self.assertRaises(ValueError):
            from research_lab.adaptive.serialize import opportunity_from_dict

            opportunity_from_dict(raw)

    def test_h3_unknown_top_level_fields_rejected(self):
        payload = _candidate().to_dict()
        payload["probability_of_profit"] = 0.9
        payload["secret_score"] = 1
        errors = validate_candidate_dict(payload)
        self.assertTrue(any("unknown field" in e for e in errors))
        with self.assertRaises(ValueError):
            candidate_from_dict(payload)

    def test_h3_unknown_nested_opportunity_field_rejected(self):
        raw = _opportunity().to_dict()
        raw["confidence_percent"] = 80
        self.assertTrue(any("unknown field" in e for e in validate_opportunity_dict(raw)))

    def test_h3_extensions_still_allowed(self):
        payload = _candidate().to_dict()
        payload["extensions"] = {"research_note": "ok", "nested": {"a": 1}}
        self.assertEqual(validate_candidate_dict(payload), [])


class MediumFindingRegressionTests(unittest.TestCase):
    def test_m1_ineligible_with_eligible_true(self):
        with self.assertRaises(ValueError):
            _candidate(
                signal_status=SignalStatus.INELIGIBLE,
                eligible=True,
                opportunity=None,
                evidence=(),
                correlated_groups=(),
            )
        payload = _candidate(
            signal_status=SignalStatus.NO_SIGNAL,
            eligible=False,
            opportunity=None,
            evidence=(),
            correlated_groups=(),
        ).to_dict()
        payload["signal_status"] = SignalStatus.INELIGIBLE.value
        payload["eligible"] = True
        errors = validate_candidate_dict(payload)
        self.assertTrue(any("INELIGIBLE cannot have eligible=true" in e for e in errors))

    def test_m1_confirmed_with_only_conflicting_evidence(self):
        payload = _candidate().to_dict()
        payload["evidence"] = [
            ConfidenceEvidence(
                evidence_id="e1",
                family=EvidenceFamily.MOMENTUM,
                kind=EvidenceKind.CONFLICTING,
                status=EvidenceStatus.CONFLICTING,
            ).to_dict()
        ]
        payload["correlated_groups"] = []
        errors = validate_candidate_dict(payload)
        self.assertTrue(any("only conflicting evidence" in e for e in errors))

    def test_m1_duplicate_evidence_ids(self):
        payload = _candidate().to_dict()
        payload["evidence"] = [
            _evidence("e1").to_dict(),
            _evidence("e1").to_dict(),
        ]
        payload["correlated_groups"] = []
        errors = validate_candidate_dict(payload)
        self.assertTrue(any("duplicate evidence_id" in e for e in errors))

    def test_m1_group_unknown_member(self):
        payload = _candidate().to_dict()
        payload["correlated_groups"] = [
            CorrelatedEvidenceGroup(
                group_id="g",
                family=EvidenceFamily.MOMENTUM,
                member_evidence_ids=("missing",),
            ).to_dict()
        ]
        errors = validate_candidate_dict(payload)
        self.assertTrue(any("unknown evidence_id" in e for e in errors))

    def test_m1_opportunity_null_fractions(self):
        raw = _opportunity().to_dict()
        for key in (
            "gross_move_fraction",
            "fee_cost_fraction",
            "spread_cost_fraction",
            "slippage_cost_fraction",
            "total_cost_fraction",
            "net_move_fraction",
        ):
            raw[key] = None
        errors = validate_opportunity_dict(raw)
        self.assertTrue(any("non-null" in e for e in errors))

    def test_m1_arithmetic_inconsistency(self):
        raw = _opportunity().to_dict()
        raw["total_cost_fraction"] = 0.99
        errors = validate_opportunity_dict(raw)
        self.assertTrue(any("total_cost_fraction must equal" in e for e in errors))
        raw = _opportunity().to_dict()
        raw["net_move_fraction"] = 0.99
        errors = validate_opportunity_dict(raw)
        self.assertTrue(any("net_move_fraction must equal" in e for e in errors))

    def test_m2_interface_version(self):
        record = AdaptiveDecisionRecord(
            decision_id="d",
            decision_time=T0,
            outcome_kind=DecisionOutcomeKind.NO_TRADE,
            no_trade=NoTradeDecision(
                decision_id="d",
                decision_time=T0,
                reason_code=NoTradeReasonCode.OTHER,
                reason="x",
            ),
        )
        payload = record.to_dict()
        self.assertEqual(validate_decision_record_dict(payload), [])
        payload["interface_version"] = "2.0.0"
        self.assertTrue(any("unsupported version" in e for e in validate_decision_record_dict(payload)))
        payload["interface_version"] = "bad"
        self.assertTrue(any("malformed version" in e for e in validate_decision_record_dict(payload)))
        payload["interface_version"] = "1.1.0"
        self.assertTrue(any("unsupported version" in e for e in validate_decision_record_dict(payload)))

    def test_m3_recommendation_non_string_timestamp(self):
        raw = StrategyRecommendation(
            recommendation_id="r1",
            decision_time=T0,
            primary_engine=EngineId.S4,
            primary_candidate_id="c1",
            rationale="synthetic",
        ).to_dict()
        raw["decision_time"] = 123
        errors = validate_recommendation_dict(raw)
        self.assertTrue(errors)
        with self.assertRaises(ValueError):
            recommendation_from_dict(raw)

    def test_m3_no_trade_non_string_timestamp(self):
        raw = NoTradeDecision(
            decision_id="n1",
            decision_time=T0,
            reason_code=NoTradeReasonCode.OTHER,
            reason="x",
        ).to_dict()
        raw["decision_time"] = 123
        errors = validate_no_trade_dict(raw)
        self.assertTrue(errors)
        with self.assertRaises(ValueError):
            no_trade_from_dict(raw)

    def test_m4_non_finite_opportunity_values(self):
        raw = _opportunity().to_dict()
        raw["gross_move_fraction"] = float("nan")
        self.assertTrue(any("finite" in e for e in validate_opportunity_dict(raw)))
        raw = _opportunity().to_dict()
        raw["net_move_fraction"] = float("inf")
        self.assertTrue(any("finite" in e for e in validate_opportunity_dict(raw)))
        with self.assertRaises(ValueError):
            from research_lab.adaptive.serialize import opportunity_from_dict

            opportunity_from_dict(raw)

    def test_m5_evidence_by_family(self):
        record = AdaptiveDecisionRecord(
            decision_id="d1",
            decision_time=T0,
            outcome_kind=DecisionOutcomeKind.NO_TRADE,
            candidates=(_candidate(),),
            no_trade=NoTradeDecision(
                decision_id="d1",
                decision_time=T0,
                reason_code=NoTradeReasonCode.OTHER,
                reason="x",
            ),
        )
        payload = record.to_dict()
        self.assertIn("evidence_by_family", payload)
        self.assertIn("MOMENTUM", payload["evidence_by_family"])
        restored = decision_record_from_dict(payload)
        self.assertEqual(
            restored.resolved_evidence_by_family()["MOMENTUM"][0]["evidence_id"], "e1"
        )
        # Inconsistent index rejected
        payload["evidence_by_family"] = {"MOMENTUM": []}
        self.assertTrue(
            any("inconsistent" in e for e in validate_decision_record_dict(payload))
        )

    def test_m6_direct_construction_validates(self):
        with self.assertRaises(ValueError):
            StrategyCandidate(
                candidate_id="c",
                engine_id=EngineId.S1,
                direction=TradeDirection.LONG,
                signal_status=SignalStatus.INELIGIBLE,
                decision_time=T0,
                eligible=True,
            )
        created = create_candidate(_candidate().to_dict())
        self.assertEqual(created.candidate_id, "c1")

    def test_m7_extensions_immutable(self):
        candidate = _candidate(extensions={"a": 1})
        self.assertIsInstance(candidate.extensions, MappingProxyType)
        with self.assertRaises(TypeError):
            candidate.extensions["a"] = 2  # type: ignore[index]


class LowFindingTests(unittest.TestCase):
    def test_l1_empty_strings_rejected(self):
        with self.assertRaises(ValueError):
            NoTradeDecision(
                decision_id="n",
                decision_time=T0,
                reason_code=NoTradeReasonCode.OTHER,
                reason="",
            )
        raw = StrategyRecommendation(
            recommendation_id="r",
            decision_time=T0,
            primary_engine=EngineId.S1,
            primary_candidate_id="c",
            rationale="ok",
        ).to_dict()
        raw["primary_candidate_id"] = ""
        self.assertTrue(validate_recommendation_dict(raw))

    def test_l3_prohibited_keys_in_extensions(self):
        payload = _candidate().to_dict()
        payload["extensions"] = {"probability_of_profit": 0.5}
        self.assertTrue(any("prohibited key" in e for e in validate_candidate_dict(payload)))


class DecisionRecordTests(unittest.TestCase):
    def _record(self, *, no_trade: bool = False) -> AdaptiveDecisionRecord:
        candidate = _candidate()
        if no_trade:
            return AdaptiveDecisionRecord(
                decision_id="d1",
                decision_time=T0,
                outcome_kind=DecisionOutcomeKind.NO_TRADE,
                candidates=(candidate,),
                no_trade=NoTradeDecision(
                    decision_id="d1",
                    decision_time=T0,
                    reason_code=NoTradeReasonCode.NO_ELIGIBLE_CANDIDATE,
                    reason="none",
                ),
                reason="no trade",
            )
        return AdaptiveDecisionRecord(
            decision_id="d1",
            decision_time=T0,
            outcome_kind=DecisionOutcomeKind.RECOMMENDATION,
            data_quality_status="OK",
            primary_regime="TRENDING",
            secondary_characteristics={"volatility": "NORMAL_VOLATILITY"},
            eligible_engines=("S4",),
            candidates=(candidate,),
            recommendation=StrategyRecommendation(
                recommendation_id="r1",
                decision_time=T0,
                primary_engine=EngineId.S4,
                primary_candidate_id="c1",
                rationale="ok",
                opportunity=_opportunity(),
            ),
            reason="recommend S4",
        )

    def test_round_trip_deterministic(self):
        record = self._record()
        text1 = dumps_canonical(record.to_dict())
        text2 = dumps_canonical(record.to_dict())
        self.assertEqual(text1, text2)
        restored = decision_record_from_dict(loads_object(text1))
        self.assertEqual(dumps_canonical(restored.to_dict()), text1)

    def test_no_trade_record(self):
        record = self._record(no_trade=True)
        self.assertEqual(validate_decision_record_dict(record.to_dict()), [])
        restored = decision_record_from_dict(record.to_dict())
        self.assertIsNotNone(restored.no_trade)
        self.assertIsNone(restored.recommendation)

    def test_missing_required_decision_fields(self):
        payload = self._record().to_dict()
        del payload["decision_id"]
        errors = validate_decision_record_dict(payload)
        self.assertTrue(any("decision_id" in e for e in errors))


class CompatibilityTests(unittest.TestCase):
    def test_older_compatible_record_missing_optional_fields(self):
        payload = _candidate().to_dict()
        for key in (
            "market_data_ref",
            "characterization_ref",
            "extensions",
            "entry_reference",
            "invalidation_reference",
            "opportunity_expiry",
        ):
            payload.pop(key, None)
        errors = validate_candidate_dict(payload)
        self.assertEqual(errors, [])
        restored = candidate_from_dict(payload)
        self.assertIsNone(restored.market_data_ref)

    def test_newly_added_optional_fields_round_trip(self):
        candidate = _candidate(
            entry_reference="entry:100",
            invalidation_reference="inv:99",
            opportunity_expiry="bar:120",
            extensions={"research_note": "optional"},
        )
        restored = candidate_from_dict(candidate.to_dict())
        self.assertEqual(restored.entry_reference, "entry:100")
        self.assertEqual(restored.extensions["research_note"], "optional")

    def test_unknown_optional_metadata_preserved(self):
        payload = _candidate().to_dict()
        payload["extensions"] = {"future_field": 123, "nested": {"a": 1}}
        restored = candidate_from_dict(payload)
        self.assertEqual(restored.extensions["future_field"], 123)

    def test_unsupported_major_schema_rejected(self):
        record = AdaptiveDecisionRecord(
            decision_id="d",
            decision_time=T0,
            outcome_kind=DecisionOutcomeKind.NO_TRADE,
            no_trade=NoTradeDecision(
                decision_id="d",
                decision_time=T0,
                reason_code=NoTradeReasonCode.OTHER,
                reason="x",
            ),
        )
        payload = record.to_dict()
        payload["schema_version"] = "2.0.0"
        errors = validate_decision_record_dict(payload)
        self.assertTrue(any("unsupported" in e for e in errors))
        with self.assertRaises(ValueError):
            decision_record_from_dict(payload)

    def test_deprecated_fields_preserved(self):
        record = AdaptiveDecisionRecord(
            decision_id="d",
            decision_time=T0,
            outcome_kind=DecisionOutcomeKind.NO_TRADE,
            no_trade=NoTradeDecision(
                decision_id="d",
                decision_time=T0,
                reason_code=NoTradeReasonCode.OTHER,
                reason="x",
            ),
            deprecated_fields={"legacy_note": "kept"},
        )
        restored = decision_record_from_dict(record.to_dict())
        self.assertEqual(restored.deprecated_fields["legacy_note"], "kept")

    def test_reader_accepts_same_major_older_patch(self):
        payload = _candidate().to_dict()
        payload["contract_version"] = "1.0.0"
        self.assertEqual(validate_candidate_dict(payload), [])

    def test_deterministic_validation_results(self):
        payload = _candidate().to_dict()
        del payload["engine_id"]
        a = validate_candidate_dict(copy.deepcopy(payload))
        b = validate_candidate_dict(copy.deepcopy(payload))
        self.assertEqual(a, b)

    def test_build_evidence_by_family_helper(self):
        payload = _candidate().to_dict()
        grouped = build_evidence_by_family_from_candidates([payload])
        self.assertEqual(grouped["MOMENTUM"][0]["evidence_id"], "e1")


if __name__ == "__main__":
    unittest.main()
