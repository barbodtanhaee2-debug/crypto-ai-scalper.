# Stage 3.2.8.2 — Event Writer Reliability and Coverage Patch

**Status:** PASS (implementation + synthetic verification only)  
**Date:** 2026-11-06  
**Scope:** `research-lab` only · €0 · paper-only · offline · no historical scoring / full replay  
**STOP:** No historical event-capture experiment was run. Awaits separate explicit approval.

## Files changed

| Path | Change |
| --- | --- |
| `research_lab/events/writer.py` | F1 partial-write safety: pre-write offset, truncate-on-`OSError`, `persistence_uncertain` fail-closed, idempotent flush/close, documented guarantees, test `fault_injector` |
| `research_lab/events/schema.py` | Schema `1.1.0`; add `exit_rejected`, `exit_cancelled_missing_candle`; `LIFECYCLE_REQUIRED_TYPES` |
| `research_lab/events/validator.py` | Recompute `event_id`; lifecycle transition checks (fill-after-cancel, close-before-open, duplicate transitions); unavailable vs missing evidence |
| `research_lab/backtesting/historical_replay.py` | Emit `exit_rejected` / `risk_limit_rejected` on exit broker reject; emit `exit_cancelled_missing_candle` on gap / end-of-series |
| `docs/RESEARCH_EVENT_CAPTURE.md` | Vocabulary 1.1.0, flush guarantees, exit lifecycle |
| `tests/test_research_event_capture.py` | Fault-injection, lifecycle, exit coverage, expanded capture-on/off equivalence |
| `reports/STAGE3.2.8.2_RELIABILITY_PATCH_REPORT.md` | This report |

Unchanged by design: B0/H1 rules/parameters, costs, PaperBroker economics, production code, Stage 2 replay, sealed holdout, Stage 3.2.5 artifacts.

## Root cause and fix for F1

**Root cause (Stage 3.2.8.1):** `flush()` used `writelines` then cleared the buffer only on success. A partial write left bytes on disk **and** the full buffer intact, so a retry could duplicate events.

**Fix:**

1. Concatenate the buffer into one payload; record `tell()` before write.
2. On `OSError`, attempt `seek`+`truncate` to the pre-write offset.
3. If truncate succeeds → file unchanged for that attempt; buffer retained; safe retry (no duplicates). Recovered errors are diagnostic-only and do not block later `persistence_complete` after a successful flush.
4. If truncate fails (or pre-write `tell` fails) → set `persistence_uncertain`, refuse further emit/flush writes, retain buffer (not discarded), never mark complete.
5. Empty `flush()` is a no-op; repeated `close()` is idempotent.

**Guarantees / limitations:** Documented in `ResearchEventWriter` module docstring and `docs/RESEARCH_EVENT_CAPTURE.md`. Truncate recovery assumes a seekable local file; non-seekable sinks fail closed.

## Fault-injection results

| Scenario | Result |
| --- | --- |
| Failure before any bytes | OSError; empty file; buffer retained; retry succeeds; `persistence_complete=True` after close |
| Failure after complete JSONL lines | Truncate recovers; retry writes exactly once; sequences `[1,2]` |
| Failure partway through a JSONL line | Truncate recovers; retry yields valid JSONL |
| Retry after partial write | No duplicate `event_id` / sequences |
| Repeated flush and close | Idempotent; single durable line |
| Failure during final completion | `close(mark_complete=True)` fails closed; empty file; `persistence_complete=False` |

All six covered by `FaultInjectionFlushTests` — **PASS**.

## Lifecycle validation results

`validate_event_stream` now:

- Recomputes `event_id` via `make_event_id(run_id, arm, sequence, event_type)` and rejects mismatches.
- Tracks entry/exit lifecycle states; detects duplicate pending, fill-after-cancel, duplicate fill/cancel/close, close-before-open.
- Treats missing required `lifecycle_id` as **corrupt/missing evidence** (error).
- Treats explicit null `lifecycle_id` on non-required types as **unavailable evidence** (recorded, not an error).

`LifecycleValidationTests` — **PASS**.

## Added event coverage

| Event | When emitted | Fabrication |
| --- | --- | --- |
| `exit_rejected` | Deferred exit `broker.process` returns not accepted (non-risk reason from `BrokerResult`) | None — reason from broker |
| `risk_limit_rejected` | Same path when `is_risk_limit_reason(result.reason)` | None |
| `exit_cancelled_missing_candle` | Pending exit cancelled on candle gap or end-of-series | None — uses existing gap/end signals |

Schema version bumped to **1.1.0**. Vocabulary documented and asserted in tests.  
`ExitEventCoverageTests` — **PASS**.

## Instrumented / non-instrumented equivalence

Capture-on vs capture-off synthetic comparisons (`ExpandedEquivalenceTests` + prior accounting test) cover:

- Trades and trade ordering  
- Cash and equity  
- Fees / slippage / total costs (when present on trade records)  
- Entry/exit decision counts and rejection reasons  
- Risk / cancel counters (`entries_*`, `fills_cancelled_*`)  
- Missing-candle behavior  
- Force-close behavior  
- B0-like production adapter accounting on the same fixture  

**PASS** — no trading-behavior changes were made to satisfy tests.

## Full lab test results

```
python -m unittest discover -s tests -v
Ran 97 tests in ~1.35s
OK
```

Exact count: **97 tests, 0 failures, 0 errors**.

(Event-capture module alone: 29 tests OK.)

Constraints honored: small synthetic fixtures only; no historical scoring; no full historical replay; no 1M-candle benchmark.

## Remaining limitations

- Mid-run broker journal flush + `_closed_trades()` re-read on exit fills remains (observability side effect; economics unchanged per equivalence tests).
- Truncate-based recovery requires seekable local files.
- Forced end-of-fixture `position_closed` may omit `lifecycle_id` when no exit signal existed (unavailable, not fabricated).
- Capture default remains **off**; formal paths unchanged until explicitly enabled.
- 1M-candle write scale still not proven in this stage (intentionally not run).
- No historical event-capture experiment in this stage.

## Verdict

Stage 3.2.8.2 reliability/coverage patch is **complete** for the authorized synthetic scope.  
**Stopped** before any historical event-capture experiment pending separate explicit approval.
