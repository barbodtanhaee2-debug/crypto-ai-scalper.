# Stage 3.3.2.5 — Independent Audit of Stage A Contract Integrity Remediation

**Audit type:** Read-only verification of Stage 3.3.2.4 remediation  
**Date:** 2026-11-06  
**Repository:** `F:\crypto-ai-scalper`  
**Auditor posture:** Independent of remediation claims; credit only when source/tests prove the fix  

---

## 1. Executive verdict

**CONDITIONAL PASS**

Stage 3.3.2.4 **successfully remediates all prior HIGH findings (H1–H3)** and substantially remediates M1–M7 / L1–L5. Fail-closed versioning, boolean cost completeness, unknown-field rejection, structural candidate/opportunity invariants, interface-version checks, timestamp alignment, numeric finiteness, validated construction, and mapping/list freezing are confirmed in code and covered by regression tests.

Limited residual integrity weaknesses remain (notably shallow `evidence_by_family` consistency and incomplete deep-freeze of arbitrary non-container extension values). These do **not** recreate the prior HIGH fail-closed holes, but they require explicit acceptance or a small follow-up remediation before Stage B should be approved.

**This verdict does not authorize Stage B.**

---

## 2. Exact audit scope

| In scope | Out of scope |
| --- | --- |
| `research_lab/adaptive/*` | Engines S1–S4 / controller |
| `tests/test_adaptive_shared_interfaces.py` | Historical / quote-tape replay |
| Stage 3.3.2.2 spec (updated) | Scoring / optimization / event capture |
| Stage 3.3.2.4 remediation report | Sealed holdout |
| Stage 3.3.2 / 3.3.2.1 architecture docs | `F:\crypto-ai-trade-v4` |
| Existing synthetic unit tests (executed) | Network / paid APIs |

**Note:** `research-lab/reports/STAGE3.3.2.3_INDEPENDENT_AUDIT_REPORT.md` was **not present** on disk. Prior findings were verified against the Stage 3.3.2.4 remediation report’s finding IDs and the earlier audit content they reference.

No source, tests, prior reports, or configuration were modified by this audit (except creation of this new report file as required).

---

## 3. Files inspected

- `research_lab/adaptive/immutability.py`
- `research_lab/adaptive/construct.py`
- `research_lab/adaptive/versioning.py`
- `research_lab/adaptive/validation.py`
- `research_lab/adaptive/opportunity.py`
- `research_lab/adaptive/evidence.py`
- `research_lab/adaptive/candidate.py`
- `research_lab/adaptive/recommendation.py`
- `research_lab/adaptive/decision_record.py`
- `research_lab/adaptive/serialize.py`
- `research_lab/adaptive/__init__.py`
- `tests/test_adaptive_shared_interfaces.py`
- `docs/STAGE3.3.2.2_SHARED_INTERFACES_SPECIFICATION.md`
- `reports/STAGE3.3.2.4_CONTRACT_INTEGRITY_REMEDIATION_REPORT.md`
- `docs/STAGE3.3.2.1_ADAPTIVE_ARCHITECTURE_REFINEMENT.md` (alignment)
- `docs/STAGE3.3.2_ADAPTIVE_SIGNAL_DISCOVERY_SPECIFICATION.md` (alignment)

---

## 4. Tests inspected and executed

### Inspected

`tests/test_adaptive_shared_interfaces.py` — includes dedicated classes:

- `HighFindingRegressionTests` (H1–H3)
- `MediumFindingRegressionTests` (M1–M7)
- `LowFindingTests` (L1, L3)
- Versioning / compatibility / decision-record suites

Tests generally assert specific error substrings and loader failures (not mere smoke execution). Gaps noted in §7.

### Executed (read-only)

```
PYTHONPATH=F:\crypto-ai-scalper\research-lab python -m unittest tests.test_adaptive_shared_interfaces -q
Ran 50 tests in 0.012s
OK
```

```
PYTHONPATH=F:\crypto-ai-scalper\research-lab python -m unittest discover -s tests -q
Ran 184 tests in 1.517s
OK
```

No historical/trading workloads beyond the existing research-lab suite were invoked.

---

## 5. Previous finding-by-finding verification

### HIGH — verified fixed

