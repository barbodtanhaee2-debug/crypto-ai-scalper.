#!/usr/bin/env python3
"""Short-fixture B0/H1 infrastructure smoke for Stage 3.2.4 (not a performance study)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = LAB_ROOT.parent
for path in (LAB_ROOT, PROJECT_ROOT):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from backtester import BacktestSettings

from research_lab.backtesting.historical_replay import (
    HistoricalCandleReplay,
    fingerprint_journal,
)
from research_lab.datasets.historical_catalog import (
    HistoricalCandle,
    bind_historical_dataset,
    ensure_default_historical_registration,
    sha256_file,
)
from research_lab.historical.constants import PROVENANCE, RESEARCH_CUTOFF
from research_lab.isolation import assert_isolated_path
from research_lab.strategies.confirmed_momentum import (
    ConfirmedMomentumBreakoutStrategy,
    DecisionLoggingAdapter,
)

RUNS = LAB_ROOT / "experiments" / "runs" / "stage324_smoke"
REPORTS = LAB_ROOT / "reports"


def _fixture_candles() -> tuple[HistoricalCandle, ...]:
    start = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    candles: list[HistoricalCandle] = []
    for minute in range(40):
        if minute < 21:
            close = 100.0
        else:
            close = 101.0
        open_px = close - 0.01
        open_time = start + timedelta(minutes=minute)
        close_time = open_time + timedelta(milliseconds=59_999)
        assert close_time < RESEARCH_CUTOFF
        candles.append(
            HistoricalCandle(
                open_time=open_time,
                close_time=close_time,
                open=open_px,
                high=close + 0.3,
                low=min(open_px, close) - 0.3,
                close=close,
                volume=1.0,
                quote_volume=100.0,
                trades=10,
                provenance=PROVENANCE,
            )
        )
    return tuple(candles)


def _run_arm(name: str, strategy, candles) -> dict[str, object]:
    journal = assert_isolated_path(RUNS / f"{name}.jsonl")
    result = HistoricalCandleReplay(
        candles=candles,
        journal_path=journal,
        strategy_object=strategy,
        settings=BacktestSettings(candle_interval_seconds=60),
        force_close_at_end=True,
    ).run()
    result["journal_fingerprint"] = fingerprint_journal(journal)
    return result


def main() -> int:
    RUNS.mkdir(parents=True, exist_ok=True)
    entry = ensure_default_historical_registration()
    bound = bind_historical_dataset(load_candles=False)
    candles = _fixture_candles()

    b0_a = _run_arm("b0_a", DecisionLoggingAdapter(), candles)
    b0_b = _run_arm("b0_b", DecisionLoggingAdapter(), candles)
    h1_a = _run_arm("h1_a", ConfirmedMomentumBreakoutStrategy(), candles)
    h1_b = _run_arm("h1_b", ConfirmedMomentumBreakoutStrategy(), candles)

    b0_obs = [
        (event["timestamp"], event.get("observation_price_basis"))
        for event in b0_a["events"]
        if event.get("event") == "strategy_decision"
    ]
    h1_obs = [
        (event["timestamp"], event.get("observation_price_basis"))
        for event in h1_a["events"]
        if event.get("event") == "strategy_decision"
    ]

    payload = {
        "stage": "3.2.4",
        "kind": "infrastructure_smoke",
        "not_a_performance_comparison": True,
        "provenance": PROVENANCE,
        "research_cutoff": RESEARCH_CUTOFF.isoformat(),
        "dataset_registration": {
            "dataset_id": bound.dataset_id,
            "manifest_sha256": bound.manifest_sha256,
            "manifest_path": str(bound.manifest_path),
            "catalog_entry_sha_matches": entry["manifest_sha256"] == bound.manifest_sha256,
        },
        "fixture_candles": len(candles),
        "identical_observations": b0_obs == h1_obs,
        "execution_model": b0_a["execution_model"],
        "deterministic_b0": b0_a["journal_fingerprint"] == b0_b["journal_fingerprint"],
        "deterministic_h1": h1_a["journal_fingerprint"] == h1_b["journal_fingerprint"],
        "b0": {
            "candles_processed": b0_a["candles_processed"],
            "trade_count": b0_a["trade_count"],
            "entries_accepted": b0_a["entries_accepted"],
            "fills_executed": b0_a["fills_executed"],
            "synthetic_quotes_only": b0_a["synthetic_quotes_only"],
        },
        "h1": {
            "candles_processed": h1_a["candles_processed"],
            "trade_count": h1_a["trade_count"],
            "entries_accepted": h1_a["entries_accepted"],
            "fills_executed": h1_a["fills_executed"],
            "synthetic_quotes_only": h1_a["synthetic_quotes_only"],
        },
        "assumptions": b0_a["assumptions"],
    }

    # Lock manifest hash into frozen config.
    config_path = LAB_ROOT / "configs" / "stage324_experiment.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["manifest_sha256"] = bound.manifest_sha256
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    out = assert_isolated_path(REPORTS / "stage324_smoke_results.json")
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    ok = (
        payload["identical_observations"]
        and payload["deterministic_b0"]
        and payload["deterministic_h1"]
        and payload["dataset_registration"]["catalog_entry_sha_matches"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
