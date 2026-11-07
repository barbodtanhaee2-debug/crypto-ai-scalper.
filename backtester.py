"""Deterministic offline candle replay using the paper strategy and broker APIs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from paper_broker import (
    EXIT_FILL_DECISION_PRICE,
    EXIT_FILL_EXECUTABLE_QUOTE,
    EXIT_FILL_MODES,
    PaperBroker,
    PaperBrokerSettings,
)
from strategy import (
    Bar,
    MomentumBreakoutStrategy,
    MovingAverageCrossoverSettings,
    MovingAverageCrossoverStrategy,
    RangeMeanReversionStrategy,
    SimulatedPosition,
    StrategyDecision,
    VolatilityTrendFollowingStrategy,
    VolatilityTrendSettings,
)

SUPPORTED_STRATEGIES = (
    MomentumBreakoutStrategy,
    RangeMeanReversionStrategy,
    MovingAverageCrossoverStrategy,
    VolatilityTrendFollowingStrategy,
)


@dataclass(frozen=True)
class BacktestSettings:
    """Offline fill assumptions; not verified exchange fees or execution data.

    Input candle timestamps denote completed-candle availability/decision
    times. Execution uses that candle's close as the reference. Latency is
    represented by an additional adverse slippage rate per side equal to
    ``latency_seconds * latency_slippage_per_second``; candles cannot resolve
    sub-candle price movement, so this is a configurable cost proxy rather
    than a claim about actual latency fills.
    """

    initial_equity: float = 100.0
    fee_rate_per_side: float = 0.001
    spread_fraction: float = 0.0002
    slippage_rate_per_side: float = 0.0005
    latency_seconds: float = 0.0
    latency_slippage_per_second: float = 0.00001
    risk_fraction: float = 0.005
    daily_loss_limit_fraction: float = 0.02
    max_drawdown_fraction: float = 0.10
    candle_interval_seconds: int = 60
    exit_fill_mode: str = EXIT_FILL_EXECUTABLE_QUOTE

    def __post_init__(self) -> None:
        for name in (
            "initial_equity",
            "fee_rate_per_side",
            "spread_fraction",
            "slippage_rate_per_side",
            "latency_seconds",
            "latency_slippage_per_second",
            "risk_fraction",
            "daily_loss_limit_fraction",
            "max_drawdown_fraction",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative.")
        if self.initial_equity <= 0:
            raise ValueError("initial_equity must be positive.")
        if self.spread_fraction <= 0:
            raise ValueError("spread_fraction must be positive.")
        if not 0 < self.risk_fraction < 1:
            raise ValueError("risk_fraction must be between 0 and 1.")
        if not 0 < self.daily_loss_limit_fraction < 1:
            raise ValueError("daily_loss_limit_fraction must be between 0 and 1.")
        if not 0 < self.max_drawdown_fraction < 1:
            raise ValueError("max_drawdown_fraction must be between 0 and 1.")
        if (
            isinstance(self.candle_interval_seconds, bool)
            or not isinstance(self.candle_interval_seconds, int)
            or self.candle_interval_seconds < 60
            or self.candle_interval_seconds % 60 != 0
        ):
            raise ValueError(
                "candle_interval_seconds must be a positive whole number of minutes."
            )
        if (
            self.slippage_rate_per_side
            + self.latency_seconds * self.latency_slippage_per_second
            >= 1
        ):
            raise ValueError("Effective per-side slippage must be below 1.")
        if self.exit_fill_mode not in EXIT_FILL_MODES:
            raise ValueError(
                "exit_fill_mode must be executable_quote or decision_exit_price."
            )

    @property
    def effective_slippage_rate_per_side(self) -> float:
        return (
            self.slippage_rate_per_side
            + self.latency_seconds * self.latency_slippage_per_second
        )


def validate_candles(
    candles: Sequence[Bar], *, interval_seconds: int = 60
) -> tuple[Bar, ...]:
    """Reject malformed, duplicate, unordered, or missing-interval candles."""
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, int)
        or interval_seconds < 60
        or interval_seconds % 60 != 0
    ):
        raise ValueError(
            "interval_seconds must be a positive whole number of minutes."
        )
    if not candles:
        raise ValueError("Historical candle dataset is empty.")
    normalized = []
    previous = None
    for index, candle in enumerate(candles):
        if not isinstance(candle, Bar):
            raise ValueError(f"Candle {index} is not a Bar.")
        timestamp = candle.timestamp
        if (
            not isinstance(timestamp, datetime)
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            raise ValueError(f"Candle {index} has a missing or invalid timestamp.")
        timestamp = timestamp.astimezone(timezone.utc)
        values = (candle.open, candle.high, candle.low, candle.close)
        if not all(
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and value > 0
            for value in values
        ):
            raise ValueError(f"Candle {index} has invalid OHLC prices.")
        if (
            candle.low > min(candle.open, candle.close)
            or candle.high < max(candle.open, candle.close)
            or candle.high < candle.low
        ):
            raise ValueError(f"Candle {index} has inconsistent OHLC values.")
        if previous is not None:
            elapsed = (timestamp - previous).total_seconds()
            if elapsed <= 0:
                raise ValueError("Candle timestamps must be strictly chronological and unique.")
            if elapsed != interval_seconds:
                raise ValueError(
                    f"Missing candle interval before {timestamp.isoformat()}; "
                    f"expected {interval_seconds} seconds."
                )
        normalized.append(
            Bar(
                timestamp,
                float(candle.open),
                float(candle.high),
                float(candle.low),
                float(candle.close),
            )
        )
        previous = timestamp
    return tuple(normalized)


def load_candles_csv(
    path: str | Path, *, interval_seconds: int = 60
) -> tuple[Bar, ...]:
    """Load local CSV columns timestamp,open,high,low,close and validate."""
    candles = []
    required = {"timestamp", "open", "high", "low", "close"}
    with Path(path).open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(
                "CSV must contain timestamp,open,high,low,close columns."
            )
        for line_number, row in enumerate(reader, start=2):
            try:
                timestamp = datetime.fromisoformat(row["timestamp"])
                candles.append(
                    Bar(
                        timestamp,
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                    )
                )
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError(
                    f"Invalid candle data on CSV line {line_number}."
                ) from error
    return validate_candles(candles, interval_seconds=interval_seconds)


def _health(candle: Bar, spread_fraction: float) -> dict[str, object]:
    mid = candle.close
    spread = mid * spread_fraction
    return {
        "healthy": True,
        "entry_allowed": True,
        "connection_status": "connected",
        "bid": mid - spread / 2,
        "ask": mid + spread / 2,
        "spread": spread,
        "message_timestamp": candle.timestamp.isoformat(),
        "last_valid_update": candle.timestamp.isoformat(),
        "age_seconds": 0.0,
        "reason": "Synthetic historical candle quote; no live feed used.",
    }


def _position_for_strategy(broker: PaperBroker) -> SimulatedPosition | None:
    position = broker.position
    if position is None:
        return None
    return SimulatedPosition(
        strategy=position.strategy,
        direction=position.direction,
        entry_price=position.entry_price,
        quantity_btc=position.quantity_btc,
        stop_price=position.stop_price,
        target_price=position.target_price,
        entry_timestamp=position.entry_timestamp,
    )


def _metric_set(
    trades: list[dict],
    initial: float,
    final: float,
    *,
    equity_curve: Sequence[float],
    exposure_bars: int,
    evaluated_bars: int,
    position_open_at_period_end: bool,
    open_positions_remaining: int,
    forced_liquidation: bool,
) -> dict:
    trades = [trade for trade in trades if trade.get("direction") == "long"]
    net_results = [trade["net_realized_pnl"] for trade in trades]
    profits = [value for value in net_results if value > 0]
    losses = [-value for value in net_results if value < 0]
    total = len(trades)
    fees = sum(trade["entry_fee"] + trade["exit_fee"] for trade in trades)
    spread_costs = sum(
        trade["entry_spread_cost_estimate"]
        + trade["exit_spread_cost_estimate"]
        for trade in trades
    )
    slippage_costs = sum(
        trade["entry_slippage_cost_estimate"]
        + trade["exit_slippage_cost_estimate"]
        for trade in trades
    )
    total_costs = sum(trade["total_costs"] for trade in trades)
    gross_pnl_before_costs = sum(
        trade["gross_pnl"]
        + trade["entry_spread_cost_estimate"]
        + trade["exit_spread_cost_estimate"]
        + trade["entry_slippage_cost_estimate"]
        + trade["exit_slippage_cost_estimate"]
        for trade in trades
    )
    trade_returns = [
        trade["net_realized_pnl"]
        / (trade["entry_fill_price"] * trade["quantity_btc"])
        for trade in trades
    ]
    max_consecutive_losses = 0
    consecutive_losses = 0
    for result in net_results:
        if result < 0:
            consecutive_losses += 1
            max_consecutive_losses = max(
                max_consecutive_losses,
                consecutive_losses,
            )
        else:
            consecutive_losses = 0

    return {
        "currency": "USDT",
        "initial_equity": initial,
        "final_equity": final,
        "net_return_fraction": (final - initial) / initial if initial else 0.0,
        "maximum_drawdown_fraction": _drawdown(equity_curve),
        "maximum_drawdown_method": (
            "Maximum peak-to-trough percentage decline in candle-close account "
            "equity, including marked-to-market open positions and modeled costs."
        ),
        "profit_factor": sum(profits) / sum(losses) if losses else None,
        "profit_factor_note": (
            "No losing closed trades; profit factor is undefined."
            if not losses else None
        ),
        "win_rate": sum(1 for value in net_results if value > 0) / total
        if total else None,
        "win_rate_note": "No completed trades; win rate is undefined." if not total else None,
        "trade_count": total,
        "gross_pnl_before_costs": gross_pnl_before_costs,
        "average_winning_trade_usdt": (
            sum(profits) / len(profits) if profits else None
        ),
        "average_winning_trade_note": (
            None if profits else "No winning closed trades; average is undefined."
        ),
        "average_losing_trade_usdt": (
            -sum(losses) / len(losses) if losses else None
        ),
        "average_losing_trade_note": (
            None if losses else "No losing closed trades; average is undefined."
        ),
        "average_trade_return_fraction": (
            sum(trade_returns) / len(trade_returns) if trade_returns else None
        ),
        "average_trade_return_note": (
            None if trade_returns else "No completed trades; average is undefined."
        ),
        "expectancy_after_costs_usdt": (
            sum(net_results) / total if total else None
        ),
        "expectancy_note": (
            None if total else "No completed trades; expectancy is undefined."
        ),
        "maximum_consecutive_losses": max_consecutive_losses,
        "scored_bars": evaluated_bars,
        "market_exposure_bars": exposure_bars,
        "market_exposure_percent": (
            100 * exposure_bars / evaluated_bars if evaluated_bars else 0.0
        ),
        "total_fees": fees,
        "modeled_spread_costs_excluding_fees": spread_costs,
        "modeled_slippage_costs_excluding_fees": slippage_costs,
        "total_modeled_transaction_costs_including_fees": total_costs,
        "total_costs": total_costs,
        "position_open_at_period_end": position_open_at_period_end,
        "open_positions_remaining": open_positions_remaining,
        "period_boundary_force_liquidation": forced_liquidation,
    }


def _drawdown(equity_curve: Sequence[float]) -> float:
    peak = 0.0
    maximum = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            maximum = max(maximum, (peak - equity) / peak)
    return maximum


class Backtester:
    """Replay one strategy against validated historical candles."""

    def __init__(self, settings: BacktestSettings | None = None) -> None:
        self.settings = settings or BacktestSettings()

    def run(
        self,
        candles: Sequence[Bar],
        strategy: (
            MomentumBreakoutStrategy
            | RangeMeanReversionStrategy
            | MovingAverageCrossoverStrategy
            | VolatilityTrendFollowingStrategy
        ),
        *,
        evaluation_start: datetime | None = None,
        evaluation_end: datetime | None = None,
        output_dir: str | Path | None = None,
    ) -> dict:
        data = validate_candles(
            candles, interval_seconds=self.settings.candle_interval_seconds
        )
        if not isinstance(strategy, SUPPORTED_STRATEGIES):
            raise TypeError("Backtester accepts one supported strategy instance at a time.")
        start = self._optional_timestamp(evaluation_start, "evaluation_start")
        end = self._optional_timestamp(evaluation_end, "evaluation_end")
        if start is not None and end is not None and start > end:
            raise ValueError("evaluation_start must not follow evaluation_end.")
        start_index = next(
            (
                index for index, candle in enumerate(data)
                if start is None or candle.timestamp >= start
            ),
            len(data),
        )
        end_index = next(
            (
                index for index, candle in enumerate(data)
                if end is not None and candle.timestamp > end
            ),
            len(data),
        )
        if start_index >= end_index:
            raise ValueError("Evaluation period has no candles.")
        scored_start_index = max(
            start_index,
            strategy.minimum_history_bars - 1,
        )
        if scored_start_index >= end_index:
            raise ValueError(
                "Evaluation period has no candles after the strategy warm-up."
            )
        scored_bar_count = end_index - scored_start_index

        strategy_slippage = self.settings.effective_slippage_rate_per_side
        strategy_settings = replace(
            strategy.settings,
            fee_rate_per_side=self.settings.fee_rate_per_side,
            slippage_rate_per_side=strategy_slippage,
            risk_fraction=self.settings.risk_fraction,
            max_holding_bars=(
                strategy.settings.max_holding_bars
                * (self.settings.candle_interval_seconds // 60)
            ),
        )
        replay_strategy = type(strategy)(strategy_settings)
        broker_settings = PaperBrokerSettings(
            initial_cash=self.settings.initial_equity,
            fee_rate_per_side=self.settings.fee_rate_per_side,
            slippage_rate_per_side=strategy_slippage,
            risk_fraction=self.settings.risk_fraction,
            daily_loss_limit_fraction=self.settings.daily_loss_limit_fraction,
            max_drawdown_fraction=self.settings.max_drawdown_fraction,
            max_market_data_age_seconds=max(
                float(self.settings.candle_interval_seconds), 1.0
            ),
            journal_path=Path(tempfile.gettempdir())
            / f"crypto_ai_backtest_{id(self)}_{strategy.name}.jsonl",
            allow_simulated_shorts=False,
            exit_fill_mode=self.settings.exit_fill_mode,
        )

        with tempfile.TemporaryDirectory(prefix="crypto_ai_backtest_") as temporary:
            broker_settings = replace(
                broker_settings,
                journal_path=Path(temporary) / "broker.jsonl",
            )
            broker = PaperBroker(broker_settings)
            processed = []
            evaluated_candles = []
            scored_equity_curve = [broker.equity]
            exposure_bars = 0
            for index, candle in enumerate(data):
                timestamp = candle.timestamp
                if index >= end_index:
                    break
                processed.append(candle)
                if index < start_index:
                    continue
                evaluated_candles.append(candle)
                health = _health(candle, self.settings.spread_fraction)
                broker.mark_to_market(health, timestamp=timestamp)
                if index >= scored_start_index and broker.position is not None:
                    exposure_bars += 1

                decision = replay_strategy.evaluate(
                    data[:index + 1],
                    decision_time=timestamp,
                    market_health=health,
                    equity=broker.equity,
                    position=_position_for_strategy(broker),
                )
                if decision.action in ("entry", "exit"):
                    broker.process(decision, market_health=health)
                if index >= scored_start_index:
                    scored_equity_curve.append(broker.equity)

            if not evaluated_candles:
                raise ValueError("Evaluation period has no candles.")
            if len(scored_equity_curve) != scored_bar_count + 1:
                raise RuntimeError("Backtest did not score the full evaluation period.")
            position_open_before_liquidation = broker.position is not None
            forced_liquidation = False
            if broker.position is not None:
                final_candle = evaluated_candles[-1]
                close_time = final_candle.timestamp + timedelta(milliseconds=1)
                health = _health(final_candle, self.settings.spread_fraction)
                health["age_seconds"] = 0.001
                health["last_valid_update"] = final_candle.timestamp.isoformat()
                health["message_timestamp"] = final_candle.timestamp.isoformat()
                final_decision = StrategyDecision(
                    action="exit",
                    strategy=broker.position.strategy,
                    direction=broker.position.direction,
                    timestamp=close_time,
                    reason="End of evaluation period; forced paper liquidation.",
                    inputs={"exit_price": final_candle.close},
                )
                broker.process(final_decision, market_health=health)
                scored_equity_curve[-1] = broker.equity
                forced_liquidation = True

            journal_events = self._events_from_journal(broker_settings.journal_path)
            trades = [
                event["details"]
                for event in journal_events
                if event.get("event") == "trade_closed"
                and event["details"].get("direction") == "long"
            ]
            initial = self.settings.initial_equity
            final = broker.equity
            period_candles = evaluated_candles
            scored_candles = data[scored_start_index:end_index]
            overall_metrics = _metric_set(
                trades,
                initial,
                final,
                equity_curve=scored_equity_curve,
                exposure_bars=exposure_bars,
                evaluated_bars=scored_bar_count,
                position_open_at_period_end=position_open_before_liquidation,
                open_positions_remaining=int(broker.position is not None),
                forced_liquidation=forced_liquidation,
            )
            results = {
                "strategy": strategy.name,
                "market": "Binance Spot",
                "position_mode": "Long-only",
                "simulation_type": "Historical candle-based paper simulation",
                "account_currency": "USDT",
                "candle_interval_seconds": self.settings.candle_interval_seconds,
                "evaluation_start": start.isoformat() if start else None,
                "evaluation_end": (
                    end.isoformat() if end else period_candles[-1].timestamp.isoformat()
                ),
                "evaluation_candles_start": period_candles[0].timestamp.isoformat(),
                "evaluation_candles_end": period_candles[-1].timestamp.isoformat(),
                "data_start": scored_candles[0].timestamp.isoformat(),
                "data_end": scored_candles[-1].timestamp.isoformat(),
                "sample_period": {
                    "start": scored_candles[0].timestamp.isoformat(),
                    "end": scored_candles[-1].timestamp.isoformat(),
                },
                "candles_available": len(data),
                "candles_processed": len(processed),
                "candles_in_evaluation_period": len(evaluated_candles),
                "candles_scored": len(scored_candles),
                "warmup_candles_excluded_from_performance": (
                    scored_start_index - start_index
                ),
                "minimum_history_bars": strategy.minimum_history_bars,
                "exit_fill_mode": self.settings.exit_fill_mode,
                "assumptions": {
                    "accounting_basis": "USDT; no EUR/USDT conversion is performed.",
                    "execution_reference": (
                        "Completed candle close with a synthetic bid/ask from the "
                        "configured spread; entries execute at ask with configured "
                        "slippage. Default exits use the synthetic bid with "
                        "configured slippage. decision_exit_price exits use the "
                        "strategy decision exit_price as the reference, then apply "
                        "the same per-side slippage and fee formulas. Spread-cost "
                        "estimates still use the candle's synthetic half-spread."
                    ),
                    "exit_fill_mode": self.settings.exit_fill_mode,
                    "intrabar_execution": (
                        "OHLC candles do not provide true intrabar sequencing or "
                        "executable historical quotes; fills are candle-close based "
                        "unless exit_fill_mode is decision_exit_price. When a bar "
                        "touches both stop and target, event order is unknown; the "
                        "strategy selects the stop first. Neither fill convention "
                        "is an exchange execution."
                    ),
                    "same_candle_stop_and_target": (
                        "When both are touched by one candle, the strategy selects "
                        "the stop first. executable_quote fills still use the "
                        "candle-close-derived bid; decision_exit_price fills use "
                        "the selected stop price as the exit reference."
                        if self.settings.exit_fill_mode == EXIT_FILL_DECISION_PRICE
                        else
                        "When both are touched by one candle, the strategy selects "
                        "the stop first; the resulting fill still uses the "
                        "candle-close-derived executable quote."
                    ),
                    "spot_position_mode": "Long-only; unsupported short trades are excluded.",
                    "fee_rate_per_side": self.settings.fee_rate_per_side,
                    "spread_fraction_round_trip": self.settings.spread_fraction,
                    "base_slippage_rate_per_side": self.settings.slippage_rate_per_side,
                    "latency_seconds": self.settings.latency_seconds,
                    "latency_slippage_rate_per_second": self.settings.latency_slippage_per_second,
                    "effective_slippage_rate_per_side": strategy_slippage,
                    "risk_fraction": self.settings.risk_fraction,
                    "daily_loss_limit_fraction": self.settings.daily_loss_limit_fraction,
                    "max_drawdown_fraction": self.settings.max_drawdown_fraction,
                    "maximum_drawdown_method": (
                        "Peak-to-trough percentage decline on scored candle-close "
                        "equity snapshots, including marked-to-market positions."
                    ),
                    "gross_pnl_definition": (
                        "Fill-price gross P&L plus modeled spread and slippage "
                        "estimates; fees, spread, and slippage are then deducted "
                        "once to derive net P&L."
                    ),
                    "transaction_cost_reporting": (
                        "Fees are reported separately. Total modeled transaction "
                        "costs include fees plus spread and slippage estimates; "
                        "spread and slippage component fields exclude fees."
                    ),
                    "market_exposure_definition": (
                        "Scored bars for which a position was open at the beginning "
                        "of the candle, divided by scored bars."
                    ),
                    "forced_liquidation": (
                        "An open position is exited using modeled costs. "
                        "executable_quote uses the last candle-close-derived bid; "
                        "decision_exit_price uses the forced-exit decision "
                        "exit_price (the last close) as the reference, then "
                        "applies the same slippage and fee formulas. Neither is "
                        "a live executable quote."
                    ),
                },
                "strategy_parameters": asdict(strategy_settings),
                "overall": overall_metrics,
                "long": overall_metrics,
                "risk_rejection_count": sum(
                    event.get("event") == "risk_rejection"
                    for event in journal_events
                ),
            }
            if output_dir is not None:
                self._save(Path(output_dir), results, trades)
            return results

    @staticmethod
    def _optional_timestamp(value: datetime | None, name: str) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware.")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _events_from_journal(path: Path) -> list[dict]:
        events = []
        with path.open("r", encoding="utf-8") as source:
            for line in source:
                events.append(json.loads(line))
        return events

    @staticmethod
    def _save(output_dir: Path, results: dict, trades: list[dict]) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        with (output_dir / "results.json").open("w", encoding="utf-8") as target:
            json.dump(results, target, indent=2, allow_nan=False)
            target.write("\n")
        fields = [
            "strategy", "direction", "entry_timestamp", "exit_timestamp",
            "entry_fill_price", "exit_fill_price", "quantity_btc",
            "gross_pnl", "net_realized_pnl", "entry_fee", "exit_fee",
            "entry_spread_cost_estimate", "exit_spread_cost_estimate",
            "entry_slippage_cost_estimate", "exit_slippage_cost_estimate",
            "total_costs", "reason",
        ]
        with (output_dir / "trades.csv").open(
            "w", encoding="utf-8", newline=""
        ) as target:
            writer = csv.DictWriter(target, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(trades)


def run_strategy_comparison(
    candles: Sequence[Bar],
    strategies: Sequence[object],
    settings: BacktestSettings,
    *,
    training_end: datetime,
    validation_end: datetime,
    sensitivity_multiplier: float = 2.0,
    output_dir: str | Path | None = None,
) -> dict:
    """Run frozen strategy candidates on chronological train/validation/test periods.

    Candidate parameters are supplied before evaluation and are never tuned by
    this function. Earlier candles are available only for causal indicator
    warm-up in validation and test runs. All periods use independent paper
    accounts; the out-of-sample results do not select or alter candidates.
    """
    data = validate_candles(
        candles,
        interval_seconds=settings.candle_interval_seconds,
    )
    strategies = tuple(strategies)
    train_cutoff = Backtester._optional_timestamp(training_end, "training_end")
    validation_cutoff = Backtester._optional_timestamp(
        validation_end,
        "validation_end",
    )
    if train_cutoff is None or validation_cutoff is None:
        raise ValueError("training_end and validation_end are required.")
    if train_cutoff >= validation_cutoff:
        raise ValueError("training_end must precede validation_end.")
    if (
        isinstance(sensitivity_multiplier, bool)
        or not isinstance(sensitivity_multiplier, (int, float))
        or not math.isfinite(sensitivity_multiplier)
        or sensitivity_multiplier <= 1
    ):
        raise ValueError("sensitivity_multiplier must be finite and greater than 1.")

    training = [candle for candle in data if candle.timestamp <= train_cutoff]
    validation = [
        candle for candle in data
        if train_cutoff < candle.timestamp <= validation_cutoff
    ]
    out_of_sample = [
        candle for candle in data if candle.timestamp > validation_cutoff
    ]
    if not training or not validation or not out_of_sample:
        raise ValueError(
            "Training and validation cutoffs must leave candles in all three "
            "chronological periods."
        )

    if any(not isinstance(strategy, SUPPORTED_STRATEGIES) for strategy in strategies):
        raise TypeError("Comparison accepts only supported strategy instances.")
    names = [strategy.name for strategy in strategies]
    if not strategies or len(names) != len(set(names)):
        raise ValueError("Provide at least one strategy with unique names.")

    phases = {
        "training": {
            "candles": training,
            "evaluation_start": None,
            "evaluation_end": training[-1].timestamp,
            "partition_start": training[0].timestamp,
            "partition_end": training[-1].timestamp,
        },
        "validation": {
            "candles": data,
            "evaluation_start": validation[0].timestamp,
            "evaluation_end": validation[-1].timestamp,
            "partition_start": validation[0].timestamp,
            "partition_end": validation[-1].timestamp,
        },
        "out_of_sample": {
            "candles": data,
            "evaluation_start": out_of_sample[0].timestamp,
            "evaluation_end": out_of_sample[-1].timestamp,
            "partition_start": out_of_sample[0].timestamp,
            "partition_end": out_of_sample[-1].timestamp,
        },
    }
    output_root = Path(output_dir) if output_dir is not None else None
    results: dict[str, object] = {
        "methodology": {
            "training_end_inclusive": train_cutoff.isoformat(),
            "validation_end_inclusive": validation_cutoff.isoformat(),
            "partitions_are_chronological": True,
            "parameters_frozen_before_validation": True,
            "automated_candidate_selection": False,
            "validation_selection_policy": (
                "Any candidate selection must use a prespecified validation-only "
                "criterion; this workflow does not choose a winner."
            ),
            "out_of_sample_used_for_selection": False,
            "warmup_policy": (
                "Only candles at or before each decision are used; prior-period "
                "candles may warm indicators, and warm-up-only bars are excluded "
                "from scored performance."
            ),
            "periods": {
                name: {
                    "start": phase["partition_start"].isoformat(),
                    "end": phase["partition_end"].isoformat(),
                    "candles": len(phase["candles"])
                    if name == "training" else sum(
                        phase["partition_start"] <= candle.timestamp
                        <= phase["partition_end"]
                        for candle in data
                    ),
                }
                for name, phase in phases.items()
            },
            "exit_fill_mode": settings.exit_fill_mode,
            "higher_cost_sensitivity_multiplier": float(sensitivity_multiplier),
            "higher_cost_policy": (
                "Fee and base slippage rates are multiplied by the sensitivity "
                "factor; spread and latency assumptions remain unchanged."
            ),
            "source": "Caller-supplied historical candles; no downloads performed.",
        },
        "strategies": {},
    }

    for strategy in strategies:
        strategy_results: dict[str, object] = {}
        for phase_name, phase in phases.items():
            path = (
                output_root / strategy.name / phase_name
                if output_root is not None else None
            )
            result = Backtester(settings).run(
                phase["candles"],
                strategy,
                evaluation_start=phase["evaluation_start"],
                evaluation_end=phase["evaluation_end"],
                output_dir=path,
            )
            stress_settings = replace(
                settings,
                fee_rate_per_side=(
                    settings.fee_rate_per_side * sensitivity_multiplier
                ),
                slippage_rate_per_side=(
                    settings.slippage_rate_per_side * sensitivity_multiplier
                ),
            )
            stress_path = (
                path / "higher_cost_sensitivity"
                if path is not None else None
            )
            stress_result = Backtester(stress_settings).run(
                phase["candles"],
                strategy,
                evaluation_start=phase["evaluation_start"],
                evaluation_end=phase["evaluation_end"],
                output_dir=stress_path,
            )
            strategy_results[phase_name] = {
                "baseline_costs": result,
                "higher_cost_sensitivity": stress_result,
            }
        results["strategies"][strategy.name] = strategy_results

    if output_root is not None:
        output_root.mkdir(parents=True, exist_ok=True)
        with (output_root / "comparison.json").open(
            "w",
            encoding="utf-8",
        ) as target:
            json.dump(results, target, indent=2, allow_nan=False)
            target.write("\n")
    return results


def _iso_timestamp(value: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Timestamp must be ISO-8601 with a timezone, e.g. 2026-01-01T00:00:00+00:00."
        ) from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise argparse.ArgumentTypeError("Timestamp must include a timezone.")
    return timestamp


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Local candle CSV path.")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for comparison reports, results, and trades.",
    )
    parser.add_argument(
        "--training-end",
        required=True,
        type=_iso_timestamp,
        help="Inclusive ISO-8601 end of the chronological training period.",
    )
    parser.add_argument(
        "--validation-end",
        required=True,
        type=_iso_timestamp,
        help="Inclusive ISO-8601 end of validation; later candles are out of sample.",
    )
    parser.add_argument("--initial-equity", type=float, default=100.0)
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument("--spread-fraction", type=float, default=0.0002)
    parser.add_argument("--slippage-rate", type=float, default=0.0005)
    parser.add_argument("--latency-seconds", type=float, default=0.0)
    parser.add_argument("--latency-slippage-per-second", type=float, default=0.00001)
    parser.add_argument("--risk-fraction", type=float, default=0.005)
    parser.add_argument("--daily-loss-limit", type=float, default=0.02)
    parser.add_argument("--max-drawdown", type=float, default=0.10)
    parser.add_argument("--candle-interval-seconds", type=int, default=60)
    parser.add_argument("--sensitivity-multiplier", type=float, default=2.0)
    parser.add_argument("--ma-fast-period", type=int, default=10)
    parser.add_argument("--ma-slow-period", type=int, default=30)
    parser.add_argument("--trend-lookback-bars", type=int, default=20)
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--minimum-atr-fraction", type=float, default=0.001)
    parser.add_argument("--maximum-atr-fraction", type=float, default=0.02)
    args = parser.parse_args()

    candles = load_candles_csv(
        args.input, interval_seconds=args.candle_interval_seconds
    )
    settings = BacktestSettings(
        initial_equity=args.initial_equity,
        fee_rate_per_side=args.fee_rate,
        spread_fraction=args.spread_fraction,
        slippage_rate_per_side=args.slippage_rate,
        latency_seconds=args.latency_seconds,
        latency_slippage_per_second=args.latency_slippage_per_second,
        risk_fraction=args.risk_fraction,
        daily_loss_limit_fraction=args.daily_loss_limit,
        max_drawdown_fraction=args.max_drawdown,
        candle_interval_seconds=args.candle_interval_seconds,
    )
    strategies = (
        MomentumBreakoutStrategy(),
        RangeMeanReversionStrategy(),
        MovingAverageCrossoverStrategy(
            MovingAverageCrossoverSettings(
                lookback_bars=args.ma_slow_period,
                fast_period=args.ma_fast_period,
                slow_period=args.ma_slow_period,
            )
        ),
        VolatilityTrendFollowingStrategy(
            VolatilityTrendSettings(
                lookback_bars=args.trend_lookback_bars,
                atr_period=args.atr_period,
                minimum_atr_fraction=args.minimum_atr_fraction,
                maximum_atr_fraction=args.maximum_atr_fraction,
            )
        ),
    )
    try:
        results = run_strategy_comparison(
            candles,
            strategies,
            settings,
            training_end=args.training_end,
            validation_end=args.validation_end,
            sensitivity_multiplier=args.sensitivity_multiplier,
            output_dir=args.output_dir,
        )
    except (TypeError, ValueError, OSError) as error:
        parser.error(str(error))

    for name, periods in results["strategies"].items():
        validation = periods["validation"]["baseline_costs"]["overall"]
        oos = periods["out_of_sample"]["baseline_costs"]["overall"]
        stressed_oos = periods["out_of_sample"]["higher_cost_sensitivity"]["overall"]
        print(
            f"{name}: validation trades={validation['trade_count']}, "
            f"OOS trades={oos['trade_count']}, "
            f"OOS net_return={oos['net_return_fraction']:.4%}, "
            f"OOS higher-cost net_return="
            f"{stressed_oos['net_return_fraction']:.4%}"
        )


if __name__ == "__main__":
    main()