| Prior ID | Status | Evidence |
| --- | --- | --- |
| **H1** Nested contract versions | **FIXED** | `validate_opportunity_dict` / `validate_confidence_evidence_dict` call `_check_version` → `classify_version` / `is_compatible`. Unsupported major/newer minor and malformed versions fail. Loaders call validators before construct (`opportunity_from_dict`, `evidence_from_dict`, `candidate_from_dict`). Tests: `test_h1_*`. CostAssumptions are **not** independently versioned (by design; rates live under opportunity version) — **INFO**, not a regression. |
| **H2** Cost completeness | **FIXED** | `_require_bool` rejects non-bools; `costs_complete is False` blocks `OPPORTUNITY`; construction `CostAssumptions.__post_init__` requires bool; `opportunity_from_dict` assigns validated bool without `bool()`. Matrix test covers `false`/`true`/`"false"`/`"true"`/`0`/`1`/`null`/missing. |
| **H3** Unknown top-level fields | **FIXED** | Explicit `*_FIELDS` frozensets + `_unknown_keys` on candidate/opportunity/evidence/group/recommendation/no_trade/decision_record/position_state/cost_assumptions. Extensions remain forward-compat path. Prohibited confidence keys scanned recursively via `_scan_prohibited_keys`. Deprecated-field bag still loads (`test_deprecated_fields_preserved`) while rejecting prohibited keys inside it. |

### MEDIUM — mostly fixed; one partial

| Prior ID | Status | Evidence |
| --- | --- | --- |
| **M1** Cross-field invariants | **FIXED** (structural) + **DEFERRED** (policy) | Enforced: INELIGIBLE+eligible=True; VALID/CONFIRMED+eligible=False; only-conflicting evidence; duplicate evidence IDs; unknown group members; within-group duplicate members; OPPORTUNITY null fractions; fee+spread+slip==total and net==gross-total. Deferred controller items documented in spec §“Deferred to future controller” and 3.3.2.4 report. Cross-group repeated membership still allowed — see **A3325-03**. |
| **M2** Interface version | **FIXED** | `validate_decision_record_dict` checks `interface_version` against `SCHEMA_VERSION`. Test `test_m2_interface_version`. |
| **M3** Timestamps | **FIXED** | Shared `_validate_utc_timestamp` requires string ISO + UTC parse for candidate, recommendation, no_trade, decision record. Non-string fails validation **and** load. Tests `test_m3_*`. |
| **M4** Numeric opportunity | **FIXED** | `_finite_number` rejects NaN/Inf/bool/non-numeric; cost components non-negative; signed gross/net allowed (probe confirmed). Test `test_m4_*`. |
| **M5** Evidence by family | **PARTIALLY FIXED** | Emitted on `to_dict`; reconstructed when absent; older payloads without field validate/load; ID-set inconsistency rejected. **However**, consistency compares **evidence_id lists only**, not full payloads — content tampering of explanations/measurements can pass. See **A3325-01**. |
| **M6** Direct construction | **FIXED** | Candidate/opportunity/evidence/recommendation/no_trade/decision_record `__post_init__` validate serialized form. `construct.create_*` delegates to loaders. Invalid direct INELIGIBLE+eligible=True raises. |
| **M7** Deep immutability | **MOSTLY FIXED** | `freeze_mapping` recursively freezes dicts and converts lists/tuples; input list mutation after construct does not affect contract; MappingProxyType blocks item assignment. Residual: arbitrary non-container objects (custom class instances) remain shared mutable refs; `set` values freeze as-is and break canonical JSON. See **A3325-02**. |

### LOW — verified fixed / assessed

| Prior ID | Status | Evidence |
| --- | --- | --- |
| **L1** Empty required strings | **FIXED** | `_non_empty_str` on IDs/reasons/rationale/group_id; tests present. |
| **L2** Malformed vs unsupported | **FIXED** | `classify_version`; error strings distinguish `malformed` vs `unsupported`. |
| **L3** Prohibited keys in extensions | **FIXED** | Recursive scan; test `test_l3_*`; also blocks `deprecated_fields`. |
| **L4** Docs match behavior | **FIXED** | Spec updated with enforced unknown-field / version / invariant / immutability sections matching code. |
| **L5** Leading-zero SemVer | **FIXED** | `parse_semver` rejects `01.0.0`; test present. |

