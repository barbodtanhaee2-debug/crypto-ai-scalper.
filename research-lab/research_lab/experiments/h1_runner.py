"""Run H1 next-bar confirmation on host_receive tape, holdout excluded from replay."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from backtester import BacktestSettings
from quote_tape_replay import journal_trade_fingerprint

from research_lab import LAB_ROOT
from research_lab.backtesting.clocks import HOST_RECEIVE
from research_lab.backtesting.engine import ResearchQuoteReplay
from research_lab.backtesting.holdout import (
    assert_no_holdout_metrics,
    dataset_excluding_holdout,
    scored_trades,
)
from research_lab.backtesting.splits import chronological_split
from research_lab.datasets.catalog import bind_dataset
from research_lab.experiments.h1_comparison import (
    economic_classification,
    match_full_path_trades,
    opportunity_overlay,
)
from research_lab.isolation import assert_isolated_path
from research_lab.metrics.evaluate import experiment_metrics
from research_lab.strategies.confirmed_momentum import (
    ConfirmedMomentumBreakoutStrategy,
    DecisionLoggingAdapter,
)

DATASET_ID = "stage23_extended_btcusdt_20261030T191728Z-4751556f"
REPORTS = LAB_ROOT / "reports" / "h1"
RUNS = LAB_ROOT / "experiments" / "runs"


def _settings() -> BacktestSettings:
    return BacktestSettings(candle_interval_seconds=60)


def _fingerprint(path: Path) -> str:
    payload = journal_trade_fingerprint(path)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _run_arm(
    dataset,
    *,
    strategy,
    journal_path: Path,
    settings: BacktestSettings,
) -> tuple[dict[str, object], object]:
    replay = ResearchQuoteReplay(
        dataset=dataset,
        journal_path=assert_isolated_path(journal_path),
        strategy_name=getattr(strategy, "name", "unknown"),
        settings=settings,
        strategy_object=strategy,
        decision_clock=HOST_RECEIVE,
        stale_after_seconds=3.0,
    )
    result = replay.run()
    return result, replay._strategy


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def run_h1(*, output_dir: Path | None = None) -> dict[str, object]:
    reports = assert_isolated_path(output_dir or REPORTS)
    reports.mkdir(parents=True, exist_ok=True)
    runs = assert_isolated_path(RUNS / "h1")
    runs.mkdir(parents=True, exist_ok=True)
    registered = bind_dataset(DATASET_ID)
    split = chronological_split(registered.tape)
    dataset = dataset_excluding_holdout(registered.tape, split)
    if any(candle.close_time >= split.test_start for candle in dataset.candles):
        raise ValueError("Holdout candles leaked into the H1 replay dataset.")
    settings = _settings()

    baseline_a, baseline_strategy = _run_arm(
        dataset,
        strategy=DecisionLoggingAdapter(),
        journal_path=runs / "baseline_a.jsonl",
        settings=settings,
    )
    baseline_b, _ = _run_arm(
        dataset,
        strategy=DecisionLoggingAdapter(),
        journal_path=runs / "baseline_b.jsonl",
        settings=settings,
    )
    candidate_a, candidate_strategy = _run_arm(
        dataset,
        strategy=ConfirmedMomentumBreakoutStrategy(),
        journal_path=runs / "candidate_a.jsonl",
        settings=settings,
    )
    candidate_b, _ = _run_arm(
        dataset,
        strategy=ConfirmedMomentumBreakoutStrategy(),
        journal_path=runs / "candidate_b.jsonl",
        settings=settings,
    )

    fp_ba = _fingerprint(runs / "baseline_a.jsonl")
    fp_bb = _fingerprint(runs / "baseline_b.jsonl")
    fp_ca = _fingerprint(runs / "candidate_a.jsonl")
    fp_cb = _fingerprint(runs / "candidate_b.jsonl")
    reproducibility = {
        "baseline_journal_fingerprint_equal": fp_ba == fp_bb,
        "candidate_journal_fingerprint_equal": fp_ca == fp_cb,
        "baseline_trades_equal": baseline_a["trades"] == baseline_b["trades"],
        "candidate_trades_equal": candidate_a["trades"] == candidate_b["trades"],
        "unexplained_differences": [],
    }
    if not reproducibility["baseline_journal_fingerprint_equal"]:
        reproducibility["unexplained_differences"].append("baseline journal fingerprint")
    if not reproducibility["candidate_journal_fingerprint_equal"]:
        reproducibility["unexplained_differences"].append("candidate journal fingerprint")
    if not reproducibility["baseline_trades_equal"]:
        reproducibility["unexplained_differences"].append("baseline trades")
    if not reproducibility["candidate_trades_equal"]:
        reproducibility["unexplained_differences"].append("candidate trades")

    scored_baseline = scored_trades(list(baseline_a.get("trades") or []), split)
    scored_candidate = scored_trades(list(candidate_a.get("trades") or []), split)
    assert_no_holdout_metrics(scored_baseline, split)
    assert_no_holdout_metrics(scored_candidate, split)

    baseline_scored_result = dict(baseline_a)
    baseline_scored_result["trades"] = scored_baseline
    candidate_scored_result = dict(candidate_a)
    candidate_scored_result["trades"] = scored_candidate
    baseline_metrics = experiment_metrics(
        baseline_scored_result, initial_equity=settings.initial_equity
    )
    candidate_metrics = experiment_metrics(
        candidate_scored_result, initial_equity=settings.initial_equity
    )
    overlay = opportunity_overlay(
        getattr(baseline_strategy, "decision_log", []),
        getattr(candidate_strategy, "decision_log", []),
        split,
    )
    full_path = match_full_path_trades(scored_baseline, scored_candidate, split)
    classification = economic_classification(
        len(scored_baseline), len(scored_candidate)
    )

    _write_csv(reports / "opportunity_overlay.csv", overlay)
    _write_csv(reports / "full_path_matched.csv", list(full_path["matched"]))
    _write_csv(reports / "full_path_unmatched_baseline.csv", list(full_path["unmatched_baseline"]))
    _write_csv(
        reports / "full_path_unmatched_candidate.csv",
        list(full_path["unmatched_candidate"]),
    )
    payload = {
        "dataset_id": registered.dataset_id,
        "session_id": registered.session_id,
        "manifest_path": str(registered.manifest_path),
        "session_json_sha256": registered.session_json_sha256,
        "decision_clock": HOST_RECEIVE,
        "split": {
            "train_start": split.train_start.isoformat(),
            "train_end": split.train_end.isoformat(),
            "validation_start": split.validation_start.isoformat(),
            "validation_end": split.validation_end.isoformat(),
            "test_start": split.test_start.isoformat(),
            "test_end": split.test_end.isoformat(),
            "train_candles": split.train_candles,
            "validation_candles": split.validation_candles,
            "test_candles": split.test_candles,
            "replay_candles": len(dataset.candles),
            "holdout_excluded_from_replay": True,
        },
        "baseline_host_receive": {
            "stale_candle_decisions": baseline_a.get("stale_candle_decisions"),
            "trade_count_full_replay_window": baseline_a.get("trade_count"),
            "scored_trade_count": len(scored_baseline),
            "equity_end_of_replay_window": baseline_a.get("equity"),
            "metrics": baseline_metrics,
        },
        "candidate_host_receive": {
            "stale_candle_decisions": candidate_a.get("stale_candle_decisions"),
            "trade_count_full_replay_window": candidate_a.get("trade_count"),
            "scored_trade_count": len(scored_candidate),
            "equity_end_of_replay_window": candidate_a.get("equity"),
            "metrics": candidate_metrics,
        },
        "overlay_summary": {
            "baseline_long_signals": len(overlay),
            "confirmed": sum(1 for row in overlay if row["candidate_outcome"] == "confirmation_pass"),
            "failed_close": sum(1 for row in overlay if row["candidate_outcome"] == "confirmation_fail"),
            "missing_minute": sum(
                1
                for row in overlay
                if row["candidate_outcome"] == "pending_cancelled_missing_minute"
            ),
            "unresolved": sum(
                1
                for row in overlay
                if row["candidate_outcome"] == "pending_unresolved_end_of_window"
            ),
        },
        "full_path_summary": {
            "matched": len(full_path["matched"]),
            "unmatched_baseline": len(full_path["unmatched_baseline"]),
            "unmatched_candidate": len(full_path["unmatched_candidate"]),
        },
        "reproducibility": reproducibility,
        "classification": classification,
        "kline_event_stage24_counts_not_used": True,
    }
    (reports / "h1_results.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (reports / "reproducibility.json").write_text(
        json.dumps(reproducibility, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def write_markdown_report(payload: dict[str, object], path: Path | None = None) -> Path:
    output = assert_isolated_path(path or LAB_ROOT / "reports" / "STAGE3.2.1_H1_REPORT.md")
    overlay = payload["overlay_summary"]
    classification = payload["classification"]
    base = payload["baseline_host_receive"]
    cand = payload["candidate_host_receive"]
    split = payload["split"]
    text = f"""# Stage 3.2.1 — H1 next-bar close confirmation

