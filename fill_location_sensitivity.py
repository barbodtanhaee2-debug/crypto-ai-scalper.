"""Frozen 1h fill-location sensitivity; does not tune or select a winner.

Two paper ledgers share signals, sizing, entries, fees, spread estimates, and
per-side slippage. Only the exit reference changes: synthetic close-derived
bid versus the strategy decision ``exit_price``. Decision-price fills are not
historical executable quotes.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from backtester import Backtester
from baseline import (
    FROZEN_BACKTEST_SETTINGS,
    FROZEN_CANDLE_INTERVAL_SECONDS,
    FROZEN_DATASET,
    FROZEN_TRAINING_END,
    FROZEN_VALIDATION_END,
    frozen_baseline_strategies,
    validate_historical_dataset,
    verify_chronological_splits,
)
from paper_broker import EXIT_FILL_DECISION_PRICE, EXIT_FILL_EXECUTABLE_QUOTE
from strategy import MomentumBreakoutStrategy

FROZEN_BASELINE_RESULTS = Path("backtest-results/frozen-baseline-btcusdt-1h-2025")
DEFAULT_OUTPUT_DIR = Path(
    "backtest-results/fill-location-sensitivity-btcusdt-1h-2025"
)
PERIODS = ("training", "validation", "out_of_sample")
SUMMARY_KEYS = (
    "trade_count",
    "gross_pnl_before_costs",
    "total_fees",
    "modeled_spread_costs_excluding_fees",
    "modeled_slippage_costs_excluding_fees",
    "total_modeled_transaction_costs_including_fees",
    "expectancy_after_costs_usdt",
    "net_return_fraction",
    "final_equity",
    "maximum_drawdown_fraction",
    "win_rate",
    "period_boundary_force_liquidation",
)


def _classification(net_pnl: float) -> str:
    if net_pnl > 0:
        return "win"
    if net_pnl < 0:
        return "loss"
    return "scratch"


def _load_trades(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def _overall_summary(result: dict) -> dict[str, object]:
    overall = result["overall"]
    return {key: overall[key] for key in SUMMARY_KEYS}


def compare_trade_ledgers(
    baseline_trades: list[dict[str, str]],
    sensitivity_trades: list[dict[str, str]],
) -> dict[str, object]:
    """Match trades by entry timestamp; later sizes may diverge after P&L path changes."""
    sensitivity_by_entry = {row["entry_timestamp"]: row for row in sensitivity_trades}
    matched = []
    classification_changes = 0
    fill_changes = 0
    for row in baseline_trades:
        other = sensitivity_by_entry.get(row["entry_timestamp"])
        if other is None:
            continue
        base_net = float(row["net_realized_pnl"])
        sens_net = float(other["net_realized_pnl"])
        base_class = _classification(base_net)
        sens_class = _classification(sens_net)
        changed_class = base_class != sens_class
        if changed_class:
            classification_changes += 1
        exit_changed = row["exit_fill_price"] != other["exit_fill_price"]
        entry_changed = row["entry_fill_price"] != other["entry_fill_price"]
        qty_changed = row["quantity_btc"] != other["quantity_btc"]
        if exit_changed:
            fill_changes += 1
        matched.append(
            {
                "entry_timestamp": row["entry_timestamp"],
                "exit_timestamp_executable_quote": row["exit_timestamp"],
                "exit_timestamp_decision_price": other["exit_timestamp"],
                "reason_executable_quote": row["reason"],
                "reason_decision_price": other["reason"],
                "entry_fill_executable_quote": float(row["entry_fill_price"]),
                "entry_fill_decision_price": float(other["entry_fill_price"]),
                "entry_fill_unchanged": not entry_changed,
                "quantity_unchanged": not qty_changed,
                "exit_fill_executable_quote": float(row["exit_fill_price"]),
                "exit_fill_decision_price": float(other["exit_fill_price"]),
                "exit_fill_changed": exit_changed,
                "net_pnl_executable_quote": base_net,
                "net_pnl_decision_price": sens_net,
                "classification_executable_quote": base_class,
                "classification_decision_price": sens_class,
                "classification_changed": changed_class,
            }
        )
    matched_entries = {row["entry_timestamp"] for row in matched}
    return {
        "executable_quote_trade_count": len(baseline_trades),
        "decision_price_trade_count": len(sensitivity_trades),
        "matched_by_entry_timestamp": len(matched),
        "unmatched_executable_quote_entries": [
            row["entry_timestamp"]
            for row in baseline_trades
            if row["entry_timestamp"] not in matched_entries
        ],
        "unmatched_decision_price_entries": [
            row["entry_timestamp"]
            for row in sensitivity_trades
            if row["entry_timestamp"] not in matched_entries
        ],
        "matched_trades_with_exit_fill_change": fill_changes,
        "classification_changes_among_matches": classification_changes,
        "trades": matched,
    }


def verify_frozen_baseline_reproduction(candles) -> dict[str, object]:
    saved = json.loads(
        (FROZEN_BASELINE_RESULTS / "comparison.json").read_text(encoding="utf-8")
    )
    saved_oos = saved["strategies"]["momentum_breakout"]["out_of_sample"][
        "baseline_costs"
    ]["overall"]
    reproduced = Backtester(FROZEN_BACKTEST_SETTINGS).run(
        candles,
        MomentumBreakoutStrategy(),
        evaluation_start=datetime(2025, 10, 1, 0, 59, 59, 999000, tzinfo=timezone.utc),
        evaluation_end=datetime(2025, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc),
    )["overall"]
    keys = (
        "trade_count",
        "net_return_fraction",
        "final_equity",
        "maximum_drawdown_fraction",
    )
    matches = all(reproduced[key] == saved_oos[key] for key in keys)
    return {
        "saved_results": str(FROZEN_BASELINE_RESULTS),
        "matches_saved_frozen_baseline": matches,
        "compared_keys": list(keys),
        "saved": {key: saved_oos[key] for key in keys},
        "reproduced": {key: reproduced[key] for key in keys},
    }


def _phases(candles):
    training = [c for c in candles if c.timestamp <= FROZEN_TRAINING_END]
    validation = [
        c for c in candles
        if FROZEN_TRAINING_END < c.timestamp <= FROZEN_VALIDATION_END
    ]
    out_of_sample = [c for c in candles if c.timestamp > FROZEN_VALIDATION_END]
    return {
        "training": {
            "candles": training,
            "evaluation_start": None,
            "evaluation_end": training[-1].timestamp,
        },
        "validation": {
            "candles": candles,
            "evaluation_start": validation[0].timestamp,
            "evaluation_end": validation[-1].timestamp,
        },
        "out_of_sample": {
            "candles": candles,
            "evaluation_start": out_of_sample[0].timestamp,
            "evaluation_end": out_of_sample[-1].timestamp,
        },
    }


def _run_ledger(candles, settings, output_root: Path) -> dict[str, dict]:
    phases = _phases(candles)
    ledger: dict[str, dict] = {}
    for strategy in frozen_baseline_strategies():
        ledger[strategy.name] = {}
        for period, phase in phases.items():
            path = output_root / strategy.name / period
            result = Backtester(settings).run(
                phase["candles"],
                strategy,
                evaluation_start=phase["evaluation_start"],
                evaluation_end=phase["evaluation_end"],
                output_dir=path,
            )
            ledger[strategy.name][period] = result
    return ledger


def run_fill_location_sensitivity(
    *,
    dataset_dir: str | Path = FROZEN_DATASET,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, object]:
    root = Path(dataset_dir)
    output = Path(output_dir)
    if output.exists() and any(output.iterdir()):
        raise ValueError(
            f"Output directory {output} already exists and is not empty; "
            "refusing to overwrite."
        )
    dataset = validate_historical_dataset(
        root / "candles.csv",
        interval_seconds=FROZEN_CANDLE_INTERVAL_SECONDS,
        expected_count=8760,
        expected_first=datetime(2025, 1, 1, 0, 59, 59, 999000, tzinfo=timezone.utc),
        expected_last=datetime(2025, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc),
        manifest_path=root / "manifest.json",
    )
    splits = verify_chronological_splits(
        dataset["candles"],
        training_end=FROZEN_TRAINING_END,
        validation_end=FROZEN_VALIDATION_END,
    )
    reproduction = verify_frozen_baseline_reproduction(dataset["candles"])
    if reproduction["matches_saved_frozen_baseline"] is not True:
        raise RuntimeError("Frozen baseline OOS momentum result did not reproduce.")

    quote_dir = output / "executable_quote"
    decision_dir = output / "decision_exit_price"
    quote_settings = FROZEN_BACKTEST_SETTINGS
    decision_settings = replace(
        FROZEN_BACKTEST_SETTINGS,
        exit_fill_mode=EXIT_FILL_DECISION_PRICE,
    )
    quote_ledger = _run_ledger(dataset["candles"], quote_settings, quote_dir)
    decision_ledger = _run_ledger(dataset["candles"], decision_settings, decision_dir)

    summaries = {}
    trade_comparisons = {}
    for strategy_name in quote_ledger:
        summaries[strategy_name] = {}
        trade_comparisons[strategy_name] = {}
        for period in PERIODS:
            summaries[strategy_name][period] = {
                "executable_quote": _overall_summary(quote_ledger[strategy_name][period]),
                "decision_exit_price": _overall_summary(
                    decision_ledger[strategy_name][period]
                ),
            }
            trade_comparisons[strategy_name][period] = compare_trade_ledgers(
                _load_trades(quote_dir / strategy_name / period / "trades.csv"),
                _load_trades(decision_dir / strategy_name / period / "trades.csv"),
            )

    report = {
        "status": "fill_location_sensitivity_complete",
        "profitability_claimed": False,
        "automated_winner_selected": False,
        "experiment": {
            "entry_fill_convention": (
                "Unchanged: synthetic ask from candle close, plus adverse per-side "
                "slippage. Entry fees, half-spread, and slippage estimates unchanged."
            ),
            "executable_quote_exit": (
                "Exit reference is the candle-close synthetic bid; fill = bid * "
                "(1 - slippage). Exit fee is charged on that fill. Half-spread and "
                "slippage estimates use the candle quote and the bid reference."
            ),
            "decision_exit_price_exit": (
                "Stop/target detection is unchanged. Exit reference is the strategy "
                "decision exit_price (stop, target, time-exit close, or gap-through "
                "open). Fill = exit_price * (1 - slippage). Exit fee is charged on "
                "that fill. Half-spread estimates still use the candle's synthetic "
                "quote so spread costs are not dropped to improve the sensitivity."
            ),
            "not_executable_history": (
                "Hourly OHLC cannot establish intrabar order when both stop and "
                "target are touched. Decision-price fills are a modeling contrast, "
                "not exchange prints."
            ),
            "two_x_cost_sensitivity_included": False,
        },
        "frozen_baseline_reproduction": reproduction,
        "chronological_splits": splits,
        "summaries": summaries,
        "trade_comparisons": trade_comparisons,
    }
    output.mkdir(parents=True, exist_ok=True)
    with (output / "fill_location_comparison.json").open(
        "w", encoding="utf-8"
    ) as target:
        json.dump(report, target, indent=2, allow_nan=False)
        target.write("\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", default=str(FROZEN_DATASET))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()
    report = run_fill_location_sensitivity(
        dataset_dir=args.dataset_dir,
        output_dir=args.output_dir,
    )
    print(json.dumps(report["frozen_baseline_reproduction"], indent=2))
    print(json.dumps(report["summaries"], indent=2))
    print("No winner selected. Decision-price fills are not live executable quotes.")


if __name__ == "__main__":
    main()
