# Stage 3.2.2 — H1 capture readiness audit

Paper-only. **Collection was not started.** No extended replay was launched. H1, production trading, frozen parameters, the broker, and the Stage 2 replay engine were not modified.

Stage 3.2.1 remains **inconclusive** (2 scored baseline trades, 1 scored H1 candidate trade). Overlay evidence stays at:

- `research-lab/reports/STAGE3.2.1_H1_REPORT.md`
- `research-lab/reports/h1/opportunity_overlay.csv`

Sealed holdout: original session candles with `close_time >= 2026-10-31T04:20:59.999Z` must never be loaded for H1 scoring.

This is not a profitability claim.

## 1. Public collector configuration and data source

Module: `quote_tape_collector.py`. Isolated diagnostics collector. It does not import `PaperBroker` or `PaperRuntime` and does not place orders.

| Item | Value |
| --- | --- |
| Symbol | BTCUSDT Spot |
| Quote stream | `wss://stream.binance.com:9443/ws/btcusdt@bookTicker` |
| Candle stream | `wss://stream.binance.com:9443/ws/btcusdt@kline_1m` |
| Auth | `authenticated: false`, `credentials_used: false`, `order_endpoints: []` |
| Default output root | `quote-tape/sessions/<UTC>-<id>/` (new directory per run) |
| Forbidden output names | `paper_broker.jsonl`, `runtime_status.json` |
| Stop | Ctrl+C → `status: stopped` and `ended_at_utc` |
| Optional `--runtime-status` | **read-only** paper heartbeat; not required for H1 tape |

For H1 evidence capture, run **without** `--runtime-status` so the session does not couple to the live paper runtime.

Tests assert those WebSocket URLs and the absence of `/api/v3/order`, `/sapi/`, `x-mbx-apikey`, `api_secret`.

bookTicker `E` is stored only if present; missing `E` is `null`. The collector does not invent exchange timestamps. H1 replay continues to use **host `received_at_utc`**.

## 2. Output paths, timestamps, continuity, integrity

### Output layout (unchanged)

| File | Role |
| --- | --- |
| `session.json` | Atomic replace (~5 s while running; forced after each stored candle) |
| `quotes.jsonl` + `quotes.partN.jsonl` | Rotation at 20,000,000 bytes |
| `candles.jsonl` | Completed klines only (`x: true`) |
| `events.jsonl` | connect / disconnect / reconnect / shutdown, including `gap_seconds` |
| `diagnostics.jsonl` | Rejects, duplicates, missing intervals |
| `position_snapshots.jsonl` | Unused if `--runtime-status` is omitted |

New sessions get **new** directory names. Do not write into `quote-tape/sessions/20261030T191728Z-4751556f`.

### Timestamps

- Quotes and candles: timezone-aware `received_at_utc` (host receive).
- Candles: `open_time_utc`, `close_time_utc`; kline `E` as `exchange_event_timestamp` when supplied.
- Negative `close_age_seconds` is recorded; the closed candle is still stored. H1 decision clock remains `host_receive`, not kline `E`.
- Do not apply kline median(E − receive) to quotes.

### Continuity (collector + loader)

- Quote `update_id`: duplicates and regressions counted; duplicates not treated as extra stored evidence.
- Candle open time: expected previous + 60 s; gaps increment `candles_missing_interval` and a diagnostic. Missing minutes are **not** interpolated (H1 cancels a pending signal if t+1 is absent).
- Disconnect/reconnect: `events.jsonl` gap start/end; empty quote windows stay empty.

### Integrity after stop (existing tools)

1. `session.json`: `status=stopped`, `ended_at_utc` populated.
2. Streaming SHA-256 of every file in the **new** session directory (Stage 2.3 pattern; `quote-tape/reports/audit_stage23_extended.py` is the prior session’s audit script — copy the **method**, do not overwrite that session’s manifest).
3. `load_quote_tape_session` issue scan (read-only).
4. Catalog bind: `session_json_sha256` must match both the file and the manifest row.

## 3. Collection without frozen-component changes

The existing CLI is sufficient:

```text
python quote_tape_collector.py --output-root quote-tape/sessions
```

No change is required to `strategy.py`, `baseline.py`, `paper_broker.py`, `quote_tape_replay.py`, H1 confirmation rules, or `configs/h1_next_bar_close.json`.

