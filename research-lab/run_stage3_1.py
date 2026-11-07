"""Stage 3.1 validation: catalog bind, stale investigation, demo + clock comparison."""

from __future__ import annotations

import json
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parent
PROJECT = LAB.parent
for path in (LAB, PROJECT):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from backtester import BacktestSettings
from quote_tape_replay import QuoteTapeReplay
from research_lab.backtesting.clocks import HOST_RECEIVE, KLINE_EVENT
from research_lab.backtesting.engine import ResearchQuoteReplay
from research_lab.datasets.catalog import bind_dataset
from research_lab.experiments.runner import run_isolated_experiment
from research_lab.experiments.stale_investigation import (
    analyze_stale_decisions,
    write_stale_report,
)
from research_lab.metrics.evaluate import experiment_metrics

DATASET_ID = "stage23_extended_btcusdt_20261030T191728Z-4751556f"
REPORTS = LAB / "reports"


def _md_stale(analysis: dict) -> str:
    return f"""# Stage 3.1 — Stale-decision investigation

Paper-only. Production freshness remains **3.0 s**. Original capture files were not modified.

## Dataset

- dataset_id: `{analysis['session_id']}` (catalog bind of Stage 2.3 extended session)
- candles: {analysis['candles']}
- quotes: {analysis['quotes']}
- reconnect gaps: {analysis['reconnect_gaps']}
- median(kline E − host receive): {analysis['median_kline_e_minus_host_receive']}

## Counts

| Clock | Stale candle decisions |
| --- | ---: |
| kline_event (Stage 2.4 policy) | {analysis['stale_on_kline_event_clock']} |
| host_receive (lab correction) | {analysis['stale_on_host_receive_clock']} |

Stage 2.4 reported **69** stale decisions on this session. This investigation's kline_event count is **{analysis['stale_on_kline_event_clock']}**.

| Classification | Count |
| --- | ---: |
| Negative age (E before last quote host time) | {analysis['negative_age_mixed_clock']} |
| Age > 3 s versus kline E | {analysis['age_exceeds_3s_on_kline_e']} |
| Stale on E but fresh on host clock | {analysis['stale_kline_that_are_fresh_on_host_clock']} |
| Stale on E and still stale on host clock | {analysis['stale_kline_that_remain_stale_on_host_clock']} |
| Candles with E before host receive | {analysis['candles_with_kline_e_before_host_receive']} |

Stale age versus kline E: `{analysis['stale_age_vs_kline_e']}`

Host-clock age on those same bars: `{analysis['host_age_on_those_same_bars']}`

## Root cause

{analysis['root_cause']}

## Correction (lab only)

Default research clock is **host_receive**. Do not widen the 3 s threshold. Do not apply the kline median offset to quotes. Do not invent bookTicker E. Do not drop stale decisions.

Residual host-clock stale bars are treated as true missing/delayed quotes (including the preserved reconnect gap), not as a clock artefact.
"""


def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    registered = bind_dataset(DATASET_ID)
    analysis = analyze_stale_decisions(registered.tape)
    write_stale_report(analysis, REPORTS / "stale_decision_analysis.json")
    (REPORTS / "STAGE3.1_STALE_DECISIONS.md").write_text(
        _md_stale(analysis), encoding="utf-8"
    )

    settings = BacktestSettings(candle_interval_seconds=60)
    frozen = QuoteTapeReplay(
        dataset=registered.tape,
        journal_path=LAB / "experiments" / "runs" / "stage31_frozen_quote.jsonl",
        strategy_name="momentum_breakout",
        settings=settings,
        stale_after_seconds=3.0,
    ).run()
    lab_kline = ResearchQuoteReplay(
        dataset=registered.tape,
        journal_path=LAB / "experiments" / "runs" / "stage31_lab_kline.jsonl",
        strategy_name="momentum_breakout",
        settings=settings,
        decision_clock=KLINE_EVENT,
    ).run()
    lab_host = ResearchQuoteReplay(
        dataset=registered.tape,
        journal_path=LAB / "experiments" / "runs" / "stage31_lab_host.jsonl",
        strategy_name="momentum_breakout",
        settings=settings,
        decision_clock=HOST_RECEIVE,
    ).run()
    demo = run_isolated_experiment(
        registered,
        strategy_name="demo_close_follow",
        decision_clock=HOST_RECEIVE,
        experiment_id="stage31-demo-host",
        settings=settings,
        label="demonstration_only",
    )
    comparison = {
        "dataset_id": registered.dataset_id,
        "manifest_path": str(registered.manifest_path),
        "session_json_sha256": registered.session_json_sha256,
        "missing_candle_intervals": registered.missing_candle_intervals,
        "reconnect_gaps": len(registered.reconnect_gaps),
        "stale_investigation": {
            "kline_event": analysis["stale_on_kline_event_clock"],
            "host_receive": analysis["stale_on_host_receive_clock"],
            "negative_age_mixed_clock": analysis["negative_age_mixed_clock"],
            "age_exceeds_3s_on_kline_e": analysis["age_exceeds_3s_on_kline_e"],
        },
        "momentum_breakout_frozen_quote_tape_replay": {
            "stale_candle_decisions": frozen["stale_candle_decisions"],
            "trade_count": frozen["trade_count"],
            "equity": frozen["equity"],
        },
        "momentum_breakout_lab_kline_event": {
            "stale_candle_decisions": lab_kline["stale_candle_decisions"],
            "trade_count": lab_kline["trade_count"],
            "equity": lab_kline["equity"],
            "matches_frozen_stale": lab_kline["stale_candle_decisions"]
            == frozen["stale_candle_decisions"],
        },
        "momentum_breakout_lab_host_receive": {
            "stale_candle_decisions": lab_host["stale_candle_decisions"],
            "trade_count": lab_host["trade_count"],
            "equity": lab_host["equity"],
            "metrics": experiment_metrics(lab_host),
        },
        "demo_close_follow": {
            "experiment_id": demo["experiment_id"],
            "metrics": demo["metrics"],
            "stale_candle_decisions": demo["result"]["stale_candle_decisions"],
        },
    }
    (REPORTS / "stage31_validation.json").write_text(
        json.dumps(comparison, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps(comparison, indent=2, default=str))


if __name__ == "__main__":
    main()
