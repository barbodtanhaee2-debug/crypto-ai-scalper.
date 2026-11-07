# Stage 3.3.2 — Architecture Planning Report

**Stage type:** Planning and documentation only  
**Date:** 2026-11-06  
**Companion specification:** `research-lab/docs/STAGE3.3.2_ADAPTIVE_SIGNAL_DISCOVERY_SPECIFICATION.md`  

**No code, tests, historical data, replay, scoring, optimization, benchmarking, event capture, or strategy execution were performed.**

---

## 1. Summary

Stage 3.3.2 defines a **single adaptive paper-scalping system** with four specialized engines (S1–S4) coordinated by a deterministic Adaptive Strategy Controller, consuming Stage 3.3.1 market intelligence and leaving central risk / PaperBroker unchanged.

This report records architecture decisions, dependencies, unresolved questions, proposed (unauthorized) implementation sequence, risks, and hard authorization boundaries.

---

## 2. Architecture decisions

| Decision | Rationale |
| --- | --- |
| One system, four engines | Avoid four unrelated bots; shared candidate contract + one risk path |
| Layered pipeline (Data → Intelligence → Opportunity → Controller → Risk) | Separates concerns; matches Stage 3.3.1 split of regime / secondary / opportunity |
| Secondary traits stay overlapping | Preserves 3.3.1 contract; e.g. TRENDING + HIGH_VOLATILITY allowed |
| Rule-based controller first | Deterministic, auditable; ML explicitly out of scope |
| NO TRADE first-class | Unknown/transitioning/poor DQ/costs/conflicts force abstention |
| Controller recommends only | Existing risk engine remains sole permit/deny authority |
| Eligibility matrix as hypothesis | Starting point, not frozen law |
| Confirmation ≠ second position | Prevents duplicate exposure and fake “independent” evidence |
| S4 new research; B0/H1 frozen | Momentum confirmation without touching baselines |
| S2 long-only initially | Spot constraint + simpler first research surface |
| Dual-track evidence forever | Candle research ≠ quote-tape research; no holdout |

---

## 3. Dependencies on Stage 3.3.1

| 3.3.1 capability | Adaptive use |
| --- | --- |
| `PrimaryRegime` + confirmation/hysteresis | Eligibility + switching gates |
| `VolatilityLevel` / momentum / strength | Matrix rows; S3/S4 conditions |
| `DataQualityStatus` | Hard NO TRADE on bad/insufficient data |
| Feature snapshot (ATR, efficiency, range, EMA) | Engine structure inputs (future) |
| `OpportunityAssessment` + cost defaults | Baseline economic filter |
| `MarketCharacterizationEngine` sequential API | No-look-ahead intelligence feed |

### Limitations inherited (not papered over)

1. **Opportunity proxy is ATR-based** — engines will need their own gross proxies later; must still share cost components.  
2. **No explicit uncertainty score** — uncertainty expressed via UNKNOWN / TRANSITIONING / conflicting / DQ; sufficient for NO TRADE policy, not for calibrated confidence weights.  
3. **Volume optional / off by default** — S2/S3 must not assume volume unless data+config support it.  
4. **Characterization ≠ execution** — 3.3.1 does not talk to PaperBroker; adaptive design keeps that separation.  
5. **`ResearchStrategy` protocol ≠ candidate contract** — adapter required in a later implementation stage.

---

## 4. Unresolved design questions

These require decisions in later authorized stages; this planning stage does **not** invent silent answers:

1. **Secondary-alone proposals:** Default is confirmation-only when a Primary exists; should S4 ever be sole primary under TRENDING with extra evidence?  
2. **Conditional S1/S2 under HIGH_VOLATILITY:** Exact extra checks (ATR percentile? range width?) remain undefined.  
3. **Compression memory for S3:** How many bars of LOW_VOLATILITY history are required before expansion qualifies?  
4. **Open-position switching:** Flatten vs hold vs ignore new primaries — needs explicit policy with risk team constraints.  
5. **Confidence aggregation:** Evidence bag is specified; numeric weighting/calibration method deferred to OOS research approval.  
6. **Candle vs tape controller feed:** When (if ever) quote-tape freshness participates in eligibility — not designed here.  
7. **Minimum net edge constant:** Align with 3.3.1 provisional `minimum_net_edge_fraction` unless a later approval changes research config only (never PaperBroker).  
8. **Decision-record storage format:** JSONL under `experiments/runs/` is likely, but schema not frozen.  
9. **Correlation detector:** “Same-bar overlapping invalidation” needs a precise geometric definition when engines exist.  
10. **Short side:** Out of scope while spot long-only policy holds.

---

## 5. Proposed implementation sequence

**Proposal only — not authorization.**

1. Shared strategy interfaces & decision-record schema  
2. S4 Momentum Confirmation  
3. S1 Trend Pullback Continuation  
4. S3 Volatility Expansion Breakout  
5. S2 Liquidity Sweep Reversal  
6. Adaptive Controller  
7. Strategy coordination (confirm / conflict / correlation)  
8. Integrated synthetic testing  
9. Separate approval: historical candle research (no holdout)  
10. Separate approval: paper-trading integration via existing risk/broker  

### Why this order

- Interfaces first prevent API divergence.  
- S4 is the simplest new engine and doubles as confirmation.  
- S1 maps cleanly onto existing TRENDING intelligence.  
- S3 needs multi-phase vol state beyond a single label.  
- S2 has the highest false-break research risk — last among engines.  
- Controller after real candidates exist.  
- Coordination after a working controller.  
- Synthetic gates before any historical approval.  
- Historical and paper integration remain separately gated.

---

## 6. Risks and limitations

| Risk | Mitigation in plan |
| --- | --- |
| Overfitting eligibility matrix early | Matrix labelled hypothesis; no auto-tuning; holdout banned |
| Treating ATR opportunity as edge | Explicit proxy language; reject weak economics |
| Duplicate correlated entries | One primary; confirmation does not size up |
| Regime flicker switching | Reuse 3.3.1 persistence + controller cooldown/hysteresis |
| Scope creep into production | Hard boundary: research-lab only; risk unchanged |
| Confusing ResearchStrategy with adaptive candidates | Documented adapter as future work |
| Premature historical scoring | Sequence places historical behind explicit approval |
| False confidence percentages | Evidence framework forbids PoP % |

---

## 7. Explicit authorization boundaries

This stage **authorized**:

- Read-only inspection of research-lab docs / Stage 3.3.1 interfaces  
- Creation of the two planning documents named in the stage brief  

This stage **did not authorize** and **did not perform**:

- Implementation or refactors  
- Tests  
- Historical replay / event capture / scoring / optimization / benchmarking  
- Sealed-holdout access  
- B0/H1 or PaperBroker / Stage 2 changes  
- Live capture or production promotion  
- Stage 3.2.9 or Stage 3.3.3+ implementation  
- Access to `F:\crypto-ai-trade-v4`  

All future adaptive code must remain under `F:\crypto-ai-scalper\research-lab\`.

---

## 8. Deliverables checklist

| Deliverable | Path | Status |
| --- | --- | --- |
| Specification | `research-lab/docs/STAGE3.3.2_ADAPTIVE_SIGNAL_DISCOVERY_SPECIFICATION.md` | Written |
| Planning report | `research-lab/reports/STAGE3.3.2_ARCHITECTURE_PLANNING_REPORT.md` | Written |
| Code / tests / experiments | — | **Not created** (by design) |

---

## 9. STOP

Stage 3.3.2 planning is complete.  
**Waiting for explicit approval** before any implementation stage (shared interfaces or engines).
