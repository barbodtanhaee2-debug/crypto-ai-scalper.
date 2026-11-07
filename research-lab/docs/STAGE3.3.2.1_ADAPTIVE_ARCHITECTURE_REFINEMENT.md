# Stage 3.3.2.1 — Adaptive Architecture Refinement

**Stage type:** Planning and documentation only  
**Status:** Refinement of Stage 3.3.2 — **not** implementation authorization  
**Supersedes for planning purposes:** implementation sequence, S4 independence policy, confidence/correlation handling, open-position switching, and engine-specific opportunity framework from Stage 3.3.2  
**Does not overwrite:** `STAGE3.3.2_ADAPTIVE_SIGNAL_DISCOVERY_SPECIFICATION.md` or `STAGE3.3.2_ARCHITECTURE_PLANNING_REPORT.md`  

This document does **not** authorize code, tests, historical research, scoring, optimization, live capture, or production promotion. It does **not** claim profitability or predictive accuracy.

---

## 1. Objective (unchanged)

Build **one** adaptive paper-scalping system with four specialized engines:

| ID | Name |
| --- | --- |
| S1 | Trend Pullback Continuation |
| S2 | Liquidity Sweep Reversal |
| S3 | Volatility Expansion Breakout |
| S4 | Momentum Confirmation |

A deterministic Adaptive Strategy Controller coordinates engines. The system must understand conditions, evaluate candidates, assess evidence and cost-adjusted opportunity, select a primary (optional confirmation), reject conflicts, or choose **NO TRADE**.

Existing central risk engine and PaperBroker remain the **sole** risk and execution authorities.

Frozen B0/H1 remain untouched. S4 is a new research candidate and does not replace them.

---

## 2. Authorization boundaries

| Constraint | Rule |
| --- | --- |
| Cost | Strict €0 unless separately approved |
| Mode | Paper-only; no live orders; no authenticated trading endpoints |
| Instruments | Spot BTC/USDT; no leverage / margin / futures |
| Frozen | No edits to B0/H1, Stage 2 replay, PaperBroker economics, quote-tape sessions, sealed artifacts |
| Holdout | Never access/score/tune against sealed holdout (`2026-10-31T04:20:59.999Z`) |
| Evidence tracks | Historical candle ≠ quote-tape |
| Code location | Future work under `F:\crypto-ai-scalper\research-lab\` only |
| Forbidden | Never access `F:\crypto-ai-trade-v4` |
| This stage | Documentation only |

---

## 3. Layered architecture (retained)

```
Layer 1  Market Data & Quality
Layer 2  Market Intelligence (Stage 3.3.1 characterization)
Layer 3  Opportunity Detection (baseline ATR proxy + future engine proxies)
Layer 4  Adaptive Strategy Controller (deterministic)
         → StrategyRecommendation | NO TRADE
