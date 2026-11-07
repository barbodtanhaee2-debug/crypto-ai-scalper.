# Stage 3.2.8.4 — Final Reliability Closure

**Status:** PASS  
**Date:** 2026-11-06  
**Scope:** `research-lab` only · €0 · paper-only · offline · synthetic fixtures only  
**Confirmation:** No historical experiment, scoring, optimization, benchmarking, live capture, or Stage 3.2.9 work was run.

## Files changed

| Path | Change |
| --- | --- |
| `research_lab/events/writer.py` | `io_stage_fault` hooks; close/retry policy (recoverable keeps handle open); `abandon()`; seal reasons; docs |
| `research_lab/events/validator.py` | Incomplete streams (no terminal) are errors, not mere warnings |
| `research_lab/backtesting/historical_replay.py` | Flush `run_completed` before seal; abandon/uncertain close on persist failure |
| `docs/RESEARCH_EVENT_CAPTURE.md` | Recovery, close/retry, run_completed vs durable persistence, incomplete conditions |
| `tests/test_research_event_capture.py` | F-02 / F-04 / F-09 fault and lifecycle tests |
| `reports/STAGE3.2.8.4_RELIABILITY_CLOSURE_REPORT.md` | This report |

Unchanged by design: B0/H1 rules/parameters, costs, PaperBroker economics, production code, Stage 2 replay, sealed holdout, Stage 3.2.5 artifacts.

## F-02 — Truncate / tell failure fault injection

**Root cause (audit):** Recovery-failure path existed in code but had no dedicated fault tests; incomplete empty streams could still report `validator.ok=True`.

**Implementation:**

- Added test-only `io_stage_fault(stage)` invoked before `tell` / `seek` / `truncate` (`writer.py`).
- Existing `fault_injector` remains for write/partial-write faults.
- Validator: missing terminal `run_completed`/`run_failed` is now an **error** (incomplete ≠ durable-complete).

**Tests (`TruncateFailureFaultTests`):** tell failure; seek failure during recovery; truncate failure during recovery; partial write + failed recovery; repeated emit/flush/close after uncertain; completion refused after uncertain. All assert `persistence_uncertain` stays true, no further writes, no `persistence_complete`, buffer not discarded, validator rejects completeness.

## F-04 — Close-time buffer orphaning

**Root cause (audit):** `close()` always sealed the handle in `finally`, even after a recoverable flush failure, orphaning the buffer and blocking retry.

**Implementation decisions:**

| Case | Behavior |
| --- | --- |
| Recoverable flush failure in `close()` | Handle stays open; `_closed=False`; buffer retained; **retry supported** |
| Unrecoverable / `persistence_uncertain` | Seal handle; buffer retained for inspection; **retry prohibited** |
| `abandon()` | Seal without flush; never complete; used by replay on failed terminal persist |
| Repeated sealed `close()` | Idempotent no-op |

Documented in module docstring and `RESEARCH_EVENT_CAPTURE.md`. Fail-closed completion rules are unchanged: never `persistence_complete` while buffer/uncertain/blocking errors remain.

**Tests (`CloseRetryLifecycleTests` + updated final-completion test):** successful close; recoverable close + retry; unrecoverable close; repeated close; abandon; completion refusal when uncertain.

## F-09 — `run_completed` persistence claim

**Root cause (audit):** Replay emitted `run_completed` with `persistence_complete: True` then relied on `close(mark_complete=True)` to flush, so the claim existed in the buffer before durability.

**Implementation:**

1. `flush()` prior events  
2. `emit("run_completed", … persistence_complete=True)`  
3. `flush()` terminal marker (durability established here)  
4. Refuse if uncertain or buffer remains  
5. `close(mark_complete=True)` seals only  

On persist failure: `abandon()` or uncertain `close(mark_complete=False)`; run raises; summary cannot report success (`persistence_complete` gate unchanged).

**Tests (`CompletionMarkerTests`):** successful completion (marker last, validator ok); failed terminal flush refuses complete run.

## Verification

```
python -m unittest discover -s tests -v
Ran 110 tests in ~1.53s
OK
```

Exact count: **110 tests, 0 failures, 0 errors**.

| Area | Result |
| --- | --- |
| Fault-injection (F-02 + prior F1) | PASS |
| Close/retry (F-04) | PASS |
| Completion marker (F-09) | PASS |
| Capture-on/off equivalence | PASS (existing `ExpandedEquivalenceTests` + accounting test still green) |

## Remaining limitations

- Truncate recovery still assumes seekable local UTF-8 JSONL files.
- After unrecoverable failure, buffered events are inspectable in memory only; disk may be partial — run is incomplete by design.
- `HistoricalCandleReplay` abandons rather than auto-retrying terminal persist (caller sees failure; writer-level retry remains available before abandon).
- Mid-run journal flush on instrumented exits remains an observability side effect.
- Capture default remains off; formal path unchanged.
- No 1M-candle scale proof in this stage (intentionally not run).

## STOP

Stage 3.2.8.4 complete. Waiting for separate explicit approval before any historical event-capture experiment or Stage 3.2.9.
