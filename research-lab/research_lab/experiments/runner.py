"""Freeze experiment configs and write isolated run artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from backtester import BacktestSettings
from quote_tape_replay import journal_trade_fingerprint

from research_lab import LAB_ROOT
from research_lab.backtesting.clocks import HOST_RECEIVE
from research_lab.backtesting.engine import run_quote_experiment
from research_lab.backtesting.splits import chronological_split
from research_lab.datasets.catalog import RegisteredDataset, slice_dataset
from research_lab.isolation import assert_isolated_path
from research_lab.metrics.evaluate import experiment_metrics
from research_lab.strategies.registry import make_research_strategy

RUNS_ROOT = LAB_ROOT / "experiments" / "runs"


def new_experiment_id(prefix: str = "s31") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid4().hex[:8]}"


def run_isolated_experiment(
    registered: RegisteredDataset,
    *,
    strategy_name: str,
    decision_clock: str = HOST_RECEIVE,
    split_name: str | None = None,
    experiment_id: str | None = None,
    settings: BacktestSettings | None = None,
    label: str = "quote_tape",
) -> dict[str, object]:
    settings = settings or BacktestSettings(candle_interval_seconds=60)
    experiment_id = experiment_id or new_experiment_id()
    run_dir = assert_isolated_path(RUNS_ROOT / experiment_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    dataset = registered.tape
    split_info = None
    if split_name:
        split = chronological_split(dataset)
        windows = {
            "train": (split.train_start, split.train_end),
            "validation": (split.validation_start, split.validation_end),
            "test": (split.test_start, split.test_end),
        }
        start, end = windows[split_name]
        dataset = slice_dataset(registered, start=start, end=end)
        split_info = asdict(split)
        split_info["selected"] = split_name
    strategy = make_research_strategy(strategy_name)
    result = run_quote_experiment(
        dataset,
        journal_path=run_dir / "journal.jsonl",
        strategy_name=strategy_name,
        decision_clock=decision_clock,
        settings=settings,
        strategy_object=strategy,
    )
    metrics = experiment_metrics(result, initial_equity=settings.initial_equity)
    config = {
        "experiment_id": experiment_id,
        "dataset_id": registered.dataset_id,
        "session_id": registered.session_id,
        "manifest_path": str(registered.manifest_path),
        "session_json_sha256": registered.session_json_sha256,
        "strategy_name": strategy_name,
        "decision_clock": decision_clock,
        "label": label,
        "stale_after_seconds": 3.0,
        "settings": {
            "initial_equity": settings.initial_equity,
            "fee_rate_per_side": settings.fee_rate_per_side,
            "slippage_rate_per_side": settings.slippage_rate_per_side,
            "spread_fraction": settings.spread_fraction,
            "risk_fraction": settings.risk_fraction,
        },
        "split": split_info,
        "kline_offset_applied_to_quotes": False,
        "bookTicker_E_invented": False,
    }
    (run_dir / "config.json").write_text(
        json.dumps(config, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
    )
    fingerprint = journal_trade_fingerprint(run_dir / "journal.jsonl")
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    payload = {
        "config": config,
        "metrics": metrics,
        "result_summary": {
            "trade_count": result.get("trade_count"),
            "stale_candle_decisions": result.get("stale_candle_decisions"),
            "entries_accepted": result.get("entries_accepted"),
            "entries_rejected": result.get("entries_rejected"),
            "equity": result.get("equity"),
            "decision_clock": result.get("decision_clock"),
            "timing_flag_counts": result.get("timing_flag_counts"),
        },
        "journal_fingerprint_sha256": digest,
        "strategy_spec": result.get("strategy_spec"),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return {
        "experiment_id": experiment_id,
        "run_dir": str(run_dir),
        "config": config,
        "metrics": metrics,
        "result": result,
        "journal_fingerprint_sha256": digest,
    }