Existing Risk Engine + PaperBroker (UNCHANGED)
```

Principles:

1. One system, many engines.  
2. Regime ≠ eligibility ≠ signal confidence ≠ opportunity quality ≠ risk approval.  
3. Secondary traits remain overlapping (3.3.1).  
4. NO TRADE is first-class.  
5. Rule-based controller initially (no ML).  
6. Controller recommends; risk decides.  
7. Expandable via shared candidate contract.

---

## 4. Finding 1 — Corrected implementation sequence

**Problem addressed:** Original Stage 3.3.2 proposed building all four engines before the controller, delaying an end-to-end adaptive loop.

**Revised proposal (not authorization):** A → H below establishes a working decision loop after S4 + minimal controller.

### Stage A — Shared interfaces

| Item | Content |
| --- | --- |
| **Objective** | Freeze research contracts so engines and controller share one vocabulary |
| **Dependencies** | Stage 3.3.1 models/enums conceptually; no code required in this planning stage |
| **Scope** | Design only (until separately authorized): StrategyCandidate; signal statuses; eligibility; confidence evidence; cost-adjusted opportunity; StrategyRecommendation; NO TRADE; decision-record schema |
| **Deliverables** | Interface/spec artifacts under research-lab (when authorized) |
| **Acceptance** | Contracts distinguish Ineligible / No signal / Pending / Confirmed / Invalidated / Expired / Rejected opportunity; pillars kept separate; decision record reconstructible |
| **Exclusions** | No engines, no controller logic, no historical work |
| **Approval** | Separate implementation approval required |

### Stage B — S4 Momentum Confirmation

| Item | Content |
| --- | --- |
| **Objective** | First independent candidate engine (also usable later as confirmation) |
| **Dependencies** | Stage A contracts; Stage 3.3.1 intelligence |
| **Scope** | S4 long-only research engine; stricter primary vs confirmation modes (see §5) |
| **Deliverables** | S4 module + synthetic unit tests (when authorized) |
| **Acceptance** | Deterministic; no look-ahead; B0/H1 untouched; emits valid StrategyCandidate |
| **Exclusions** | No controller yet; no S1–S3; no historical scoring |
| **Approval** | Separate implementation approval |

### Stage C — Minimal adaptive controller

| Item | Content |
| --- | --- |
| **Objective** | Genuine end-to-end adaptive loop with **one** engine (S4) |
| **Dependencies** | Stages A–B; Stage 3.3.1 `MarketCharacterization` |
| **Scope** | Consume intelligence; S4 eligibility; collect candidates; evidence + economics; recommend or NO TRADE; write decision records |
| **Deliverables** | Minimal controller + synthetic loop tests |
| **Acceptance** | Can produce Recommendation and NO TRADE with full reasoning from validated fixtures; never places orders or sizes positions |
| **Exclusions** | Multi-engine coordination; S1–S3; historical research |
| **Approval** | Separate implementation approval |

**Why C before more engines:** Proves the adaptive pipeline (intelligence → eligibility → candidate → evidence → costs → decision record → risk handoff) before combinatorial complexity.

### Stage D — S1 Trend Pullback

| Item | Content |
| --- | --- |
| **Objective** | Add TRENDING primary engine; expand eligibility/selection |
| **Dependencies** | A–C |
| **Scope** | S1 + controller eligibility for S1 Primary / S4 Secondary or primary-under-stricter-rules |
| **Acceptance** | Synthetic trend/pullback fixtures; conflict with S4 handled; NO TRADE paths intact |
| **Exclusions** | S2/S3; full coordination suite |
| **Approval** | Separate |

### Stage E — S3 Volatility Expansion

| Item | Content |
| --- | --- |
| **Objective** | Compression→expansion state machine + breakout candidates |
| **Dependencies** | A–D; 3.3.1 secondary volatility |
| **Scope** | S3 with explicit compression memory (parameters provisional) |
| **Acceptance** | Confirmed expansion required for Primary; false expansion → no confirmed signal |
| **Exclusions** | S2; historical tuning |
| **Approval** | Separate |

### Stage F — S2 Liquidity Sweep

| Item | Content |
| --- | --- |
| **Objective** | Range sweep/rejection engine (highest structural false-positive risk) |
| **Dependencies** | A–E preferred so controller patterns exist |
| **Scope** | Long-side spot S2; rejection vs breakout distinction |
| **Acceptance** | Synthetic sweep/reject and false-break fixtures |
| **Exclusions** | Short sweeps; holdout |
| **Approval** | Separate |

### Stage G — Full strategy coordination

| Item | Content |
| --- | --- |
| **Objective** | Multi-candidate primary/confirm, conflicts, correlation, switching, position-state |
| **Dependencies** | A–F (or at least A–D with stubs — prefer full engines) |
| **Scope** | §§6–8 policies implemented as deterministic rules |
| **Acceptance** | Synthetic multi-engine scenarios; no duplicate exposure; open-position policy honored |
| **Exclusions** | Auto-tuning; ML |
| **Approval** | Separate |

### Stage H — Integrated synthetic validation

| Item | Content |
| --- | --- |
| **Objective** | End-to-end synthetic validation of the architecture |
| **Dependencies** | A–G |
| **Scope** | Deterministic fixtures only; no historical datasets |
| **Acceptance** | Full research-lab suite green; scenario matrix documented |
| **Exclusions** | Historical replay/scoring |
| **Approval** | Separate |

### Future separate approvals (unchanged)

- Historical candle research (still dual-track; **no sealed holdout**)  
- Paper-trading integration via existing risk/broker  

---

## 5. Finding 2 — S4 independent eligibility (refined)

**Problem addressed:** S4 was described as both standalone and confirmation without clear primacy rules.

### Dual role

| Role | When |
| --- | --- |
| **Confirmation** | Another engine is selected Primary; S4 may confirm same direction under lighter evidence |
| **Independent primary** | Confirmed `TRENDING` **and** stricter evidence set below is fully satisfied |

S4 remains **separate from frozen B0/H1**.

### Minimum market regime (independent primary)

Required:

- `confirmed_regime == TRENDING`  
- Data quality OK (not `INVALID` / `INSUFFICIENT_DATA`)  
- Not `TRANSITIONING` / `UNKNOWN`  

Optional affinity (provisional, configurable):

- Momentum not weakly opposing the candidate direction  
- Volatility not forced `LOW_VOLATILITY` if matrix says No under low vol (default eligibility matrix: Low vol → all No)

### Momentum confirmation (conceptual)

Evidence that price momentum aligns with proposed long direction (e.g. 3.3.1 `STRONG_UP` or engine-local momentum rule).  
**Thresholds:** configurable/provisional; not invented as fixed “edge” numbers here.

### Price-structure confirmation (conceptual)

Structure supports continuation (e.g. EMA alignment with trend, price holding above a provisional structure reference).  
Must be listed as **directional-structure family** evidence (see §6), not stacked as independent with momentum.

### Signal persistence (independent primary)

Require multi-bar persistence of the momentum/structure condition before `Confirmed signal`.  
Exact bar count: **provisional config** (suggested research starting point only: ≥ persistence used by regime confirmation philosophy; not empirically optimized).

### Cost-adjusted opportunity

- Gross proxy: S4-specific framework (§8) or fallback ATR baseline  
- Same fee/spread/slip assumptions as 3.3.1  
- Incomplete costs → cannot select  
- Net edge ≥ provisional `minimum_net_edge_fraction` from 3.3.1 config unless separately approved to change research config only  

### Invalidation (conceptual)

Examples for future design (not numeric locks):

- Confirmed regime leaves TRENDING  
- Momentum flips opposing  
- Structure reference broken  
- Opportunity expiry elapsed  

### When S4 is confirmation-only

- Another engine is eligible Primary and has a confirmed candidate  
- S4 evidence meets confirmation bar but **not** the stricter independent-primary bar  
- Or controller policy prefers Primary from matrix (e.g. S1 Primary in TRENDING) and S4 is Secondary  

### When S4 can become primary

All of:

1. Confirmed TRENDING  
2. No higher-priority Primary candidate selected (or none eligible)  
3. Stricter independent evidence set satisfied  
4. Opportunity economics pass  
5. Position-state allows a **new** opportunity recommendation (see §7)  
6. No unresolved conflicts  

### Force NO TRADE (S4-relevant)

- UNKNOWN / TRANSITIONING regime  
- Bad/insufficient data quality  
- Conflicting S4 vs other candidate directions  
- Failed economics / incomplete costs  
- Insufficient independent-primary evidence when S4 is the only proposer  
- Low-volatility matrix exclusion  
- Open-position policy forbids new recommendation (§7)  

---

## 6. Finding 3 — Confidence and correlated evidence

**Problem addressed:** Multiple indicators of the same directional move must not inflate confidence.

### Pillars remain separate

1. Strategy eligibility  
2. Signal confidence (evidence)  
3. Opportunity quality  
4. Risk approval  

No arbitrary confidence %. No probability-of-profit.

### Evidence kinds

| Kind | Meaning |
| --- | --- |
| **Independent evidence** | Distinct information families that can fail separately |
| **Correlated evidence** | Multiple metrics reflecting the same underlying phenomenon |
| **Confirmation evidence** | Second engine or second-family check agreeing with primary |
| **Missing evidence** | Required family unavailable → cannot claim that family |
| **Conflicting evidence** | Families or engines disagree → NO TRADE or demote |

### Logical evidence families

| Family | Examples (non-exhaustive) |
| --- | --- |
| Market regime | Confirmed primary regime, persistence, conflicting raw label |
| Directional structure | EMA alignment, efficiency, rolling bounds / structure references |
| Momentum | Close vs EMA momentum labels, engine momentum score |
| Volatility | NATR / RVOL secondary levels; compression/expansion state |
| Price action | Pullback/sweep/breakout pattern geometry |
| Data quality | DQ status, gaps, staleness |
| Opportunity economics | Gross proxy, costs, net edge, completeness |

### Correlated-group rule (critical)

**Directional structure** and **Momentum** (and often trend-regime alignment) frequently describe the **same** directional persistence.

Controller policy:

1. Group correlated indicators into a **single family credit** (at most one “satisfied” mark for the directional cluster unless a documented independent family also passes).  
2. EMA alignment + directional strength + momentum **do not** count as three independent supports.  
3. Confirmation evidence from **another engine** may count only if its pattern family differs (e.g. S1 pullback geometry confirming S4 momentum) — still not a license to double size.  
4. Missing family → record `missing`; do not impute.  
5. Conflict within or across families → record `conflicting`; default **NO TRADE**.

### Future calibration

Weights/calibration of families may only be studied under **separate** out-of-sample research authorization, never sealed holdout, never auto-tuned from small recent P&L. Rule changes require documented validation + explicit approval.

---

## 7. Finding 4 — Open-position switching policy (conservative default)

**Problem addressed:** Stage 3.3.2 left open-position behavior unresolved.

### Default policy

| Rule | Behavior |
| --- | --- |
| Selection scope | Strategy preference applies to **future** opportunities only |
| Existing position | Remains under **original** management / risk / broker rules |
| Preferred strategy change | Does **not** auto-close |
| Preferred strategy change | Does **not** reverse |
| Preferred strategy change | Does **not** create duplicate exposure |
| New preference alone | Does **not** authorize a new position |
| Authority | Central risk + PaperBroker unchanged |
| Exits | Existing authorized risk/broker exit rules only |

Controller must not modify PaperBroker or position-management behavior.

### Scenario behavior

| Situation | Controller behavior |
| --- | --- |
| Regime changes while position open | Update intelligence + decision records; may set preferred engine to NO TRADE for **new** entries; **do not** force flatten via controller |
| Preferred strategy changes while open | Record new preference for future entries only; leave open position alone |
| Conflicting signal from another engine | Do not open opposing exposure; do not reverse via controller; log conflict; NO TRADE for new entries |
| Existing signal expires | Expiry affects **candidate** state only; open position still follows risk/broker exits |
| Market data invalid or stale | NO TRADE for new recommendations; do not invent exits; surface DQ in decision record; risk/broker retain existing fail-closed behaviors if any |

### Position-state awareness inputs (future)

Controller should observe (read-only) whether a position is open, its direction, and strategy tag if available from broker/risk snapshots — **without** writing broker state.

Exact snapshot fields: **unresolved** until implementation maps to existing PaperBroker/risk APIs without changing them.

---

## 8. Finding 5 — Engine-specific opportunity estimates

**Problem addressed:** ATR baseline must not be treated as achievable profit; engines need research frameworks for gross proxies.

### Baseline (Stage 3.3.1)

`gross ≈ normalized_atr × atr_move_multiplier`  

- Research filter proxy only  
- Not a forecast  
- Not historical MFE as guarantee  
- Costs: fee / spread / slip defaults unchanged  
- Incomplete costs → no positive selection  
- `minimum_net_edge_fraction` reuses 3.3.1 provisional config unless separately approved  

### Future research frameworks (not implemented now)

#### S1 — Trend Pullback

Investigate proxies from: pullback depth; continuation structure; distance to next structural boundary.  
**Limitation:** Boundary choice is provisional; pullback depth ≠ guaranteed continuation.

#### S2 — Liquidity Sweep

Investigate: sweep depth; rejection distance; room back into range.  
**Limitation:** Failed sweeps vs true breakouts are structurally ambiguous; proxy easily overstates quality.

#### S3 — Volatility Expansion

Investigate: compression magnitude; breakout distance; expansion magnitude; post-confirmation room.  
**Limitation:** Expansion can mean noise; compression definition needs state memory (unresolved bar count).

#### S4 — Momentum Confirmation

Investigate: momentum strength; directional persistence; structural continuation; post-confirmation room.  
**Limitation:** Momentum metrics correlate heavily with directional structure (§6); proxy must not double-count as both confidence and gross edge without documentation.

### Shared cost rule

All engine proxies **must** apply the same existing fee, spread, and slippage assumptions. No competing cost model.

---

## 9. Full adaptive decision process

1. Receive validated market data.  
2. Receive Stage 3.3.1 characterization.  
3. Check data quality → fail closed / NO TRADE if invalid or insufficient.  
4. Check confirmed primary regime.  
5. Evaluate overlapping secondary characteristics.  
6. Determine eligible engines (matrix ∩ hard rules ∩ S4 independence policy).  
7. Collect strategy candidates.  
8. Validate candidate status and expiry.  
9. Evaluate evidence and **correlated evidence groups**.  
10. Evaluate cost-adjusted opportunity (baseline or engine proxy + shared costs).  
11. Identify compatible confirmation candidates.  
12. Detect conflicts and correlated duplicate opportunities.  
13. Apply position-state constraints (§7).  
14. Select one primary, optional confirmation, or NO TRADE.  
15. Produce complete decision record (§10).  
16. Pass recommendation to existing risk engine for independent permit/deny.  

### Controller must never

- Place orders  
- Size positions  
- Modify risk limits  
- Change execution assumptions  
- Bypass the existing risk engine  

---

## 10. Decision-record requirements

Future schema must reconstruct decisions. Logical fields:

- Decision identifier  
- UTC timestamp  
- Market-data reference  
- Characterization snapshot reference  
- Data-quality status  
- Primary regime (confirmed + raw if available)  
- Secondary characteristics  
- Eligible engines  
- Ineligible engines + reasons  
- Candidate identifiers + statuses  
- Confidence evidence **by family**  
- Correlated evidence groups  
- Cost assumptions  
- Opportunity assessment  
- Selected primary  
- Selected confirmation (if any)  
- Rejected candidates + reasons  
- Position-state constraints applied  
- Final recommendation  
- NO TRADE reason (if any)  
- Controller configuration / version  

### Storage format (not frozen)

| Option | Pros | Cons |
| --- | --- | --- |
| **JSONL** (proposed default) | Append-only; matches lab event/journal habits; easy offline grep | Less queryable than DB |
| JSON documents per decision | Human readable | Harder streaming |
| SQLite | Queryable | Heavier ops; not required for €0 offline lab |

**Proposal:** JSONL under `research-lab/experiments/runs/<experiment>/<run_id>/` when implementation is authorized — consistent with existing lab isolation. Alternatives remain open until an implementation stage freezes format.

---

## 11. Eligibility matrix (retained hypothesis)

| Market condition | S1 | S2 | S3 | S4 |
| --- | --- | --- | --- | --- |
| Trending | Primary | No | Conditional | Secondary **or** Independent-primary if §5 stricter bar met |
| Ranging | No | Primary | No | Secondary |
| High volatility | Conditional | Conditional | Primary if expansion confirmed | Conditional |
| Low volatility | No | No | No | No |
| Transitioning | No | No | No | No |
| Unknown | No | No | No | No |

S4 row clarified vs Stage 3.3.2: Secondary by default; Independent-primary only under §5.

---

## 12. Engine summaries (unchanged intent)

- **S1** — Trend pullback continuation; preferred TRENDING.  
- **S2** — Liquidity sweep reversal; preferred RANGING; long-only initially.  
- **S3** — Vol expansion breakout; preferred confirmed expansion after compression.  
- **S4** — Momentum confirmation / independent TRENDING primary under stricter evidence; not B0/H1.  

Shared candidate contract and status vocabulary remain as in Stage 3.3.2 §9, refined by evidence families in §6.

---

## 13. Risk and execution separation (unchanged)

Controller recommends. Risk permits/denies. PaperBroker executes paper fills.  
No second risk engine. No fee/spread/slip/risk-limit changes.

---

## 14. Feedback methodology (unchanged principle)

Observations may be logged; automatic parameter changes from small samples forbidden.  
Changing selection rules requires documented OOS research + explicit approval; never sealed holdout.

---

## 15. Known limitations

- Planning refinement only.  
- Numeric thresholds still provisional / largely unspecified by design.  
- Compression bar count for S3 unresolved.  
- Exact PaperBroker position snapshot fields unresolved.  
- Correlation geometry for overlapping invalidations unresolved at code level.  
- ATR and engine proxies are not expected returns.  
- No profitability or readiness claims.

---

## 16. STOP

Stage 3.3.2.1 documentation ends here. Implementation of Stages A–H requires **new explicit authorization**.
