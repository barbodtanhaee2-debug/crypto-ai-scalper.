# Stage 3.3.2.2 — Shared Interfaces Specification (Stage A)

**Stage type:** Implementation — shared contracts only  
**Package:** `research_lab.adaptive`  
**Schema / interface versions (initial):** `1.0.0` (retained through Stage 3.3.2.4 integrity remediation)  
**Date:** 2026-11-06  
**Integrity remediation:** Stage 3.3.2.4 (validators enforce documented fail-closed behavior)

This document freezes the research contracts for adaptive signal discovery Stage A. It does **not** authorize strategy engines, controller behavior, historical research, scoring, or paper-trading integration.

Source of truth for policy intent: `STAGE3.3.2.1_ADAPTIVE_ARCHITECTURE_REFINEMENT.md` and Stage 3.3.2 specification (preserved unmodified).

Remediation details: `reports/STAGE3.3.2.4_CONTRACT_INTEGRITY_REMEDIATION_REPORT.md`.

---

## 1. Boundaries

| Layer | Authority |
| --- | --- |
| Candidate evaluation | Future engines emit `StrategyCandidate` |
| Recommendation / `NO_TRADE` | Future adaptive controller |
| Risk approval | Existing risk engine (unchanged) |
| Execution | PaperBroker only (unchanged) |

A `StrategyRecommendation` **never** grants permission to place an order. Incomplete costs, unknown statuses, and unsupported major versions **fail closed**.

Pillars remain separate and must not collapse into one score:

1. Eligibility  
2. Signal / evidence confidence  
3. Opportunity quality (cost-adjusted)  
4. Risk approval  

---

## 2. Versioning policy

Explicit constants:

| Constant | Initial value | Applies to |
| --- | --- | --- |
| `SCHEMA_VERSION` / `interface_version` | `1.0.0` | Adaptive interface package |
| `CANDIDATE_CONTRACT_VERSION` | `1.0.0` | Candidate, evidence, opportunity |
| `RECOMMENDATION_CONTRACT_VERSION` | `1.0.0` | Recommendation and `NO_TRADE` |
| `DECISION_RECORD_SCHEMA_VERSION` | `1.0.0` | Serialized decision records (`schema_version`) |

Semantic versioning:

- **Major:** breaking structural or semantic changes. Unsupported major → fail closed.  
- **Minor:** backward-compatible additions (optional fields with safe defaults; new statuses that do not reinterpret existing ones).  
- **Patch:** corrections that do not change meaning.

Compatibility rule (reader at `X.Y.Z`):

- Accept same major `X` with record `minor <= Y`.  
- Higher writer minor accepted only if reader sets `allow_newer_minor=True` (default **False** for research tools).  
- Different major → reject.

### Compatible changes

- Adding an optional field with a safe default.  
- Adding a new recognized status without changing existing status meanings.  
- Preserving unknown optional metadata under `extensions`.  
- Recording retired fields in `deprecated_fields` during a transition window.

### Incompatible / fail-closed

- Removing or reinterpreting required fields.  
- Field renames or type changes (treated as potentially breaking → major).  
- Missing required fields.  
- Unknown signal statuses interpreted as valid.  
- Unsupported major schema / contract versions.  
- Silent coercion of incompatible payloads into valid candidates or recommendations.  
- Probability-of-profit / confidence-percent measurement keys.

### Deprecated-field policy

1. Writers stop emitting the field; readers may still accept it via `deprecated_fields` or documented legacy keys.  
2. Readers must not *require* deprecated fields.  
3. After a documented transition window, removal is a **major** version bump.

### Unknown fields and statuses