---

## 6. New / residual findings

### A3325-01 — `evidence_by_family` consistency is ID-shallow

| Field | Value |
| --- | --- |
| **Severity** | MEDIUM |
| **Location** | `validation.py` → `validate_decision_record_dict` (approx. L951–L988) |
| **Evidence** | Consistency uses `expected_ids` / `actual_ids` from `evidence_id` only. Probe: tampering `evidence_by_family["MOMENTUM"][0]["explanation"]` still yields empty validation errors. Spec claims supplied index “must be consistent with candidates” without limiting consistency to IDs. |
| **Impact** | Decision-record reconstruction can diverge in evidence payload content while appearing valid. Not an execution authority issue. |
| **Remediation** | Compare canonical evidence payloads (or hash/`dumps_canonical` of each evidence dict) per family order; add regression test for content tamper. |
| **Blocks Stage B?** | Not strictly if accepted explicitly; recommend fix or written acceptance before Stage B. |

### A3325-02 — Non-container extension values not deep-frozen / not JSON-constrained

| Field | Value |
| --- | --- |
| **Severity** | LOW |
| **Location** | `immutability.py` → `_freeze_value`; contract `__post_init__` freezers |
| **Evidence** | Custom object in `extensions` remains shared reference (`box.v` mutated externally). `set` in extensions constructs but `dumps_canonical` raises `TypeError`. Dict/list nesting **is** frozen (verified). |
| **Impact** | Wire-format JSON use is safe; in-memory non-JSON extension abuse can mutate or break serialization. |
| **Remediation** | Restrict extension leaves to JSON-compatible types, or deep-copy/reject non-JSON objects; add tests. |
| **Blocks Stage B?** | No, if engines only emit JSON-compatible extensions. |

### A3325-03 — Cross-group duplicate evidence membership allowed

| Field | Value |
| --- | --- |
| **Severity** | INFO |
| **Location** | `validation.py` → `_candidate_structural_invariants` |
| **Evidence** | Same `evidence_id` may appear in two `CorrelatedEvidenceGroup`s; validates clean. Within-group duplicates rejected. |
| **Impact** | Ambiguous correlation accounting possible; aligns with deferred correlation geometry. |
| **Remediation** | Defer to controller/correlation stage, or reject cross-group duplicates later. |
| **Blocks Stage B?** | No (documented deferral class). |

### A3325-04 — Missing cost-assumption rates defaulted in loader

| Field | Value |
| --- | --- |
| **Severity** | LOW |
| **Location** | `serialize.py` → `opportunity_from_dict` |
| **Evidence** | Opportunity with `costs_complete=True` but no fee/spread/slip keys validates; loader inserts `0.001` / `0.0002` / `0.0005`. |
| **Impact** | Silent assumption fill; fractions on the opportunity object may still be explicit. Not the H2 boolean coercion bug. |
| **Remediation** | Require explicit rates when `costs_complete=True`, or document defaulting as intentional. |
| **Blocks Stage B?** | No. |

### A3325-05 — CostAssumptions lack independent contract version

| Field | Value |
| --- | --- |
| **Severity** | INFO |
| **Evidence** | No `contract_version` on `CostAssumptions`; versioned via parent opportunity. Acceptable design. |
| **Blocks Stage B?** | No. |

### A3325-06 — Deferred controller-policy items

| Field | Value |
| --- | --- |
| **Severity** | INFO |
| **Evidence** | Spec + 3.3.2.4 report correctly list deferred items (multi-engine priority, correlation geometry, mixed-evidence demotion, broker snapshot mapping, min net edge). |
| **Blocks Stage B?** | No for Stage A closure; remain Stage C+ concerns. |

### A3325-07 — Test coverage gaps

| Field | Value |
| --- | --- |
| **Severity** | LOW |
| **Evidence** | No automated tests for: ebf content tamper; post-construct input isolation; non-JSON extension values; cross-group membership; missing rate defaulting; naive recommendation timestamp (non-string covered). |
| **Impact** | Residual risks above were found by auditor probes, not the suite. |
| **Blocks Stage B?** | No by itself. |

---

## 7. Detailed source evidence (selected)

