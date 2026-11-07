# Stage 3.2.2 — H1 evidence-collection plan

Paper-only planning. The pre-registered H1 rule (next-bar close confirmation of frozen `momentum_breakout`) is **unchanged**. Production trading, frozen parameters, the broker, and the Stage 2 replay engine are **unchanged**. No extended replay was launched. No capture was started. This document does **not** claim profitability or that confirmation is superior.

The Stage 3.2.1 result is preserved as the frozen H1 snapshot. Do not rewrite H1 decision logic to chase sample size.

## 1. Audit of Stage 3.2.1 artifacts

### Report

Source: `research-lab/reports/STAGE3.2.1_H1_REPORT.md`

Checked against `research-lab/reports/h1/h1_results.json`, journals under `research-lab/experiments/runs/h1/`, catalog bind, and `session.json` SHA-256. No disagreement on counts, clock, holdout start, or classification.

The overlay path printed in the H1 report is `research-lab/reports/h1/opportunity_overlay.csv`. There is **no** file at `research-lab/h1/opportunity_overlay.csv`.

### Overlay CSV (primary comparison)

Two rows; both on 2026-10-30, both **before** holdout `2026-10-31T04:20:59.999Z`.

| Signal bar close | Outcome | Quote ≤ decision | Candle close ≤ decision |
| --- | --- | --- | --- |
| 2026-10-30T19:55:59.999Z | `confirmation_fail` (t+1 at 19:56:59.933Z) | true | true |
| 2026-10-30T23:33:59.999Z | `confirmation_pass` (t+1 at 23:34:59.989Z) | true | true |

This matches the report: 2 baseline long signals, 1 confirmed, 1 failed close, 0 missing minutes, 0 unresolved.

### Full-path (secondary)

- Matched: 1 pair, candidate entry **59.70 s** after baseline (one host minute), both max-holding exits.
- Unmatched baseline: 1 long at 19:56:00Z — the failed-confirmation overlay row. Not forced into a match.
- Unmatched candidate: 0.

### Sample counts (verified)

| Item | Report | Artifact |
| --- | ---: | ---: |
| Train / validation / holdout candles | 362 / 181 / 182 | `h1_results.json` split |
| Replay candles (holdout excluded) | 543 | 362+181 |
| host_receive baseline scored completed trades | 2 | classification + baseline metrics |
| H1 candidate scored completed trades | 1 | classification + candidate metrics |
| Stale candle decisions (both arms) | 1 | `h1_results.json` |
| Economic class | inconclusive | both arms &lt; 30 |

Independent `host_receive` baseline on the **eligible** window is 2 trades. Stage 2.4 `kline_event` counts were not used (`kline_event_stage24_counts_not_used: true`). Stage 3.1’s 3 host_receive trades on the **full** 725-candle session included holdout time; those extra trades are correctly **absent** here.

### Timestamps vs holdout

Holdout starts `2026-10-31T04:20:59.999000+00:00`. Overlay signals, matched fills, and unmatched baseline entry/exit (`20:22Z`) are all earlier. This audit did **not** open holdout klines for strategy evaluation.

### Dataset identity

| Field | Verified |
| --- | --- |
| session.json SHA-256 | `c3995cb3483d53b648054fee495c8323a71ad81bbe5791614a29a8c8f8f1cf70` (matches catalog) |
| status / ended_at_utc | `stopped` / `2026-10-31T07:22:59.755340+00:00` |
| quotes / candles in session.json | 2,787,240 / 725 |
| Manifest path | Stage 2.3 SHA-256 CSV |

### Reproducibility fingerprints

Recomputed from `baseline_a/b.jsonl` and `candidate_a/b.jsonl` using `journal_trade_fingerprint` (drops `account_initialized` wall-clock):

- baseline A vs B: **equal**
- candidate A vs B: **equal**
- `unexplained_differences`: **[]**

Matches `research-lab/reports/h1/reproducibility.json`.

### Tests (this stage)

No H1 rule changes. Relevant suites re-run 2026-10-31:

| Suite | Result |
| --- | --- |
| `python -m unittest discover -s research-lab/tests` | **22 OK** (12 lab + 10 H1) |
| `python -m unittest tests.test_quote_tape_collector` | **26 OK** |

Stage 3.2.1 claimed `unittest discover -s tests` → 259 OK. That full project suite was **not** re-executed in 3.2.2 (planning only). Collector and laboratory tests that guard H1 mechanics and public capture still pass.

## 2. What additional eligible data the 30-trade gate needs

Pre-registered gate: **do not rank** unless **each** arm has ≥ 30 **scored completed** trades. Scoring requires entry **and** exit before any sealed holdout, `host_receive`, frozen costs, long-only PaperBroker.

Current eligible tape: **543 one-minute candles ≈ 9.05 hours**.

Observed **eligible** rates (descriptive, n is tiny):

| Arm | Completed scored trades | Rate |
| --- | ---: | ---: |
| Baseline frozen momentum | 2 | ≈ 0.221 / hour |
| H1 confirmed | 1 | ≈ 0.110 / hour |

Shortage: **28** baseline trades and **29** candidate trades.

If the candidate rate stayed at 1 trade per 9.05 hours, additional eligible time ≈ **29 / 0.110 ≈ 263 hours** (~11 days of continuous public tape). Baseline would cross 30 first (~127 additional hours). The **binding** constraint is the candidate.