The collector already ran a ~12 h public session (Stage 2.3). There is no duration flag; the operator stops with Ctrl+C. That is acceptable for future H1 tape.

## 4. How new data will be catalogued and audited

1. New session id under `quote-tape/sessions/` — never mutate the sealed Stage 2.3 directory.
2. After clean stop: SHA-256 manifest + validation note under `quote-tape/reports/` with a **new** filename (do not replace `STAGE2.3_EXTENDED_SHA256_MANIFEST.csv`).
3. When **authorized**, append a pointer to `research-lab/datasets/catalog.json` (`dataset_id`, `session_dir`, `manifest_path`, `validation_report`, `session_json_sha256`, `immutable: true`).
4. `bind_dataset` refuses: unknown id, missing files, hash mismatch, `status != stopped`, missing `ended_at_utc`.
5. Original catalog row for `20261030T191728Z-4751556f` stays the H1 snapshot identity.

`run_h1()` currently binds **only** that original dataset and applies the original chronological split. Multi-session H1 scoring is **not** implemented in this audit. Counting new-session trades will need a later **authorized** research-lab runner change (or a one-off lab script) that:

- still excludes original holdout `close_time >= 2026-10-31T04:20:59.999Z`
- adds **new** stopped sessions in full (until a future holdout is reserved from data not yet collected)
- does not change H1 rules or frozen parameters

## 5. Safe collection procedure (not authorized to start)

1. From `F:\crypto-ai-scalper`, with project venv, no API keys in the environment for this process.
2. Start `python quote_tape_collector.py --output-root quote-tape/sessions` (no `--runtime-status`).
3. Confirm the new directory name is **not** the sealed session id.
4. Leave it running; Ctrl+C for a clean stop.
5. Verify `stopped` / `ended_at_utc`, hashes, loader issues, gaps not filled.
6. Do not copy or merge files into the original capture.
7. Do not replay holdout klines. Do not tune H1 after seeing tape.

## 6. Monitoring baseline and candidate scored trade counts

**Gate:** ≥ 30 **scored completed** trades on **each** arm (entry and exit; `host_receive`; frozen costs; long-only). Overlay remains primary when a replay is authorized.

Current frozen counts (do not reset):

| Arm | Scored completed trades |
| --- | ---: |
| host_receive frozen `momentum_breakout` | 2 |
| H1 next-bar confirmation | 1 |

After a new session is validated **and** an H1 evaluation replay is separately authorized:

- Replay **new** session(s) with the same lab engine, `DecisionLoggingAdapter` vs `ConfirmedMomentumBreakoutStrategy`, `host_receive`, 3 s freshness.
- Score closed trades with `scored_trades` / holdout mask so original holdout timestamps never count.
- Add new-session scored counts to 2 and 1. **Do not** replace Stage 3.2.1 journals.
- `economic_classification(baseline_n, candidate_n)` stays fail-closed below 30.
- Two-run journal fingerprints per new evaluation.
- Monitor **counts**, not P&L, win rate, or confirmation fraction, for stopping.

Do not use `run_h1()` as-is on a session that would re-split the original 725 candles and expose the holdout.

## 7. The ~260-hour figure is not a stopping condition

Stage 3.2.2 planning noted that, **if** the candidate kept producing about one scored trade per 9.05 eligible hours, on the order of **~260 additional eligible hours** might be needed. That extrapolation uses **n = 1** confirmed trade. It is **uncertain** and **must not** be used as a stopping rule, quota, or success criterion.

**Stop collection only when:**

- scored completed trades ≥ 30 on baseline **and** ≥ 30 on the H1 candidate, same pre-registered rules, or
- integrity failure, or
- operator cap (14 days of public capture) then reassess **without** unblinding the sealed holdout and **without** changing H1.

**Do not stop because** ~260 hours elapsed, or because interim P&L “looks good” or “looks bad.”

## Constraints recap

- No capture started in this stage.
- No live orders, API keys, or authenticated endpoints.
- Sealed holdout beginning `2026-10-31T04:20:59.999Z` remains sealed.
- Estimated hours are a planning prior only.

## Tests (this audit)

| Suite | Result |
| --- | --- |
| `python -m unittest tests.test_quote_tape_collector` | **26 OK** |
| `python -m unittest discover -s research-lab/tests` | **22 OK** |

No collector or H1 code was changed to obtain those results. Collection was not started.