Paper-only. Frozen `momentum_breakout` parameters were not changed. Production files were not modified. The chronological holdout (final 25% by candle close) was **excluded from replay** and was **not scored**.

This is **not** a profitability result.

## Dataset

- dataset_id: `{payload["dataset_id"]}`
- session: `{payload["session_id"]}`
- manifest: `{payload["manifest_path"]}`
- session.json SHA-256: `{payload["session_json_sha256"]}`
- clock: `host_receive` for both arms
- Stage 2.4 kline_event trade counts were **not** used

## Windows

| Split | Candles |
| --- | ---: |
| Train | {split["train_candles"]} |
| Validation | {split["validation_candles"]} |
| Holdout (unscored, not replayed) | {split["test_candles"]} |
| Replay window (session start through validation end) | {split["replay_candles"]} |

Holdout starts at `{split["test_start"]}`.

## Independent host_receive baseline

Frozen `momentum_breakout` on recorded bid/ask, `host_receive` freshness.

| Metric | Value |
| --- | --- |
| Stale candle decisions | {base["stale_candle_decisions"]} |
| Completed trades in replay window | {base["trade_count_full_replay_window"]} |
| Completed trades scored (train+validation, both legs before holdout) | {base["scored_trade_count"]} |
| After-cost net P&L (scored only) | {base["metrics"]["net_profit_and_loss"]} |
| Ranking allowed | {base["metrics"]["ranking_allowed"]} |