### H2 boolean enforcement

`validation.py` `_require_bool` rejects non-bools; opportunity path uses it for `costs_complete`. `serialize.opportunity_from_dict` passes through validated bool (no `bool(value)`).

### H3 unknown fields

`_unknown_keys(raw, CANDIDATE_FIELDS, ...)` etc. Comment vs behavior now aligned with rejection.

### M5 shallow compare

```text
expected_ids = { fam: [item.get("evidence_id") for item in items] ... }
actual_ids[...] = item.get("evidence_id")
if expected_ids != actual_ids: error
```

### M7 freeze behavior

Lists → tuples; dicts → `MappingProxyType`; other objects returned unchanged (`_freeze_value`).

### Double validation

`__post_init__` + loaders both validate. Consistent fail-closed; no observed conflicting accept/reject for the same payload in probes. Slight redundancy only (INFO).

### Validation mutation

Validators do not mutate caller dicts as part of checks (read-only traversal).

---

## 8. Compatibility assessment

- Schema/interface/candidate/recommendation versions remain **`1.0.0`**.  
- Older records omitting optional fields (including `evidence_by_family`) still validate and load; emission adds reconstructed `evidence_by_family`.  
- Newer minor rejected by default; `allow_newer_minor` exists on helpers but validators do not enable it (fail closed) — correct.  
- Compatibility claims in 3.3.2.4 are largely accurate; do **not** over-claim deep payload equivalence for supplied `evidence_by_family` beyond ID lists (A3325-01).  
- Stricter unknown-key rejection is intentional policy enforcement, not a silent major bump.

---

## 9. Serialization and immutability assessment

| Property | Result |
| --- | --- |
| Deterministic canonical JSON | Confirmed (`sort_keys`, `allow_nan=False`); round-trip tests pass |
| UTC timestamps | Confirmed; naive/non-string rejected consistently |
| Extensions JSON round-trip | Confirmed for dict/list/primitive nests |
| Mapping/list freeze | Confirmed |
| Arbitrary object freeze | Incomplete (A3325-02) |
| `set` in extensions | Constructs; canonical dump fails |
| NaN in opportunity validate | Rejected before object acceptance |

---

## 10. Architecture alignment

Aligned with Stage 3.3.2.1 Stage A intent:

- Shared contracts only; no engines/controller.  
- Pillars separated; no PoP confidence scores.  
- Evidence families/kinds + correlated groups present.  
- Cost completeness vs opportunity quality separated.  
- `NO_TRADE` first-class.  
- Decision records reconstructible with family index (with A3325-01 caveat).  
- Risk/execution authority not encoded in contracts.

---

## 11. Stage B readiness

| Question | Answer |
| --- | --- |
| Prior HIGH defects remain? | **No** |
| Material new HIGH defects? | **No** |
| Residual issues needing acceptance/fix? | **Yes** — primarily A3325-01 (and optionally A3325-02/04) |
| Stage B authorized by this audit? | **No** |
| Recommended gate | Explicit acceptance of CONDITIONAL residuals **or** small integrity patch + re-audit / targeted verification before Stage B approval |

---

## 12. Explicit boundary confirmation

This audit:

- Did **not** modify source, tests, docs, config, or prior reports (except creating this file).  
- Did **not** fix findings or implement Stage B / S4 / controller.  
- Did **not** run historical candle/quote-tape replay, event capture, scoring, optimization, or benchmarking beyond the existing synthetic research-lab suite.  
- Did **not** inspect or use sealed holdout data.  
- Did **not** access `F:\crypto-ai-trade-v4` or network/paid services.  
- Did **not** alter frozen strategies, PaperBroker economics, risk logic, or Stage 2 replay.

---

## Summary table

| Category | Count |
| --- | --- |
| Prior HIGH fixed | 3 / 3 |
| Prior MEDIUM fixed | 6 fully + 1 partial (M5) + M7 mostly |
| Prior LOW fixed | 5 / 5 |
| New MEDIUM | 1 (A3325-01) |
| New LOW | 3 (A3325-02, A3325-04, A3325-07) |
| INFO | 3 (A3325-03, A3325-05, A3325-06) |

**Verdict: CONDITIONAL PASS — stop. Do not proceed to Stage B without separate approval.**
