# Stage 3.3.2.1 — Architecture Review Response

**Stage type:** Planning / documentation only  
**Date:** 2026-11-06  
**Companion refinement:** `research-lab/docs/STAGE3.3.2.1_ADAPTIVE_ARCHITECTURE_REFINEMENT.md`  
**Original Stage 3.3.2 docs:** preserved unmodified  

**No code, tests, historical data, replay, scoring, optimization, benchmarking, event capture, or strategy execution were performed.**

---

## 1. Purpose

Respond to the Stage 3.3.2 architecture review by refining planning documents only. Address five findings without inventing unsupported capabilities or authorizing implementation.

---

## 2. Review findings and responses

### Finding 1 — Correct implementation sequence

| Item | Response |
| --- | --- |
| **Issue** | Original roadmap built S1–S4 before any controller, delaying an end-to-end adaptive loop |
| **Addressed in** | Refinement §4 Stages A–H |
| **Decision** | Revised order: **A** shared interfaces → **B** S4 → **C** minimal controller (single-engine loop) → **D** S1 → **E** S3 → **F** S2 → **G** full coordination → **H** synthetic validation |
| **Rationale** | Prove intelligence→eligibility→candidate→evidence→costs→recommendation/NO TRADE→risk handoff early; add combinatorial engines after |
| **Status** | Resolved at planning level (proposal only, not authorized) |

### Finding 2 — S4 independent eligibility

| Item | Response |
| --- | --- |
| **Issue** | Dual role (candidate + confirmation) lacked primacy rules |
| **Addressed in** | Refinement §5 |
| **Decision** | S4 **may** be independent primary in confirmed TRENDING under **stricter** evidence (momentum + structure + persistence + economics); otherwise confirmation-only / Secondary; B0/H1 untouched |
| **Numeric thresholds** | Not invented as fixed edge values; marked configurable/provisional |
| **Status** | Resolved at policy level; exact bar counts remain provisional parameters |

### Finding 3 — Confidence and correlated evidence

| Item | Response |
| --- | --- |
| **Issue** | Correlated indicators (EMA, strength, momentum) could be miscounted as independent evidence |
| **Addressed in** | Refinement §6 |
| **Decision** | Evidence kinds (independent / correlated / confirmation / missing / conflicting) + logical families; directional structure + momentum share one family credit by default; no confidence %; no PoP |
| **Pillars** | Eligibility, signal confidence, opportunity quality, risk approval remain separate |
| **Status** | Resolved at framework level; calibration deferred to future OOS approval |

### Finding 4 — Open-position switching policy

| Item | Response |
| --- | --- |
| **Issue** | Stage 3.3.2 left open-position switching unresolved |
| **Addressed in** | Refinement §7 |
| **Decision** | Conservative default: preference changes apply to **future** opportunities only; never auto-close, reverse, or duplicate; exits stay with existing risk/broker; invalid data → NO TRADE for new entries only |
| **Status** | Resolved for default policy; exact broker snapshot field mapping remains unresolved until implementation maps read-only APIs |

### Finding 5 — Engine-specific opportunity estimates

| Item | Response |
| --- | --- |
| **Issue** | ATR proxy must not be treated as achievable profit; engines need own research frameworks |
| **Addressed in** | Refinement §8 |
| **Decision** | Keep 3.3.1 ATR proxy as baseline filter; define investigation frameworks for S1–S4 gross proxies; same fee/spread/slip; incomplete costs block selection; min net edge reuses 3.3.1 provisional config unless separately approved |
| **Status** | Resolved as research framework; proxies themselves not implemented or calibrated |

---

## 3. Updated implementation sequence

| Stage | Deliverable |
| --- | --- |
| A | Shared interfaces and decision records |
| B | S4 Momentum Confirmation |
| C | Minimal adaptive controller (end-to-end with S4 only) |
| D | S1 Trend Pullback |
| E | S3 Volatility Expansion |
| F | S2 Liquidity Sweep |
| G | Full strategy coordination |
| H | Integrated synthetic validation |