## Candidate (next-bar close confirmation)

| Metric | Value |
| --- | --- |
| Stale candle decisions | {cand["stale_candle_decisions"]} |
| Completed trades in replay window | {cand["trade_count_full_replay_window"]} |
| Completed trades scored | {cand["scored_trade_count"]} |
| After-cost net P&L (scored only) | {cand["metrics"]["net_profit_and_loss"]} |
| Ranking allowed | {cand["metrics"]["ranking_allowed"]} |

## Opportunity overlay (primary)

Baseline long **signals** (evaluate→entry), not occupancy-matched fills.

| Outcome | Count |
| --- | ---: |
| Baseline long signals | {overlay["baseline_long_signals"]} |
| Confirmed (t+1 close held breakout) | {overlay["confirmed"]} |
| Failed confirmation | {overlay["failed_close"]} |
| Missing next-minute candle | {overlay["missing_minute"]} |
| Unresolved at window end | {overlay["unresolved"]} |

CSV: `research-lab/reports/h1/opportunity_overlay.csv`

## Full-path replay (secondary)

Occupancy can make trade sets incomparable. Unmatched trades are **not** forced.

| Set | Count |
| --- | ---: |
| Matched (entry within 120 s, same direction) | {payload["full_path_summary"]["matched"]} |
| Unmatched baseline | {payload["full_path_summary"]["unmatched_baseline"]} |
| Unmatched candidate | {payload["full_path_summary"]["unmatched_candidate"]} |

## Reproducibility

Two runs per arm. Journal fingerprints drop `account_initialized` wall-clock.

`{json.dumps(payload["reproducibility"])}`

## Economic classification

**{classification["economic_result"]}** — {classification["reason"]}

Baseline scored trades: {classification["baseline_completed_scored_trades"]}. Candidate scored trades: {classification["candidate_completed_scored_trades"]}. Gate: {classification["min_closed_trades_to_rank"]}.
"""
    output.write_text(text, encoding="utf-8")
    return output
