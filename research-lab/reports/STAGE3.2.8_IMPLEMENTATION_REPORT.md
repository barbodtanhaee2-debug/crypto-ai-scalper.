# Stage 3.2.8 — Bounded Research Event Capture Infrastructure

Paper-only. €0. Instrumentation only. No full historical scoring, no B0/H1 rule changes, no PaperBroker/Stage 2/tape modifications.

## Pre-implementation audit findings

| Area | Finding |
| --- | --- |
| In-memory `_events` on `HistoricalCandleReplay` | Exists but formal path sets `retain_verbose_events=False` and Stage 3.2.5 strips events from results JSON |
| PaperBroker journal | Durable fills/rejects; **no** incompat-cancel rows; mark_to_market optionally omitted |
| Buffered broker journal | `_install_buffered_broker_journal` in `historical_replay.py` (flush batches / end) |
| Missing for 3.2.7 | Full entry decisions, signal prices, compatibility checks, incompat cancels with ask/stop/target |
| Event-generation points | Gap cancel; next-open compat; deferred `broker.process`; post-close `strategy.evaluate`; end-of-series cancel; optional force close |
| Constraint | Capture must observe replay-layer outcomes only — **no** PaperBroker or strategy source edits |

## Files created and modified

**Created**
- `research-lab/research_lab/events/__init__.py`
- `research-lab/research_lab/events/schema.py`
- `research-lab/research_lab/events/writer.py`
- `research-lab/research_lab/events/validator.py`
- `research-lab/tests/test_research_event_capture.py`
- `research-lab/docs/RESEARCH_EVENT_CAPTURE.md`
- `research-lab/reports/STAGE3.2.8_IMPLEMENTATION_REPORT.md` (this file)

**Modified**
- `research-lab/research_lab/backtesting/historical_replay.py` — optional `research_event_capture` instrumentation; trading path semantics preserved when capture is `None` or enabled

## Event schema and vocabulary

- Schema version: `1.0.0`
- Documented in `docs/RESEARCH_EVENT_CAPTURE.md` and `events/schema.py`
- Vocabulary: `strategy_decision`, `entry_signal`, `entry_pending`, `entry_compatibility_check`, `entry_cancelled_incompatible`, `entry_cancelled_missing_candle`, `entry_rejected`, `entry_filled`, `position_exit_signal`, `position_closed`, `risk_limit_rejected`, `run_completed`, `run_failed`
- Compatibility payload retains signal close, next-open mid/bid/ask, stop, target, result, cancellation reason
- Explicit nulls for unavailable fields; no fabricated signal prices; no secrets

## Event capture coverage

| Concern | Coverage |
| --- | --- |
| Decisions (incl. no-trade) | `strategy_decision` each candle when capture enabled |
| Entry lifecycle | signal → pending → compatibility → fill/cancel/reject |
| Risk rejects | Mapped from `BrokerResult.reason` to `risk_limit_rejected` vs `entry_rejected` |
| Position close | From durable `trade_closed` after journal flush on exit |
| Run lifecycle | `run_completed` / `run_failed` + persistence status |
| Default formal path | Capture **off** (`None`) — no behavior change until explicitly enabled |

## Buffering and durability

- `ResearchEventWriter`: bounded buffer (default 512), single open append handle, auto-flush, final flush, exception-path flush attempt
- Refuses overwrite; requires `experiments/runs/`
- Incomplete persistence → run raises rather than claiming success
- Repeated empty flush does not duplicate records

## Integrity validator results

`validate_event_stream` covered by unit tests:

- Schema / vocabulary rejection
- Sequence gap detection
- Duplicate `event_id` detection
- Lifecycle pending→cancel / pending→fill
- Terminal `run_completed` / `run_failed`

## Determinism results

- Stable `make_event_id(run_id, arm, sequence, event_type)`
- Replica event-file fingerprints equal for same arm on identical synthetic fixtures
- Different arms produce different fingerprints (arm embedded in IDs)

## Instrumented versus non-instrumented equivalence

Synthetic fixture comparison asserts equality of:

`trades`, `cash`, `equity`, `decision_counts`, entry/cancel counters, `trade_count`

Instrumentation does not alter strategy decisions or accounting on the tested fixture.

## Full lab test results

```
python -m unittest discover -s research-lab/tests
Ran 80 tests in ~1.2s
OK
```

(Prior baseline 68 + 12 new Stage 3.2.8 tests.)

## Small-fixture performance benchmark

- 500 synthetic candles with capture enabled
- Assert elapsed < 15s and > 20 candles/s (loose offline budget)
- Not a full-window measurement; no 1M-candle run executed

## Isolation verification

- Event paths go through `assert_isolated_path`
- Must live under `experiments/runs/`
- Live journal names still refused
- No access to `F:\crypto-ai-trade-v4`, credentials, holdout scoring, or live capture

## Remaining limitations

1. Exit broker rejects lack a dedicated event type (PaperBroker journal only).
2. Capture observes replay outcomes; does not instrument inside `paper_broker.py` / `strategy.py`.
3. Enabling capture on a future full historical run is a **separate authorization** — not done here.
4. Synthetic candle quotes remain non-equivalent to live L2 / tape execution.
5. `run_formal_historical_replay` still does not enable capture by default.

## Stop

Implementation and offline verification complete. No full historical dataset run, no Stage 3.2.5 rerun, no strategy/cost/risk changes, no live capture, no holdout access.