**Uncertainty:** one confirmed trade is not a stable rate. Actual additional time could be much shorter or much longer. Calendar time is a **planning prior only**. The gate is **observed scored trade counts**, not a scheduled number of hours.

Uncatalogued earlier sessions (`0804c921`, `67027f45`, `f1b1733f`) add on the order of **~3 hours** of 1m candles even if later hashed and bound. They cannot meet the gate. They were **not** strategy-evaluated in this stage.

The frozen 2025 **1h** CSV is the wrong timeframe, synthetic execution, and its period after 2025-09-30 remains a **separate** holdout. It is not eligible H1 evidence.

## 3. Can the existing collection pipeline gather this without frozen changes?

**Yes.** `quote_tape_collector.py` already:

- uses public `bookTicker` + `kline_1m` only (`authenticated: false`)
- never writes `paper_broker.jsonl` or `runtime_status.json`
- does not invent bookTicker `E`
- records reconnect gaps
- stops with `status=stopped` and `ended_at_utc` on Ctrl+C
- rotates quote JSONL at 20 MB

No change to production, broker, frozen `momentum_breakout` parameters, H1 confirmation rule, or `quote_tape_replay.py` is required to **collect**. Catalog bind, SHA-256 manifests, and Stage 2.3-style validation (`quote-tape/reports/audit_stage23_extended.py` pattern) already exist as **read-only** integrity tools.

Operator stop is Ctrl+C; the collector has no duration flag. That is sufficient (Stage 2.3 ran ~12 h that way). Do not modify the collector unless a later stage explicitly authorizes a duration helper.

## 4. Proposed evidence-collection plan (not authorized to start)

### Preserve H1

- Freeze Stage 3.2.1 reports, overlay CSV, journals, and `configs/h1_next_bar_close.json` as the registered snapshot.
- Do not add volume, extra filters, or parameter search.
- Replay engine for evaluation remains lab `ResearchQuoteReplay` + `host_receive`.

### Holdout protection

1. **Seal** original-session holdout: candles with `close_time >= 2026-10-31T04:20:59.999Z` in `20261030T191728Z-4751556f` must never be loaded for H1 scoring, matching, overlays, or rule changes.
2. Do **not** re-split that session. Do **not** “use the last 25% now that we need trades.”
3. **New** public sessions are additional evidence with **new session ids**. Do not append into the sealed holdout directory.
4. Eligible set for the sample-size gate = original **[session start, test_start)** ∪ **new validated sessions in full**, until a **future** confirmation holdout is reserved from data **not yet collected** *after* the 30-trade count is met. That future holdout is not the sealed 182 bars.
5. Journals only under `research-lab/experiments/runs/`. Isolation checks remain.

Taking 25% of *new* captures as holdout *before* the gate would inflate required wall-clock (~263 / 0.75 ≈ 350 h). That is optional conservatism, not required to protect the **already sealed** original holdout.

### Collection procedure (when authorized)

1. Run `python quote_tape_collector.py --output-root quote-tape/sessions` from `F:\crypto-ai-scalper` (public streams only).
2. Prefer multi-day continuous sessions (Ctrl+C for clean `stopped`), same as Stage 2.3.
3. After each stop: confirm `status=stopped`, `ended_at_utc` set, SHA-256 manifest of the session directory, catalog pointer, no write to original capture files.
4. Integrity: quote/candle counts vs `session.json`; reconnect gaps not interpolated; bookTicker `E` still not invented; kline median offset **not** applied to quotes.
5. Bind via `research_lab.datasets.catalog.bind_dataset` after catalog update **when authorized**.
6. H1 evaluation replay (only when authorized): `host_receive`, frozen settings, original holdout mask + new sessions. Two-run fingerprints. Overlay remains primary.

### Stopping conditions

**Stop collection (success path for sample size, still not a profitability claim):**

- Scored completed trades ≥ 30 on **baseline** and ≥ 30 on **H1 candidate**, same rules, after-cost, both legs outside sealed holdout.

**Stop collection (inconclusive / abort):**

- Integrity failure (hash mismatch, unclean stop, capture mutation).
- Operator wall-clock cap: **14 days** of public capture, then reassess sample size **without** unblinding the sealed holdout and **without** changing H1.
- Any request to tune buffer, lookback, costs, or confirmation delay.

**Do not stop because** interim P&L, win rate, or overlay confirmation fraction “looks good” or “looks bad.”

### Data-integrity checks (each new session)

- `session.json` status `stopped` and `ended_at_utc` populated
- Streaming SHA-256 manifest; re-hash verify
- Catalog `session_json_sha256` matches
- Loader issue scan (`load_quote_tape_session`) without rewriting JSONL
- Gaps listed from `events.jsonl`; empty quote windows not filled
- Public endpoints only; `credentials_used=false`

### Explicitly not in this plan

- Launching a capture or an extended H1 replay (needs a later authorization)
- Inspecting or scoring the sealed 182-bar holdout
- Cataloguing short leftover sessions as a substitute for ~10² hours of tape
- Using 1h 2025 synthetic candles as H1 evidence
- Declaring H1 confirmed or rejected on economic grounds before the gate

## 5. Classification carried forward

Stage 3.2.1 economic result remains **inconclusive** (2 vs 1 scored completed trades). Stage 3.2.2 does not replace that result. Additional public tape under the existing collector is the only path identified to approach the pre-registered 30-trade gate without touching frozen components or H1 rules.
