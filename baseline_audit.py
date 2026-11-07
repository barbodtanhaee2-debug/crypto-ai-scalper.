"""Read-only diagnostics for the frozen BTCUSDT 1h baseline. No tuning."""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from backtester import (
    Backtester,
    _health,
    _position_for_strategy,
    load_candles_csv,
)
from baseline import (
    FROZEN_BACKTEST_SETTINGS,
    FROZEN_CANDLE_INTERVAL_SECONDS,
    FROZEN_DATASET,
    FROZEN_TRAINING_END,
    FROZEN_VALIDATION_END,
    frozen_baseline_strategies,
)
from paper_broker import PaperBroker, PaperBrokerSettings
from strategy import Bar, MomentumBreakoutStrategy, VolatilityTrendFollowingStrategy

FROZEN_RESULTS = Path("backtest-results/frozen-baseline-btcusdt-1h-2025")


def _trade_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def compare_trade_files(left: Path, right: Path, *, ignore: tuple[str, ...] = ("strategy",)) -> dict:
    a = _trade_rows(left)
    b = _trade_rows(right)
    equal = len(a) == len(b)
    mismatched = []
    if equal:
        for index, (row_a, row_b) in enumerate(zip(a, b)):
            keys = set(row_a) | set(row_b)
            for key in keys:
                if key in ignore:
                    continue
                if row_a.get(key) != row_b.get(key):
                    equal = False
                    mismatched.append({"index": index, "field": key})
                    break
    return {
        "left": str(left),
        "right": str(right),
        "left_count": len(a),
        "right_count": len(b),
        "equal_except_ignored": equal and len(a) == len(b),
        "ignored_fields": list(ignore),
        "mismatches": mismatched[:5],
    }


def cost_filter_thresholds(settings=FROZEN_BACKTEST_SETTINGS) -> dict[str, float]:
    spread = settings.spread_fraction
    estimated_cost = (
        2 * settings.fee_rate_per_side
        + 2 * settings.effective_slippage_rate_per_side
        + spread
    )
    stressed = replace(
        settings,
        fee_rate_per_side=settings.fee_rate_per_side * 2,
        slippage_rate_per_side=settings.slippage_rate_per_side * 2,
    )
    stressed_cost = (
        2 * stressed.fee_rate_per_side
        + 2 * stressed.effective_slippage_rate_per_side
        + spread
    )
    close = 1.0
    ask = close * (1 + spread / 2)
    target = close * (1 + 0.006)
    expected_move = abs(target - ask) / ask
    min_edge = 0.001
    return {
        "spread_fraction": spread,
        "baseline_estimated_round_trip_cost": estimated_cost,
        "baseline_cost_plus_min_edge": estimated_cost + min_edge,
        "stressed_estimated_round_trip_cost": stressed_cost,
        "stressed_cost_plus_min_edge": stressed_cost + min_edge,
        "momentum_target_fraction": 0.006,
        "synthetic_ask_expected_move_from_0_6pct_target": expected_move,
    }


def reconstruct_fill_math(close: float, settings=FROZEN_BACKTEST_SETTINGS) -> dict[str, float]:
    spread = close * settings.spread_fraction
    bid = close - spread / 2
    ask = close + spread / 2
    entry = ask * (1 + settings.effective_slippage_rate_per_side)
    exit_fill = bid * (1 - settings.effective_slippage_rate_per_side)
    return {
        "close": close,
        "synthetic_bid": bid,
        "synthetic_ask": ask,
        "entry_fill": entry,
        "exit_fill_if_same_close": exit_fill,
        "round_trip_price_drag_fraction": (entry - exit_fill) / entry,
    }