- Prefer `extensions` for forward-compatible optional metadata.  
- Unknown optional metadata in `extensions` is preserved on round-trip.  
- Unknown signal statuses / enum values fail validation (not silently valid).  
- **Enforced:** each contract has a fixed recognized field set; unknown top-level keys are rejected (fail closed). Forward-compatible optional metadata must use `extensions`.  
- **Enforced:** nested contracts (opportunity, evidence, groups, position state, etc.) use the same unknown-field policy.  
- **Enforced:** prohibited confidence keys (`probability_of_profit`, `confidence_percent`, `win_probability`) are rejected in `measurements`, `extensions`, and nested mappings (including `deprecated_fields`).  
- SemVer components must be non-negative integers **without leading zeros** (except `0`). Malformed versions are reported distinctly from unsupported versions.

### Nested contract versions

- `CostAdjustedOpportunity.contract_version` and `ConfidenceEvidence.contract_version` are independently compatibility-checked against `CANDIDATE_CONTRACT_VERSION`.  
- `AdaptiveDecisionRecord.schema_version` is checked against `DECISION_RECORD_SCHEMA_VERSION`.  
- `AdaptiveDecisionRecord.interface_version` is checked against `SCHEMA_VERSION`.  
- Recommendation / `NO_TRADE` `contract_version` is checked against `RECOMMENDATION_CONTRACT_VERSION`.

### Structural invariants (enforced)

- `INELIGIBLE` cannot have `eligible=true`; `VALID_CANDIDATE` / `CONFIRMED_SIGNAL` cannot have `eligible=false`.  
- Duplicate `evidence_id` values within a candidate are rejected.  
- Correlated group members must reference evidence IDs present on that candidate.  
- `VALID_CANDIDATE` / `CONFIRMED_SIGNAL` cannot have *only* conflicting evidence.  
- `OPPORTUNITY` requires `costs_complete=true` (strict boolean), all fraction fields non-null, finite numbers, and structural arithmetic consistency: `total = fee+spread+slippage`, `net = gross - total` (epsilon `1e-9`).  
- Cost component fractions and cost-assumption rates must be finite and non-negative; gross/net may be signed.  
- `costs_complete` must be a real JSON/Python boolean — strings/ints/null are rejected (no `bool(value)` coercion).

### Deferred to future controller (not structural contract errors)

- Multi-engine conflict resolution and selection priority.  
- Correlation geometry / thresholds / cross-family credit weights.  
- Whether mixed conflicting+satisfied evidence should demote status (beyond the only-conflicting case).  
- Exact broker/risk position-snapshot field mapping.  
- Minimum net-edge trading threshold (not invented here).

### Construction and immutability

- Dataclass `__post_init__` validates the serialized form (fail closed).  
- Preferred dictionary entry points: `*_from_dict` / `create_*` in `construct.py`.  
- Mapping fields (`extensions`, measurements, etc.) are recursively frozen (`MappingProxyType`); external mutation raises `TypeError`.

---

## 3. Serialization format

- In-memory: frozen dataclasses with frozen nested mappings.  
- Wire / storage: JSON objects via `to_dict()` / `*_from_dict()`.  
- Canonical text: `dumps_canonical` — `json.dumps(..., separators=(",", ":"), sort_keys=True, allow_nan=False)`.  
- Timestamps: timezone-aware UTC ISO-8601 strings (`datetime.isoformat()` after UTC normalize). Naive datetimes are rejected. Non-string timestamps fail validation (validators and loaders agree).  
- Decision records always emit `evidence_by_family` (deterministic family → evidence payloads reconstructed from candidates when not supplied). If supplied, it must be consistent with candidates.  
- Proposed research storage (not frozen): JSONL under `research-lab/experiments/runs/`, one decision record object per line, each carrying `schema_version`.  

---

## 4. Signal status vocabulary

Centralized in `SignalStatus`:

| Value | Meaning |
| --- | --- |
| `VALID_CANDIDATE` | Structurally valid candidate for controller consideration |
| `CONFIRMATION_ONLY` | Usable as confirmation / secondary, not independent primary |
| `INELIGIBLE` | Engine not eligible under current characterization |
| `NO_SIGNAL` | Eligible or evaluated, but no signal |
| `PENDING_CONFIRMATION` | Awaiting confirmation evidence |
| `CONFIRMED_SIGNAL` | Confirmed candidate signal |
| `INVALIDATED` | Previously valid path invalidated |
| `EXPIRED` | Opportunity / signal expired |
| `REJECTED_OPPORTUNITY` | Rejected on opportunity / economics grounds |
| `MISSING_EVIDENCE` | Required evidence unavailable |
| `CONFLICTING_EVIDENCE` | Evidence conflict |
| `STALE_OR_INVALID_INPUT` | Stale or invalid market / input data |

`NON_ACTIONABLE_SIGNAL_STATUSES` documents statuses that must never be treated as actionable primary proposals. Stage A does not implement selection logic.

---

## 5. Contracts

### 5.1 `ConfidenceEvidence`

| Field | Required | Notes |
| --- | --- | --- |
| `contract_version` | yes | Semver |
| `evidence_id` | yes | Non-empty string |
| `family` | yes | `EvidenceFamily` |
| `kind` | yes | `INDEPENDENT` / `CORRELATED` / `CONFIRMATION` / `MISSING` / `CONFLICTING` |
| `status` | yes | `EvidenceStatus` |
| `direction` | no | `TradeDirection` or null |
| `source_reference` | no | Opaque research reference |
| `explanation` | no | Human-readable |
| `rejection_reason` | no | Human-readable |
| `measurements` | no | Extensible; forbids PoP / confidence % keys |
| `extensions` | no | Unknown optional metadata |

Evidence families (extensible vocabulary): `MARKET_REGIME`, `DIRECTIONAL_STRUCTURE`, `MOMENTUM`, `VOLATILITY`, `PRICE_ACTION`, `DATA_QUALITY`, `OPPORTUNITY_ECONOMICS`.

Architecture rule preserved: correlated evidence must not receive multiple independent confidence credits. Geometry / thresholds are **not** finalized here.

### 5.2 `CorrelatedEvidenceGroup`

| Field | Required | Notes |
| --- | --- | --- |
| `group_id` | yes | |
| `family` | yes | |
| `member_evidence_ids` | yes | List of `evidence_id` |
| `counts_as_single_credit` | no | Default `true` |
| `note` | no | |
| `extensions` | no | Future correlation detail |

### 5.3 `CostAssumptions` / `CostAdjustedOpportunity`

Cost defaults reuse **BacktestSettings** field meanings (not a second fee model; PaperBroker economics unchanged):

- `fee_rate_per_side = 0.001`  
- `spread_fraction = 0.0002`  
- `slippage_rate_per_side = 0.0005`  

| Field | Required | Notes |
| --- | --- | --- |
| `contract_version` | yes | |
| `status` | yes | `OpportunityAssessmentStatus` |
| `gross_move_fraction` | yes (nullable) | Gross proxy; **not** a forecast |
| `fee_cost_fraction` | yes (nullable) | |
| `spread_cost_fraction` | yes (nullable) | |
| `slippage_cost_fraction` | yes (nullable) | |
| `total_cost_fraction` | yes (nullable) | |
| `net_move_fraction` | yes (nullable) | Diagnostic; incomplete costs cannot be `OPPORTUNITY` |
| `cost_assumptions` | yes | Includes `costs_complete` |
| `gross_proxy_label` | no | e.g. ATR baseline proxy label |
| `is_forecast` | no | Must be `false` for research proxies |
| `uses_historical_mfe_as_guarantee` | no | Must be `false` |
| `reason` | no | |
| `extensions` | no | |

Statuses: `OPPORTUNITY`, `NO_OPPORTUNITY`, `REJECTED_COSTS_TOO_HIGH`, `INCOMPLETE_COST_INFORMATION`, `UNAVAILABLE`.

**Rule:** `costs_complete=false` cannot validate as `status=OPPORTUNITY`.

### 5.4 `StrategyCandidate`

