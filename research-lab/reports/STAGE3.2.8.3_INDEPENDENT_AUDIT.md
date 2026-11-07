# STAGE3.2.8.3_INDEPENDENT_AUDIT

**Stage:** 3.2.8.3 — Independent read-only code audit of Stage 3.2.8.2 claims  
**Auditor posture:** Code inspection only. No implementation changes. No historical replay, scoring, benchmarking, live capture, or sealed-holdout access.  
**Evidence base:** Source under `research-lab/research_lab/events/{writer,schema,validator}.py`, `research-lab/backtesting/historical_replay.py`, and `research-lab/tests/test_research_event_capture.py`, cross-checked against `reports/STAGE3.2.8.2_RELIABILITY_PATCH_REPORT.md`.  
**Environment note (IO probe only, not a lab suite re-run):** On this host’s Python 3.15.0 (Win64), UTF-8 text-mode `tell()` matched on-disk byte size for ASCII and non-ASCII JSONL-like writes, and `seek(start); truncate(start)` restored prior content. That probe supports local semantics; it is not a multi-platform proof.

**Overall verdict: CONDITIONAL PASS**

The Stage 3.2.8.2 F1 design is present and, for seekable local UTF-8 JSONL files when truncate recovery succeeds, prevents duplicate append retries. Fail-closed paths for uncertain persistence exist in code. Gaps remain: truncate-failure is untested; `close()` can orphan a recovered buffer by closing the handle; `run_completed` asserts `persistence_complete: True` before close confirms durability.

---

## Focus question 1 — Does seek + truncate recovery prevent duplicate writes?

**Classification: PASS** (with platform CONDITIONAL note below)

**Code:** `ResearchEventWriter.flush` / `_try_truncate`  
`research_lab/events/writer.py` L187–262

| Step | Lines | Behavior |
| --- | --- | --- |
| Concatenate buffer | L205 | Single payload; not line-at-a-time without tracking |
| Record offset | L206–215 | `tell()` before write; `tell` failure → `_mark_uncertain` |
| Write | L217–222 | `write` + `flush`, or test `fault_injector` |
| On `OSError` | L223–238 | `_try_truncate(start_pos)`; buffer **not** cleared; re-raise |
| Truncate | L246–262 | `seek(start_pos)` then `truncate(start_pos)` then `flush` |
| Success | L240–244 | Fingerprint update + `_buffer.clear()` only after success |

If truncate returns True, the next `flush()` rewrites the same buffer once from the restored offset. That is the anti-duplicate mechanism claimed in 3.2.8.2.

**CONDITIONAL note:** Recovery assumes seekable text IO where `tell()`/`truncate(size)` agree on file size. Documented limitation matches (`writer.py` L1–18). Verified on this Python 3.15 Win64 for UTF-8 samples; not independently proven for every OS/Python text-IO cookie model. Safer portable idiom would be `seek(start_pos); truncate()` with no size argument — current code passes `start_pos` explicitly (L250–251).

**Report claim match:** Yes for the recovered-retry case.

---

## Focus question 2 — What if seek or truncate itself fails?

**Classification: PASS (code) / NOT VERIFIED (tests)**

**Code:** `_try_truncate` L246–262; `flush` L223–238; `_mark_uncertain` L264–282

- Truncate/`seek`/`flush` `OSError` → `_try_truncate` returns `False` and appends a diagnostic error with `detail="truncate_after_partial_write_failed"` (L254–261).
- `flush` then sets `_persistence_uncertain = True` when `not recovered` (L235–236).
- Pre-write `tell()` failure calls `_mark_uncertain` with `detail="tell_failed_before_write"` (L208–215).
- Buffer is retained (not cleared) on the `OSError` path (L237–238 comment; no `clear()` in `except`).

**Test gap:** `FaultInjectionFlushTests` (`tests/test_research_event_capture.py` L435–589) only injects write/`OSError` paths where truncate **succeeds** (`persistence_uncertain` asserted False at L462, L519). There is **no** test that forces `_try_truncate` to fail or asserts `persistence_uncertain is True`.

**Report claim match:** Code matches the written fail-closed rule. The claim is **not** backed by a dedicated fault test → NOT VERIFIED by automated evidence.

---

## Focus question 3 — Can `persistence_uncertain` be bypassed?

**Classification: PASS** (public/control-flow API)