def _count_period(
    candles: tuple[Bar, ...],
    strategy,
    *,
    evaluation_start: datetime,
    evaluation_end: datetime,
) -> dict[str, object]:
    settings = FROZEN_BACKTEST_SETTINGS
    strategy_settings = replace(
        strategy.settings,
        fee_rate_per_side=settings.fee_rate_per_side,
        slippage_rate_per_side=settings.effective_slippage_rate_per_side,
        risk_fraction=settings.risk_fraction,
        max_holding_bars=(
            strategy.settings.max_holding_bars
            * (settings.candle_interval_seconds // 60)
        ),
    )
    replay = type(strategy)(strategy_settings)
    from tempfile import TemporaryDirectory

    reasons: Counter[str] = Counter()
    broker_reasons: Counter[str] = Counter()
    long_signals = 0
    short_signals = 0
    entries = 0
    exits = 0
    pending_exits = 0
    with TemporaryDirectory() as directory:
        broker = PaperBroker(
            PaperBrokerSettings(
                initial_cash=settings.initial_equity,
                fee_rate_per_side=settings.fee_rate_per_side,
                slippage_rate_per_side=settings.effective_slippage_rate_per_side,
                risk_fraction=settings.risk_fraction,
                daily_loss_limit_fraction=settings.daily_loss_limit_fraction,
                max_drawdown_fraction=settings.max_drawdown_fraction,
                max_market_data_age_seconds=float(settings.candle_interval_seconds),
                journal_path=Path(directory) / "broker.jsonl",
            )
        )
        start_index = next(
            i for i, c in enumerate(candles) if c.timestamp >= evaluation_start
        )
        for index, candle in enumerate(candles):
            if candle.timestamp > evaluation_end:
                break
            if index < start_index:
                continue
            health = _health(candle, settings.spread_fraction)
            broker.mark_to_market(health, timestamp=candle.timestamp)
            decision = replay.evaluate(
                candles[: index + 1],
                decision_time=candle.timestamp,
                market_health=health,
                equity=broker.equity,
                position=_position_for_strategy(broker),
            )
            reasons[decision.reason] += 1
            if decision.action == "entry" and decision.direction == "long":
                long_signals += 1
            if decision.action == "entry" and decision.direction == "short":
                short_signals += 1
            if decision.action in ("entry", "exit"):
                result = broker.process(decision, market_health=health)
                broker_reasons[result.reason] += 1
                if result.action == "exit_pending":
                    pending_exits += 1
                elif result.accepted and decision.action == "entry":
                    entries += 1
                elif result.accepted and decision.action == "exit":
                    exits += 1
        remaining = broker.position is not None
        pending = broker.exit_pending
    return {
        "strategy": strategy.name,
        "decision_reasons": dict(reasons.most_common()),
        "broker_reasons": dict(broker_reasons.most_common()),
        "long_entry_decisions": long_signals,
        "short_entry_decisions": short_signals,
        "accepted_entries": entries,
        "accepted_exits": exits,
        "exit_pending_count": pending_exits,
        "position_open_at_end": remaining,
        "exit_pending_at_end": pending,
    }


def signal_overlap(
    candles: tuple[Bar, ...],
    *,
    evaluation_start: datetime,
    evaluation_end: datetime,
) -> dict[str, int]:
    momentum = MomentumBreakoutStrategy()
    trend = VolatilityTrendFollowingStrategy()
    counts = Counter()
    for index, candle in enumerate(candles):
        if candle.timestamp < evaluation_start or candle.timestamp > evaluation_end:
            continue
        if index + 1 < max(momentum.minimum_history_bars, trend.minimum_history_bars):
            continue
        window = candles[: index + 1]
        m = momentum._entry_signal(window, candle.timestamp)
        t = trend._entry_signal(window, candle.timestamp)
        m_dir = None if m is None else m[0]
        t_dir = None if t is None else t[0]
        if m_dir == "long" and t_dir == "long":
            counts["both_long"] += 1
        elif m_dir == "long" and t_dir is None:
            counts["momentum_long_only"] += 1
        elif m_dir == "short":
            counts["momentum_short"] += 1
            if t_dir == "long":
                counts["momentum_short_and_trend_long"] += 1
        elif t_dir == "long":
            counts["trend_long_only"] += 1
        else:
            counts["neither"] += 1
    return dict(counts)


def first_trade_trace(candles: tuple[Bar, ...], trade: Mapping[str, str]) -> dict:
    entry_ts = datetime.fromisoformat(trade["entry_timestamp"])
    exit_ts = datetime.fromisoformat(trade["exit_timestamp"])
    by_ts = {bar.timestamp: bar for bar in candles}
    entry_bar = by_ts[entry_ts]
    exit_bar = by_ts[exit_ts]
    reconstructed_entry = reconstruct_fill_math(entry_bar.close)
    reconstructed_exit = reconstruct_fill_math(exit_bar.close)
    qty = float(trade["quantity_btc"])
    entry_fill = float(trade["entry_fill_price"])
    exit_fill = float(trade["exit_fill_price"])
    return {
        "entry_timestamp": trade["entry_timestamp"],
        "exit_timestamp": trade["exit_timestamp"],
        "exit_reason": trade["reason"],
        "entry_ohlc": {
            "open": entry_bar.open,
            "high": entry_bar.high,
            "low": entry_bar.low,
            "close": entry_bar.close,
        },
        "exit_ohlc": {
            "open": exit_bar.open,
            "high": exit_bar.high,
            "low": exit_bar.low,
            "close": exit_bar.close,
        },
        "reported_entry_fill": entry_fill,
        "reconstructed_entry_fill_from_close": reconstructed_entry["entry_fill"],
        "entry_fill_matches_synthetic_ask_plus_slippage": abs(
            entry_fill - reconstructed_entry["entry_fill"]
        )
        < 1e-6,
        "reported_exit_fill": exit_fill,
        "reconstructed_exit_fill_from_close_bid": reconstructed_exit["exit_fill_if_same_close"],
        "exit_fill_matches_synthetic_bid_minus_slippage": abs(
            exit_fill - reconstructed_exit["exit_fill_if_same_close"]
        )
        < 1e-6,
        "reported_net_pnl": float(trade["net_realized_pnl"]),
        "reconstructed_gross_from_fills": (exit_fill - entry_fill) * qty,
        "reported_gross_pnl": float(trade["gross_pnl"]),
        "fees": float(trade["entry_fee"]) + float(trade["exit_fee"]),
        "total_costs": float(trade["total_costs"]),
        "note": (
            "Broker fills use synthetic bid/ask from the candle close, not the "
            "stop or target prices in the strategy decision."
        ),
    }


def reproduce_oos_metrics(candles: tuple[Bar, ...], strategy) -> dict:
    result = Backtester(FROZEN_BACKTEST_SETTINGS).run(
        candles,
        strategy,
        evaluation_start=datetime(2025, 10, 1, 0, 59, 59, 999000, tzinfo=timezone.utc),
        evaluation_end=datetime(2025, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc),
    )
    return {
        "strategy": strategy.name,
        "trade_count": result["overall"]["trade_count"],
        "net_return_fraction": result["overall"]["net_return_fraction"],
        "final_equity": result["overall"]["final_equity"],
        "maximum_drawdown_fraction": result["overall"]["maximum_drawdown_fraction"],
        "risk_rejection_count": result["risk_rejection_count"],
    }


def run_audit(*, results_dir: Path = FROZEN_RESULTS) -> dict:
    dataset = load_candles_csv(
        FROZEN_DATASET / "candles.csv",
        interval_seconds=FROZEN_CANDLE_INTERVAL_SECONDS,
    )
    oos_start = datetime(2025, 10, 1, 0, 59, 59, 999000, tzinfo=timezone.utc)
    saved = json.loads((results_dir / "comparison.json").read_text(encoding="utf-8"))
    momentum = next(
        strategy
        for strategy in frozen_baseline_strategies()
        if strategy.name == "momentum_breakout"
    )
    reproduced = reproduce_oos_metrics(dataset, momentum)
    saved_oos = saved["strategies"]["momentum_breakout"]["out_of_sample"]["baseline_costs"][
        "overall"
    ]
    trade_identity = compare_trade_files(
        results_dir / "momentum_breakout" / "out_of_sample" / "trades.csv",
        results_dir / "volatility_trend_following" / "out_of_sample" / "trades.csv",
    )
    first = _trade_rows(
        results_dir / "momentum_breakout" / "out_of_sample" / "trades.csv"
    )[0]
    oos_end = datetime(2025, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc)
    overlap = signal_overlap(
        dataset,
        evaluation_start=oos_start,
        evaluation_end=oos_end,
    )
    counts = _count_period(
        dataset,
        MomentumBreakoutStrategy(),
        evaluation_start=oos_start,
        evaluation_end=oos_end,
    )
    trend_counts = _count_period(
        dataset,
        VolatilityTrendFollowingStrategy(),
        evaluation_start=oos_start,
        evaluation_end=oos_end,
    )
    report = {
        "profitability_claimed": False,
        "winner_selected": False,
        "verified": {
            "oos_momentum_reproduced": reproduced,
            "saved_oos_momentum": {
                "trade_count": saved_oos["trade_count"],
                "net_return_fraction": saved_oos["net_return_fraction"],
                "final_equity": saved_oos["final_equity"],
                "maximum_drawdown_fraction": saved_oos["maximum_drawdown_fraction"],
            },
            "reproduction_matches_saved": (
                reproduced["trade_count"] == saved_oos["trade_count"]
                and reproduced["net_return_fraction"] == saved_oos["net_return_fraction"]
                and reproduced["final_equity"] == saved_oos["final_equity"]
            ),
            "momentum_vs_trend_oos_trades": trade_identity,
            "cost_filter": cost_filter_thresholds(),
            "first_oos_trade_trace": first_trade_trace(dataset, first),
            "oos_signal_overlap_unconditional": overlap,
            "oos_momentum_decision_census": counts,
            "oos_trend_decision_census": trend_counts,
            "training_end": FROZEN_TRAINING_END.isoformat(),
            "validation_end": FROZEN_VALIDATION_END.isoformat(),
        },
    }
    output = results_dir / "diagnostics.json"
    serializable = json.loads(json.dumps(report, default=str))
    output.write_text(json.dumps(serializable, indent=2) + "\n", encoding="utf-8")
    return serializable


if __name__ == "__main__":
    payload = run_audit()
    print(json.dumps(payload["verified"]["reproduction_matches_saved"]))
    print(json.dumps(payload["verified"]["momentum_vs_trend_oos_trades"], indent=2))
    print(json.dumps(payload["verified"]["cost_filter"], indent=2))
    print(json.dumps(payload["verified"]["oos_signal_overlap_unconditional"], indent=2))