| Field | Required | Notes |
| --- | --- | --- |
| `contract_version` | yes | |
| `candidate_id` | yes | |
| `engine_id` | yes | `S1`–`S4` |
| `direction` | yes | Stage A: `long` |
| `signal_status` | yes | Vocabulary above |
| `decision_time` | yes | UTC |
| `market_data_ref` | no | Opaque reference |
| `characterization_ref` | no | Opaque Stage 3.3.1 reference |
| `eligible` | no | Separate from confidence / opportunity |
| `eligibility_reason` | no | |
| `evidence` | no | List of `ConfidenceEvidence` |
| `correlated_groups` | no | List of `CorrelatedEvidenceGroup` |
| `opportunity` | no | `CostAdjustedOpportunity` |
| `rejection_reason` | no | |
| `required_market_characteristics` | no | Map of characteristic → value |
| `entry_reference` | no | Opaque |
| `invalidation_reference` | no | Opaque |
| `opportunity_expiry` | no | Opaque |
| `extensions` | no | Engine-specific future metadata |

Not an execution permit.

### 5.5 `StrategyRecommendation`

| Field | Required | Notes |
| --- | --- | --- |
| `contract_version` | yes | |
| `outcome_kind` | yes | Always `RECOMMENDATION` on wire |
| `recommendation_id` | yes | |
| `decision_time` | yes | UTC |
| `primary_engine` | yes | |
| `primary_candidate_id` | yes | |
| `rationale` | yes | |
| `confirmation_engine` | no | Confirmation-only path |
| `confirmation_candidate_id` | no | |
| `supporting_candidate_ids` | no | |
| `rejected_candidate_ids` | no | |
| `rejected_reasons` | no | |
| `opportunity` | no | Cost assessment snapshot |
| `position_state` | no | Read-only placeholder |
| `position_constraints_applied` | no | Labels only |
| `extensions` | no | |

Does **not** authorize orders.

### 5.6 `NoTradeDecision` (`NO_TRADE`)

First-class valid decision (not a missing recommendation).

| Field | Required | Notes |
| --- | --- | --- |
| `contract_version` | yes | |
| `outcome_kind` | yes | Always `NO_TRADE` |
| `decision_id` | yes | |
| `decision_time` | yes | UTC |
| `reason_code` | yes | See below |
| `reason` | yes | Human-readable |
| `considered_candidate_ids` | no | |
| `ineligible_engines` | no | engine → reason |
| `position_state` | no | |
| `position_constraints_applied` | no | |
| `extensions` | no | |

Reason codes: `NO_ELIGIBLE_CANDIDATE`, `INSUFFICIENT_EVIDENCE`, `CONFLICTING_EVIDENCE`, `INCOMPLETE_COST_INFORMATION`, `STALE_OR_INVALID_MARKET_DATA`, `POSITION_STATE_RESTRICTION`, `REGIME_UNKNOWN_OR_TRANSITIONING`, `CONTROLLER_POLICY`, `OTHER`.

### 5.7 `PositionStateSnapshot`

Optional read-only placeholder (`position_open`, `direction`, `strategy_tag`, `reference`, `notes`, `extensions`). Stage A does not fetch broker/risk APIs. Exact field mapping remains an unresolved architecture dependency.

### 5.8 `AdaptiveDecisionRecord`

Versioned serializable record for offline reconstruction.

