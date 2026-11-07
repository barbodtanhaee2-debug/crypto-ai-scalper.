"""Frozen four-strategy baseline comparison; no tuning or candidate selection."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from backtester import (
    BacktestSettings,
    load_candles_csv,
    run_strategy_comparison,
    validate_candles,
)
from strategy import (
    MomentumBreakoutStrategy,
    MovingAverageCrossoverSettings,
    MovingAverageCrossoverStrategy,
    RangeMeanReversionStrategy,
    VolatilityTrendFollowingStrategy,
    VolatilityTrendSettings,
)

FROZEN_DATASET = Path("data/btcusdt_1h_2025_20261029T101006Z")
FROZEN_CANDLE_INTERVAL_SECONDS = 3600
FROZEN_TRAINING_END = datetime(2025, 6, 30, 23, 59, 59, 999000, tzinfo=timezone.utc)
FROZEN_VALIDATION_END = datetime(2025, 9, 30, 23, 59, 59, 999000, tzinfo=timezone.utc)
FROZEN_SENSITIVITY_MULTIPLIER = 2.0

FROZEN_BACKTEST_SETTINGS = BacktestSettings(
    initial_equity=100.0,
    fee_rate_per_side=0.001,
    spread_fraction=0.0002,
    slippage_rate_per_side=0.0005,
    latency_seconds=0.0,
    latency_slippage_per_second=0.00001,
    risk_fraction=0.005,
    daily_loss_limit_fraction=0.02,
    max_drawdown_fraction=0.10,
    candle_interval_seconds=FROZEN_CANDLE_INTERVAL_SECONDS,
)

FROZEN_BASELINE_STRATEGY_PARAMETERS = {
    "momentum_breakout": {
        "lookback_bars": 20,
        "risk_fraction": 0.005,
        "stop_loss_fraction": 0.002,
        "max_holding_bars": 10,
        "fee_rate_per_side": 0.001,
        "slippage_rate_per_side": 0.0005,
        "minimum_net_edge_fraction": 0.001,
        "max_spread_fraction": 0.001,
        "breakout_buffer_fraction": 0.0005,
        "momentum_target_fraction": 0.006,
        "range_max_width_fraction": 0.015,
        "range_entry_fraction": 0.2,
    },
    "range_mean_reversion": {
        "lookback_bars": 20,
        "risk_fraction": 0.005,
        "stop_loss_fraction": 0.002,
        "max_holding_bars": 10,
        "fee_rate_per_side": 0.001,
        "slippage_rate_per_side": 0.0005,
        "minimum_net_edge_fraction": 0.001,
        "max_spread_fraction": 0.001,
        "breakout_buffer_fraction": 0.0005,
        "momentum_target_fraction": 0.006,
        "range_max_width_fraction": 0.015,
        "range_entry_fraction": 0.2,
    },
    "moving_average_crossover": {
        "lookback_bars": 30,
        "risk_fraction": 0.005,
        "stop_loss_fraction": 0.002,
        "max_holding_bars": 10,
        "fee_rate_per_side": 0.001,
        "slippage_rate_per_side": 0.0005,
        "minimum_net_edge_fraction": 0.001,
        "max_spread_fraction": 0.001,
        "breakout_buffer_fraction": 0.0005,
        "momentum_target_fraction": 0.006,
        "range_max_width_fraction": 0.015,
        "range_entry_fraction": 0.2,
        "fast_period": 10,
        "slow_period": 30,
    },
    "volatility_trend_following": {
        "lookback_bars": 20,
        "risk_fraction": 0.005,
        "stop_loss_fraction": 0.002,
        "max_holding_bars": 10,
        "fee_rate_per_side": 0.001,
        "slippage_rate_per_side": 0.0005,
        "minimum_net_edge_fraction": 0.001,
        "max_spread_fraction": 0.001,
        "breakout_buffer_fraction": 0.0005,
        "momentum_target_fraction": 0.006,
        "range_max_width_fraction": 0.015,
        "range_entry_fraction": 0.2,
        "atr_period": 14,
        "minimum_atr_fraction": 0.001,
        "maximum_atr_fraction": 0.02,
    },
}


def frozen_baseline_strategies() -> tuple[
    MomentumBreakoutStrategy,
    RangeMeanReversionStrategy,
    MovingAverageCrossoverStrategy,
    VolatilityTrendFollowingStrategy,
]:
    """Return the four locked candidates; parameters are constructor defaults."""
    strategies = (
        MomentumBreakoutStrategy(),
        RangeMeanReversionStrategy(),
        MovingAverageCrossoverStrategy(MovingAverageCrossoverSettings()),
        VolatilityTrendFollowingStrategy(VolatilityTrendSettings()),
    )
    names = [strategy.name for strategy in strategies]
    if names != list(FROZEN_BASELINE_STRATEGY_PARAMETERS):
        raise RuntimeError("Frozen baseline strategy set does not match the lockfile.")
    for strategy in strategies:
        if asdict(strategy.settings) != FROZEN_BASELINE_STRATEGY_PARAMETERS[strategy.name]:
            raise RuntimeError(
                f"{strategy.name} defaults drifted from the frozen baseline lockfile."
            )
    return strategies


def validate_historical_dataset(
    csv_path: str | Path,
    *,
    interval_seconds: int = FROZEN_CANDLE_INTERVAL_SECONDS,
    expected_count: int | None = None,
    expected_first: datetime | None = None,
    expected_last: datetime | None = None,
    manifest_path: str | Path | None = None,
) -> dict[str, object]:
    """Load and validate a local candle CSV before any comparison is run."""
    path = Path(csv_path)
    candles = load_candles_csv(path, interval_seconds=interval_seconds)
    checks: list[str] = [
        "CSV columns timestamp,open,high,low,close are present.",
        "Timestamps are timezone-aware, strictly increasing, and contiguous.",
        "OHLC values are finite, positive, and internally consistent.",
    ]
    if expected_count is not None and len(candles) != expected_count:
        raise ValueError(
            f"Dataset row count {len(candles)} does not match expected {expected_count}."
        )
    if expected_count is not None:
        checks.append(f"Row count matches expected {expected_count}.")
    if expected_first is not None and candles[0].timestamp != expected_first:
        raise ValueError(
            "Dataset first timestamp does not match the declared sample start."
        )
    if expected_last is not None and candles[-1].timestamp != expected_last:
        raise ValueError(
            "Dataset last timestamp does not match the declared sample end."
        )
    if expected_first is not None:
        checks.append("First candle timestamp matches the declared sample start.")
    if expected_last is not None:
        checks.append("Last candle timestamp matches the declared sample end.")
    if manifest_path is not None:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        if manifest.get("valid") is not True:
            raise ValueError("Dataset manifest is not marked valid.")
        if manifest.get("interval_seconds") != interval_seconds:
            raise ValueError("Dataset manifest interval does not match the CSV interval.")
        if manifest.get("row_count") != len(candles):
            raise ValueError("Dataset manifest row_count does not match the CSV.")
        if manifest.get("authenticated") is True or manifest.get("api_keys_used") is True:
            raise ValueError("Dataset manifest reports authenticated access.")
        checks.append("Local manifest agrees with the validated CSV and is unauthenticated.")
    return {
        "valid": True,
        "path": str(path),
        "symbol": "BTCUSDT",
        "market": "Binance Spot",
        "interval_seconds": interval_seconds,
        "row_count": len(candles),
        "first_timestamp": candles[0].timestamp.isoformat(),
        "last_timestamp": candles[-1].timestamp.isoformat(),
        "timestamp_convention": "timezone-aware candle close times",
        "checks": checks,
        "candles": candles,
    }


def verify_chronological_splits(
    candles: Sequence[object],
    *,
    training_end: datetime,
    validation_end: datetime,
) -> dict[str, object]:
    """Confirm train/validation/OOS are chronological and OOS is unused for tuning."""
    data = validate_candles(
        candles,
        interval_seconds=FROZEN_CANDLE_INTERVAL_SECONDS,
    )
    if training_end.tzinfo is None or validation_end.tzinfo is None:
        raise ValueError("Split timestamps must be timezone-aware.")
    train_cutoff = training_end.astimezone(timezone.utc)
    validation_cutoff = validation_end.astimezone(timezone.utc)
    if train_cutoff >= validation_cutoff:
        raise ValueError("training_end must precede validation_end.")
    training = [candle for candle in data if candle.timestamp <= train_cutoff]
    validation = [
        candle for candle in data
        if train_cutoff < candle.timestamp <= validation_cutoff
    ]
    out_of_sample = [candle for candle in data if candle.timestamp > validation_cutoff]
    if not training or not validation or not out_of_sample:
        raise ValueError("Splits must leave candles in all three chronological periods.")
    if training[-1].timestamp > validation[0].timestamp:
        raise ValueError("Training overlaps validation.")
    if validation[-1].timestamp >= out_of_sample[0].timestamp:
        raise ValueError("Validation overlaps out-of-sample.")
    return {
        "partitions_are_chronological": True,
        "training_end_inclusive": train_cutoff.isoformat(),
        "validation_end_inclusive": validation_cutoff.isoformat(),
        "training_candles": len(training),
        "validation_candles": len(validation),
        "out_of_sample_candles": len(out_of_sample),
        "training_start": training[0].timestamp.isoformat(),
        "training_end": training[-1].timestamp.isoformat(),
        "validation_start": validation[0].timestamp.isoformat(),
        "validation_end": validation[-1].timestamp.isoformat(),
        "out_of_sample_start": out_of_sample[0].timestamp.isoformat(),
        "out_of_sample_end": out_of_sample[-1].timestamp.isoformat(),
        "parameters_frozen_before_validation": True,
        "out_of_sample_used_for_tuning": False,
        "automated_candidate_selection": False,
    }


def candidate_summary(comparison: dict[str, object]) -> dict[str, object]:
    """Extract every candidate's metrics without ranking or selecting a winner."""
    strategies = comparison["strategies"]
    if not isinstance(strategies, dict):
        raise ValueError("Comparison is missing strategy results.")
    summary = {}
    for name, periods in strategies.items():
        if not isinstance(periods, dict):
            raise ValueError(f"{name} is missing period results.")
        summary[name] = {}
        for period_name, costs in periods.items():
            if not isinstance(costs, dict):
                continue
            summary[name][period_name] = {}
            for cost_name in ("baseline_costs", "higher_cost_sensitivity"):
                result = costs[cost_name]
                overall = result["overall"]
                summary[name][period_name][cost_name] = {
                    "trade_count": overall["trade_count"],
                    "net_return_fraction": overall["net_return_fraction"],
                    "final_equity": overall["final_equity"],
                    "maximum_drawdown_fraction": overall["maximum_drawdown_fraction"],
                    "win_rate": overall["win_rate"],
                    "profit_factor": overall["profit_factor"],
                    "expectancy_after_costs_usdt": overall["expectancy_after_costs_usdt"],
                    "total_fees": overall["total_fees"],
                    "total_modeled_transaction_costs_including_fees": (
                        overall["total_modeled_transaction_costs_including_fees"]
                    ),
                    "market_exposure_percent": overall["market_exposure_percent"],
                    "period_boundary_force_liquidation": (
                        overall["period_boundary_force_liquidation"]
                    ),
                    "strategy_parameters": result["strategy_parameters"],
                }
    return summary