Then, **only with separate approvals:** historical candle research (no holdout); paper-trading integration.

Dependencies, acceptance criteria, exclusions: see refinement §4.

**This sequence is a proposal, not authorization.**

---

## 4. Decisions made

1. Establish adaptive loop at Stage C (after S4), not after all four engines.  
2. S4 independent primary allowed under stricter TRENDING evidence; confirmation role retained.  
3. Correlated directional metrics count as one evidence family.  
4. Open positions are never controller-flipped; selection is for future opportunities.  
5. Engine gross proxies are future research frameworks; shared costs remain frozen assumptions.  
6. Decision records must store evidence **by family** and correlated groups.  
7. JSONL proposed for storage; format not frozen.  
8. Original Stage 3.3.2 documents left intact; refinement is additive.

---

## 5. Remaining unresolved questions

Explicitly **not** invented away:

1. Exact provisional integers for S4 independent persistence bars.  
2. Exact compression lookback bars for S3 state memory.  
3. Conditional HIGH_VOLATILITY extra checks for S1/S2.  
4. Precise geometric definition of “correlated overlapping invalidation” across engines.  
5. Read-only PaperBroker/risk snapshot fields for position-state awareness.  
6. Whether Secondary-alone S4 is ever allowed outside the stricter independent-primary path when no other Primary exists (policy allows independent-primary; Secondary-alone still discouraged).  
7. If/when quote-tape freshness participates in controller eligibility.  
8. Confidence family weighting / calibration method (requires future OOS approval).  
9. Final decision-record on-disk format freeze.  
10. Short-side policy (out of scope under current spot long-only stance).

---

## 6. Dependencies on Stage 3.3.1

| Capability | Use in refined architecture |
| --- | --- |
| Primary + confirmed regime / hysteresis | Eligibility, S4 primacy gate, switching inputs |
| Secondary vol / momentum / strength | Families + matrix; correlated with structure |
| Data quality statuses | Hard NO TRADE |
| Feature snapshot | Future engine structure / proxies |
| OpportunityAssessment + cost defaults | Baseline economics; min net edge config |
| Sequential no-look-ahead engine | Intelligence feed for controller |

Inherited limits: ATR proxy only; no PoP uncertainty score; volume optional; characterization does not execute; `ResearchStrategy` ≠ candidate contract.

---

## 7. Risks and limitations

| Risk | Planning mitigation |
| --- | --- |
| Premature historical tuning | A–H synthetic-first; historical behind approval; holdout banned |
| Confidence inflation | Correlated-family rule |
| Controller-driven churn of open positions | Conservative open-position policy |
| Treating proxies as expected return | Explicit proxy language + shared costs only |
| Scope creep into production | research-lab only; risk/broker frozen |
| Building engines without a loop | Sequence puts Stage C early |

---

## 8. Authorization boundaries

**Authorized and done:**

- Read-only review of Stage 3.3.2 + Stage 3.3.1 docs/interfaces  
- Creation of:
  - `research-lab/docs/STAGE3.3.2.1_ADAPTIVE_ARCHITECTURE_REFINEMENT.md`
  - `research-lab/reports/STAGE3.3.2.1_ARCHITECTURE_REVIEW_RESPONSE.md`

**Not authorized and not done:**

- Any code or refactor  
- Tests  
- Historical/event-capture/scoring/optimization/benchmarking  
- Sealed-holdout access  
- B0/H1, PaperBroker, Stage 2 changes  
- Stage A–H implementation  
- Access to `F:\crypto-ai-trade-v4`  

---

## 9. Consistency check

Documents reaffirm: €0, paper-only, no live trading, holdout sealed, dual-track evidence, research-lab-only future code, no profitability claims.

---

## 10. STOP

Stage 3.3.2.1 complete.  
**Waiting for explicit approval** before any Stage A (or later) implementation.