| Field | Required | Notes |
| --- | --- | --- |
| `schema_version` | yes | Decision-record schema |
| `interface_version` | yes | Package interface version |
| `decision_id` | yes | |
| `decision_time` | yes | UTC |
| `outcome_kind` | yes | `RECOMMENDATION` or `NO_TRADE` |
| `market_data_ref` | no | |
| `characterization_ref` | no | |
| `data_quality_status` | no | Opaque / Stage 3.3.1 aligned string |
| `primary_regime` | no | |
| `secondary_characteristics` | no | |
| `eligible_engines` | no | |
| `ineligible_engines` | no | |
| `candidates` | no | Embedded candidates |
| `correlated_evidence_groups` | no | Record-level groups |
| `evidence_by_family` | no (emitted) | Family → evidence payloads; reconstructed if omitted; must match candidates if present |
| `recommendation` | conditional | Required iff `RECOMMENDATION` |
| `no_trade` | conditional | Required iff `NO_TRADE` |
| `position_state` | no | |
| `controller_config_version` | no | Placeholder until controller exists |
| `reason` | no | Final decision reason |
| `extensions` | no | |
| `deprecated_fields` | no | Transition bag |

Mutual exclusion: `RECOMMENDATION` must not include `no_trade`; `NO_TRADE` must not include `recommendation`.

---

## 6. Validation rules (summary)

- Missing required fields → error list; loaders raise `ValueError`.  
- Incorrect types (e.g. non-string `decision_time`) → error.  
- Naive / non-UTC timestamps → reject.  
- Unknown enums / signal statuses → reject.  
- Unsupported major versions → reject.  
- Forbidden measurement keys → reject.  
- Incomplete costs + `OPPORTUNITY` → reject.  
- Non-finite or negative cost assumption rates → reject.  
- Validation is deterministic for the same input dict.

API: `validate_*_dict` returns `list[str]`; empty means valid.

---

## 7. Examples

### Valid minimal `NO_TRADE` decision record (conceptual)

```json
{
  "schema_version": "1.0.0",
  "interface_version": "1.0.0",
  "decision_id": "d1",
  "decision_time": "2026-10-30T12:00:00+00:00",
  "outcome_kind": "NO_TRADE",
  "recommendation": null,
  "no_trade": {
    "contract_version": "1.0.0",
    "outcome_kind": "NO_TRADE",
    "decision_id": "d1",
    "decision_time": "2026-10-30T12:00:00+00:00",
    "reason_code": "INSUFFICIENT_EVIDENCE",
    "reason": "no confirming family credit"
  },
  "reason": "insufficient evidence"
}
```

### Invalid examples

- Missing `candidate_id`.  
- `signal_status`: `"TOTALLY_UNKNOWN"`.  
- `schema_version`: `"2.0.0"` with reader `1.0.0`.  
- `opportunity.status`: `"OPPORTUNITY"` with `costs_complete: false`.  
- `measurements.probability_of_profit` present.  
- Naive `decision_time` datetime at construction.

---

## 8. Unresolved architecture dependencies

| Item | Stage A handling |
| --- | --- |
| Correlation geometry / thresholds | Extensible groups only; no thresholds |
| Confidence calibration | Not represented as probabilities |
| Exact broker/risk position snapshot mapping | `PositionStateSnapshot` placeholder |
| Controller selection policy | Out of scope (Stage C+) |
| Engine-specific gross proxies beyond ATR baseline research | Label + extensions; not forecasts |
| JSONL storage path conventions | Proposed, not frozen |
| Min net edge numeric threshold | Not invented; reuse future approved 3.3.1 config when engines exist |

---

## 9. Module map

| Module | Role |
| --- | --- |
| `versioning.py` | Semver + compatibility |
| `enums.py` | Vocabularies |
| `timeutil.py` | UTC helpers |
| `evidence.py` | Evidence + correlation groups |
| `opportunity.py` | Cost assumptions + opportunity |
| `candidate.py` | `StrategyCandidate` |
| `recommendation.py` | Recommendation + `NO_TRADE` + position snapshot |
| `decision_record.py` | `AdaptiveDecisionRecord` |
| `validation.py` | Fail-closed validators |
| `serialize.py` | Canonical JSON + loaders |

---

## 10. Explicit non-goals (Stage A)

No S1–S4 engines, no adaptive controller, no strategy switching, no historical candle replay, no event capture, no sealed-holdout access, no paper-trading integration, no production strategy changes.