| Gate | File:lines | Effect |
| --- | --- | --- |
| `emit` | `writer.py` L153–157 | Raises; no new events |
| `flush` | `writer.py` L196–199 | Raises; no write |
| `close` | `writer.py` L290–291 | Skips `flush()` when uncertain |
| `persistence_complete` property | `writer.py` L117–124 | Requires `not self._persistence_uncertain` |
| `close(mark_complete=True)` | `writer.py` L292–306 | Will not set `_persistence_complete True` if uncertain; forces False when mark requested with residual problems |

No public setter clears `_persistence_uncertain`. Replay refuses a successful summary when capture is on and `persistence_complete` is false (`historical_replay.py` L947–950).

**Not a bypass, but a related hole:** Private attribute assignment (`writer._persistence_uncertain = False`) is possible in Python; not used by lab code paths inspected.

**Report claim match:** Yes for intended API.

---

## Focus question 4 — Can `close()` incorrectly mark an incomplete stream complete?

**Classification: PASS** for the completion flag rules; **CONDITIONAL PASS** for post-failure recoverability

**Code:** `ResearchEventWriter.close` `writer.py` L284–313

Completion requires all of (`writer.py` L292–298):

- `mark_complete=True`
- no `_blocking_flush_errors` (unrecovered only; L104–115)
- empty `_buffer`
- not `_persistence_uncertain`

Otherwise, when `mark_complete` was requested with residual problems, `_persistence_complete` is forced False (L301–306). Idempotent early return when already closed (L287–288).

**CONDITIONAL finding — orphaned buffer on flush failure during close:**

```text
close() try: flush()  → OSError (even if truncate recovered)
finally: close handle; _closed = True
```

(`writer.py` L289–313)

After that, buffer may still hold lines, but `emit`/`flush` refuse because `_closed` / no handle. Completion stays False (fail-closed for the **claim**), yet the 3.2.8.2 narrative “buffer retained for safe retry” does **not** hold across a failed `close()`. Durable bytes are not falsely marked complete; events can still be lost from disk with only an in-memory orphan.

**Replay interaction:** `historical_replay.py` L902–928 emits `run_completed` with `"persistence_complete": True` in the **payload** before `close(mark_complete=True)`. If that flush fails, exception path / L947–950 prevents a successful return. Payload can still theoretically claim True only if those bytes were durably written; writer flag remains the gate for the run result.

**Report claim match:** “Never mark complete if persistence uncertain” — PASS. “Buffer retained for retry” after failed close — overstated → CONDITIONAL.

---

## Focus question 5 — Event IDs and lifecycle validation

**Classification: PASS** with minor CONDITIONAL edges

### Event IDs

- Construction: `make_event_id` `schema.py` L70–75; used in `writer.emit` L163–168.
- Recompute + reject: `validator.py` L118–135.
- Schema version / vocabulary: `schema.py` L15–53 (`1.1.0`, includes exit types + `LIFECYCLE_REQUIRED_TYPES`).

### Lifecycle

| Check | Location |
| --- | --- |
| Required `lifecycle_id` vs unavailable null | `validator.py` L140–149 |
| Duplicate pending entry | L166–171 |
| Fill-after-cancel / cancel-after-fill | L198–207, L240–243 |
| Duplicate fill | L244–247 |
| Close-before-open | L282–287 |
| Exit pending / exit cancel | L256–281 |
| `risk_limit_rejected` entry or exit | L211–233 |

**CONDITIONAL edges:**

1. `position_closed` is **not** in `LIFECYCLE_REQUIRED_TYPES` (`schema.py` L38–53). Forced end close omits `lifecycle_id` (`historical_replay.py` L871–889). Matches 3.2.8.2 limitation text; validator will not treat that omission as corrupt.
2. `risk_limit_rejected` clearing logic (`validator.py` L226–233) is heuristic when both entry and exit pending were somehow open (malformed stream). Replay does not keep both open simultaneously under normal emission.
3. `entry_compatibility_check` allows `prior in {None, signaled, pending_entry}` (L181–187) — does not require an open pending flag; slightly weaker than pending→check coupling, but still rejects spent states.

**Report claim match:** Core claims hold.

---

## Focus question 6 — Exit rejection and missing-candle events

**Classification: PASS**

| Event | Emission site | Condition |
| --- | --- | --- |
| `exit_cancelled_missing_candle` (gap) | `historical_replay.py` L367–380 | `pending.action == "exit"` after gap |
| `exit_cancelled_missing_candle` (EOS) | L825–837 | End-of-series pending exit |
| `exit_rejected` / `risk_limit_rejected` | L600–667 | `pending.action == "exit"` and `capture is not None`; reject uses `BrokerResult.reason` via `is_risk_limit_reason` (L649–653) |

