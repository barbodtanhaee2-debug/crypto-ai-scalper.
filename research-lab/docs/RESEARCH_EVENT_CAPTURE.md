# Research Event Capture (Stage 3.2.8 / 3.2.8.4)

Lab-only instrumentation for durable, deterministic research events on the historical candle replay path.

**Not** live order-book execution. **Not** a change to B0/H1 rules, PaperBroker, or Stage 2 tape replay.

## Purpose

Retain the diagnostic information missing from Stage 3.2.5 / 3.2.7:

- Strategy decisions (including no-trade)
- Entry signals, pending creation, next-open compatibility checks
- Incompatible-open and missing-candle cancellations
- Broker / risk rejections with exact reasons (entry and exit)
- Entry fills and position closes
- Exit pending cancellations when the next candle is missing
- Run completion / failure and persistence status

Future research can separate **signal quality**, **execution compatibility**, and **transaction costs** without re-inferring missing events.

## Schema

- Version: `1.1.0` (`research_lab.events.schema.SCHEMA_VERSION`)
- Format: append-only JSONL
- Required fields on every record:

| Field | Notes |
| --- | --- |
| `schema_version` | `"1.1.0"` |
| `run_id` | Caller-supplied run identifier |
| `arm` | e.g. `B0` / `H1` |
| `event_id` | Stable SHA-derived ID from run/arm/sequence/type |
| `sequence` | Monotonic integer starting at 1 per stream |
| `event_type` | See vocabulary |
| `event_time` | UTC ISO-8601 or `null` |
| `candle_open_time` | UTC ISO-8601 or `null` |
| `signal_time` | UTC ISO-8601 or `null` |
| `signal_price` | float or `null` (never fabricated) |
| `symbol` | default `BTCUSDT` |
| `provenance` | `BINANCE_SPOT_HISTORICAL` |
| `payload` | Event-specific object |

### Event vocabulary

1. `strategy_decision`
2. `entry_signal`
3. `entry_pending`
4. `entry_compatibility_check` — payload includes signal close, next-open mid/bid/ask, stop, target, `compatible`, `cancellation_reason`
5. `entry_cancelled_incompatible`
6. `entry_cancelled_missing_candle`
7. `entry_rejected`
8. `entry_filled`
9. `position_exit_signal`
10. `exit_rejected` — broker rejected a deferred exit (reason from `BrokerResult`; never fabricated)
11. `exit_cancelled_missing_candle` — pending exit dropped due to gap / end-of-series
12. `position_closed`
13. `risk_limit_rejected` — DD / daily-loss / sizing reasons (entry or exit pending)
14. `run_completed` — payload may include `persistence_complete`; see durability rules below
15. `run_failed`

## Lifecycle

```
candle close → strategy_decision
  ├─ entry → entry_signal → entry_pending
  │            └─ next open → entry_compatibility_check
  │                 ├─ compatible → broker.process
  │                 │    ├─ accept → entry_filled
  │                 │    └─ reject → entry_rejected | risk_limit_rejected
  │                 └─ incompatible → entry_cancelled_incompatible
  │            └─ missing N+1 → entry_cancelled_missing_candle
  └─ exit → position_exit_signal
               ├─ next open → broker.process
               │    ├─ accept → position_closed (from durable trade_closed)
               │    └─ reject → exit_rejected | risk_limit_rejected
               └─ missing N+1 → exit_cancelled_missing_candle
run end → flush prior → run_completed|run_failed → flush terminal → seal
```

`lifecycle_id` in payloads ties pending → check → fill/cancel (and exit signal → close/reject/cancel).

## File layout

```
research-lab/experiments/runs/<experiment>/<run_id>/
  B0_events.jsonl
  H1_events.jsonl
  B0_a.jsonl          # existing PaperBroker journal (unchanged role)
  ...
```

- Separate streams per arm.
- Never overwrite an existing event file.
- Paths must remain under `experiments/runs/` (isolation helpers).

## Buffer, flush, and close (Stage 3.2.8.4)

`ResearchEventWriter`:

- Configurable `buffer_limit` (default 512 lines)
- Single open append handle (no per-event open/close)
- Auto-flush at buffer limit
- Empty repeated `flush()` is a no-op; repeated sealed `close()` is idempotent

### Partial-write guarantees

Each flush concatenates the buffer into one payload and records the file offset
before writing. On `OSError`:

1. Attempt `seek`+`truncate` back to the pre-write offset.
2. If truncation succeeds, the file is unchanged for that attempt; the buffer is
   retained and a later flush may retry safely (**no duplicates**).
3. If `tell`, `seek`, or `truncate` fails (or recovery cannot be established),
   `persistence_uncertain` is set, further writes/emits are refused, the buffer
   is retained in memory (**not discarded**), and the run **must not** be marked
   complete.

The writer never blindly re-appends a buffer after an unknown partial write.

### Close / retry policy