def write_research_record(
    output_dir: Path,
    *,
    dataset: dict[str, object],
    splits: dict[str, object],
    comparison: dict[str, object],
) -> dict[str, object]:
    dataset_record = {
        key: value for key, value in dataset.items() if key != "candles"
    }
    record = {
        "status": "historical_simulation_complete",
        "profitability_claimed": False,
        "automated_winner_selected": False,
        "verified": {
            "dataset_validation": dataset_record,
            "chronological_splits": splits,
            "frozen_strategy_parameters": FROZEN_BASELINE_STRATEGY_PARAMETERS,
            "frozen_cost_assumptions": asdict(FROZEN_BACKTEST_SETTINGS),
            "candidates": candidate_summary(comparison),
        },
        "unresolved_risks": [
            "Candle-close fills are not executable historical quotes.",
            "Paper runtime exit_pending can leave simulated exposure open during a feed outage; no loss bound is guaranteed until a fresh bid resolves the exit.",
            "Default cost, spread, and slippage rates are research assumptions, not verified Binance fees.",
            "This dataset is 1h BTC/USDT, not the runtime default 1m interval.",
        ],
        "hypotheses": [
            "Cost-sensitive 1h long-only heuristics may not survive round-trip fees and slippage.",
            "Regime routing or additional indicators could change results; they were not evaluated.",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "research_record.json").open("w", encoding="utf-8") as target:
        json.dump(record, target, indent=2, allow_nan=False)
        target.write("\n")
    return record


def run_frozen_baseline(
    *,
    dataset_dir: str | Path = FROZEN_DATASET,
    output_dir: str | Path,
) -> dict[str, object]:
    root = Path(dataset_dir)
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
    output = Path(output_dir)
    comparison = run_strategy_comparison(
        dataset["candles"],
        frozen_baseline_strategies(),
        FROZEN_BACKTEST_SETTINGS,
        training_end=FROZEN_TRAINING_END,
        validation_end=FROZEN_VALIDATION_END,
        sensitivity_multiplier=FROZEN_SENSITIVITY_MULTIPLIER,
        output_dir=output,
    )
    record = write_research_record(
        output,
        dataset=dataset,
        splits=splits,
        comparison=comparison,
    )
    return {"dataset": {k: v for k, v in dataset.items() if k != "candles"},
            "splits": splits,
            "comparison": comparison,
            "research_record": record}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-dir",
        default=str(FROZEN_DATASET),
        help="Directory containing candles.csv and manifest.json.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for frozen comparison reports. Must not be live runtime paths.",
    )
    args = parser.parse_args()
    result = run_frozen_baseline(
        dataset_dir=args.dataset_dir,
        output_dir=args.output_dir,
    )
    print(json.dumps(result["research_record"]["verified"]["candidates"], indent=2))
    print("No winner was selected. Results are historical simulations, not profitability.")


if __name__ == "__main__":
    main()