Reasons are taken from broker results / existing gap machinery — no fabricated prices. Entry gap cancel remains at L353–366.

Coverage gated on capture: `_emit` no-ops when capture is None (`historical_replay.py` L288–290), so disabled runs do not write these types (correct).

---

## Focus question 7 — Capture-disabled replay behavior unchanged

**Classification: PASS**

| Evidence | Location |
| --- | --- |
| Default off | `historical_replay.py` L222–224 `research_event_capture: ... = None` |
| Emit gate | L288–290 early return |
| Exit journal flush / trade re-read only if capture on | L600–603, L866–868 |
| Formal entrypoint does not pass a writer | `run_formal_historical_replay` L1072–1082 |

Trading counters, `broker.process`, pending cancel accounting, and strategy evaluation run regardless of capture. Capture-on adds mid-run journal flush for exit observability; that is a side effect on journal durability timing, not on PaperBroker economics used for decisions. Formal scoring path remains capture-off by construction.

---

## Cross-check of other 3.2.8.2 report claims

| Claim | Audit result |
| --- | --- |
| F1 root cause addressed by truncate-before-retry | **PASS** (`writer.py` L187–244) |
| Recovered flush errors do not block later completion | **PASS** (`_blocking_flush_errors` L104–115, L292–298) |
| Schema 1.1.0 + exit vocabulary | **PASS** (`schema.py` L15–53) |
| Equivalence / capture-off economics | **PASS** by code gating + existence of equivalence tests (not re-executed as this audit’s proof) |
| Truncate-failure fail-closed tested | **NOT VERIFIED** (no test) |
| “Buffer retained for retry” after any failure | **CONDITIONAL PASS** — true until `close()` closes the handle |

---

## Finding register

| ID | Severity class | Summary |
| --- | --- | --- |
| F-01 | **PASS** | Seek/truncate-on-`OSError` prevents duplicate retry when recovery succeeds (`writer.py` L206–244, L246–252). |
| F-02 | **PASS** / **NOT VERIFIED** | Truncate/`tell` failure sets `persistence_uncertain` and blocks emit/flush/complete (`writer.py` L208–215, L235–236, L153–157, L196–199); **untested**. |
| F-03 | **PASS** | Public API cannot bypass `persistence_uncertain` for completion. |
| F-04 | **PASS** / **CONDITIONAL PASS** | `close(mark_complete=True)` will not claim complete when incomplete; failed `close` flush orphans buffer (`writer.py` L284–313). |
| F-05 | **PASS** | Event ID recompute + lifecycle illegal transitions implemented (`validator.py` L118–135, L153–296). |
| F-06 | **PASS** | Exit reject / exit missing-candle emissions correct and non-fabricating (`historical_replay.py` L367–380, L600–667, L825–837). |
| F-07 | **PASS** | Capture default off; formal path unmodified; `_emit` is a no-op without writer. |
| F-08 | **CONDITIONAL PASS** | Text-mode `truncate(start_pos)` portability not proven beyond this Python 3.15 Win64 probe. |
| F-09 | **CONDITIONAL PASS** | `run_completed` payload hardcodes `persistence_complete: True` before close (`historical_replay.py` L925–928); mitigated by writer flag + L947–950. |

No **FAIL** findings that overturn the core “no blind full-buffer retry after unknown partial write when truncate succeeds” claim.

---

## Is a small bounded smoke test justified?

**Yes — narrowly.**

Justified smoke (synthetic only, after separate approval if it writes under `experiments/runs/`):

1. Tiny scripted candle fixture with `research_event_capture` enabled → assert `persistence_complete`, `validate_event_stream(...).ok`, and presence of at least one entry or exit lifecycle type.
2. Optional: inject truncate failure (mock `_try_truncate` → False) and assert `persistence_uncertain` and refused completion — closes F-02 evidence gap.

**Not justified here:** historical capture experiment, scoring, 1M-candle bench, or holdout access.

---

## Conclusion

Stage 3.2.8.2’s reliability patch is **conditionally validated** by independent code review: the F1 duplicate-write defect is fixed for the recovered-truncate path; uncertain persistence is fail-closed in the public API; exit coverage and capture-off gating match the report. Residual risk is concentrated in **untested truncate-failure**, **close-time buffer orphaning**, and **text-IO truncate portability**.

**STOP.** No further implementation or historical work under this stage.