| Situation | Handle | Buffer | Retry writes? | `persistence_complete` |
| --- | --- | --- | --- | --- |
| `close()` after successful flush | sealed | empty | no (sealed) | may be True if `mark_complete` |
| Recoverable flush failure during `close()` | **stays open** | retained | **yes** (`flush`/`close`) | False |
| Unrecoverable flush / `tell` failure | sealed | retained (inspection) | **no** (prohibited) | False |
| Already `persistence_uncertain` at `close()` | sealed | retained | **no** | False |
| `abandon()` | sealed without flush | retained | **no** | False |

- Recovery after a **recoverable** failed `close()` attempt is **supported**.
- Recovery after seal due to uncertain/unrecoverable failure is **intentionally prohibited**.
- `HistoricalCandleReplay` calls `abandon()` (or uncertain `close`) if terminal persistence fails, so a failed run does not leave an open handle.

**Limitation:** truncate-based recovery assumes a seekable local file. Non-seekable sinks fail closed.

### `run_completed` vs confirmed durable persistence

These are distinct:

1. **Terminal event** — `run_completed` / `run_failed` records that trading finished.
2. **Payload flag** — `payload.persistence_complete=true` on `run_completed` is only meaningful once that line has been **successfully flushed**.
3. **Writer seal** — `ResearchEventWriter.persistence_complete` is the authoritative flag for the replay summary. It requires a successful flush of all buffered lines (including the terminal event), no uncertainty, and `close(mark_complete=True)`.

Replay order (Stage 3.2.8.4):

1. `flush()` prior events  
2. `emit("run_completed", … persistence_complete=True)`  
3. `flush()` the terminal marker (durability established here)  
4. `close(mark_complete=True)` (seal only; buffer must already be empty)

If step 3 fails, the run remains incomplete: no successful summary, validator must not treat the stream as a complete durable capture.

### When a run is incomplete

A capture-enabled run is incomplete when any of the following hold:

- `persistence_uncertain` is true  
- Buffered events remain unflushed at seal / abandon  
- Unrecovered flush errors remain  
- `run_completed` was not durably flushed  
- Writer `persistence_complete` is false  

`HistoricalCandleReplay.run()` raises rather than returning a successful summary in those cases.

## Integrity validation

```python
from research_lab.events import validate_event_stream
report = validate_event_stream(path)
```

Checks schema, UTC timestamps, sequence continuity, unique `event_id`,
recomputed `event_id` from documented inputs, run/arm consistency,
lifecycle transitions (including fill-after-cancel and close-before-open),
and terminal status.

Distinguishes:

- legitimate absence (no pending ever created)
- unavailable evidence (explicit nulls on non-required fields)
- missing/corrupt evidence (required `lifecycle_id` absent, bad IDs, illegal transitions)

Does **not** silently repair files. Streams without a durable `run_completed`
with `persistence_complete=true` are not accepted as complete.

## Determinism

Identical candles, settings, strategy instance class behavior, `run_id`, and `arm` produce:

- Identical sequences and event IDs
- Identical payloads (no wall-clock fields in events)
- Identical file fingerprints via `fingerprint_event_file`

## Enabling capture

```python
from research_lab.events import ResearchEventWriter
from research_lab.backtesting.historical_replay import HistoricalCandleReplay

writer = ResearchEventWriter(
    path=runs_dir / "B0_events.jsonl",
    run_id="20261106T120000Z",
    arm="B0",
    buffer_limit=512,
)
result = HistoricalCandleReplay(
    candles=...,
    journal_path=runs_dir / "B0.jsonl",
    strategy_object=...,
    research_event_capture=writer,  # None => legacy behavior
).run()
assert result["research_event_capture"]["persistence_complete"]
```

Default remains **off** (`research_event_capture=None`) so existing formal/smoke paths are unchanged until explicitly enabled.

## Performance

- Bounded memory (buffer only)
- Incremental SHA-256 over flushed bytes
- Suitable for future 1M+ candle runs when enabled
- Small-fixture benchmark covered in `tests/test_research_event_capture.py`

## Known limitations

- PaperBroker internals are not modified; risk/reject reasons are observed from `BrokerResult.reason` at the replay layer.
- Strategy objects are not modified; decisions are recorded after `evaluate()` returns.
- Synthetic candle quotes ≠ live L2 / bookTicker.
- Capture does **not** authorize full historical scoring by itself.
- Forced end-of-fixture closes may omit `lifecycle_id` when no exit signal existed (unavailable, not fabricated).
- Mid-run broker journal flush on instrumented exit fills remains an observability side effect (economics unchanged under equivalence tests).

## Inspecting events without replaying

1. Open the arm JSONL under `experiments/runs/...`.
2. Filter by `event_type` (e.g. `entry_cancelled_incompatible`).
3. Join on `payload.lifecycle_id` for a single entry’s lifecycle.
4. Run `validate_event_stream` before analysis.
5. Use `fingerprint_event_file` for deterministic comparison across replicas.
