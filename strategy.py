"""Deterministic paper-only BTC/USDT strategy experiments."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Bar:
    """One completed OHLC bar; timestamps must be timezone-aware."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class StrategySettings:
    """Documented defaults for the small, deterministic strategy experiment.

    Bars are expected to be completed one-minute bars. The default lookback is
    20 bars; stops are 0.2%, the risk budget is 0.5% of equity, and a position
    is held for at most 10 bars. Estimated costs assume 0.1% fees per side,
    0.05% slippage per side, plus the observed full bid/ask spread per round
    trip. These are research assumptions, not verified exchange fees.
    """

    lookback_bars: int = 20
    risk_fraction: float = 0.005
    stop_loss_fraction: float = 0.002
    max_holding_bars: int = 10
    fee_rate_per_side: float = 0.001
    slippage_rate_per_side: float = 0.0005
    minimum_net_edge_fraction: float = 0.001
    max_spread_fraction: float = 0.001
    breakout_buffer_fraction: float = 0.0005
    momentum_target_fraction: float = 0.006
    range_max_width_fraction: float = 0.015
    range_entry_fraction: float = 0.2

    def __post_init__(self) -> None:
        if self.lookback_bars < 2:
            raise ValueError("lookback_bars must be at least 2.")
        if self.max_holding_bars < 1:
            raise ValueError("max_holding_bars must be positive.")
        for name in (
            "risk_fraction",
            "stop_loss_fraction",
            "fee_rate_per_side",
            "slippage_rate_per_side",
            "minimum_net_edge_fraction",
            "max_spread_fraction",
            "breakout_buffer_fraction",
            "momentum_target_fraction",
            "range_max_width_fraction",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative.")
        if self.risk_fraction <= 0 or self.risk_fraction >= 1:
            raise ValueError("risk_fraction must be between 0 and 1.")
        if self.stop_loss_fraction <= 0:
            raise ValueError("stop_loss_fraction must be positive.")
        if self.momentum_target_fraction <= 0:
            raise ValueError("momentum_target_fraction must be positive.")
        if not 0 < self.range_entry_fraction < 0.5:
            raise ValueError("range_entry_fraction must be between 0 and 0.5.")


@dataclass(frozen=True)
class MovingAverageCrossoverSettings(StrategySettings):
    """Settings for a close-based, long-only moving-average crossover."""

    lookback_bars: int = 30
    fast_period: int = 10
    slow_period: int = 30

    def __post_init__(self) -> None:
        super().__post_init__()
        if (
            isinstance(self.lookback_bars, bool)
            or not isinstance(self.lookback_bars, int)
            or self.lookback_bars < 2
        ):
            raise ValueError("lookback_bars must be an integer of at least 2.")
        if (
            isinstance(self.fast_period, bool)
            or not isinstance(self.fast_period, int)
            or self.fast_period < 2
        ):
            raise ValueError("fast_period must be an integer of at least 2.")
        if (
            isinstance(self.slow_period, bool)
            or not isinstance(self.slow_period, int)
            or self.slow_period <= self.fast_period
        ):
            raise ValueError("slow_period must be an integer greater than fast_period.")


@dataclass(frozen=True)
class VolatilityTrendSettings(StrategySettings):
    """Settings for a range breakout filtered by average true range."""

    lookback_bars: int = 20
    atr_period: int = 14
    minimum_atr_fraction: float = 0.001
    maximum_atr_fraction: float = 0.02

    def __post_init__(self) -> None:
        super().__post_init__()
        if (
            isinstance(self.lookback_bars, bool)
            or not isinstance(self.lookback_bars, int)
            or self.lookback_bars < 2
        ):
            raise ValueError("lookback_bars must be an integer of at least 2.")
        if (
            isinstance(self.atr_period, bool)
            or not isinstance(self.atr_period, int)
            or self.atr_period < 1
        ):
            raise ValueError("atr_period must be a positive integer.")
        if self.atr_period > self.lookback_bars:
            raise ValueError("atr_period cannot exceed lookback_bars.")
        for name in ("minimum_atr_fraction", "maximum_atr_fraction"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive.")
        if self.minimum_atr_fraction >= self.maximum_atr_fraction:
            raise ValueError(
                "minimum_atr_fraction must be less than maximum_atr_fraction."
            )


@dataclass(frozen=True)
class SimulatedPosition:
    strategy: str
    direction: str
    entry_price: float
    quantity_btc: float
    stop_price: float
    target_price: float
    entry_timestamp: datetime


@dataclass(frozen=True)
class StrategyDecision:
    action: str
    strategy: str
    direction: str | None
    timestamp: datetime
    reason: str
    inputs: Mapping[str, object] = field(default_factory=dict)
    position: SimulatedPosition | None = None


def _aware_utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("Timestamp must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _valid_bar(bar: Bar) -> bool:
    try:
        prices = (bar.open, bar.high, bar.low, bar.close)
        return (
            all(
                not isinstance(price, bool)
                and math.isfinite(price)
                and price > 0
                for price in prices
            )
            and bar.low <= min(bar.open, bar.close)
            and bar.high >= max(bar.open, bar.close)
            and bar.high >= bar.low
        )
    except (AttributeError, TypeError, ValueError):
        return False


class _Strategy:
    name = "base"

    def __init__(self, settings: StrategySettings | None = None) -> None:
        self.settings = settings or StrategySettings()

    @property
    def minimum_history_bars(self) -> int:
        return self.settings.lookback_bars + 1

    def _decision(
        self,
        action: str,
        direction: str | None,
        timestamp: datetime,
        reason: str,
        inputs: Mapping[str, object],
        position: SimulatedPosition | None = None,
    ) -> StrategyDecision:
        decision = StrategyDecision(
            action, self.name, direction, timestamp, reason, dict(inputs), position
        )
        logger.info(
            "strategy_decision strategy=%s direction=%s action=%s "
            "timestamp=%s inputs=%s rejection_reason=%s",
            decision.strategy,
            decision.direction,
            decision.action,
            decision.timestamp.isoformat(),
            decision.inputs,
            reason if action == "no_trade" else None,
        )
        return decision

    def _exit_if_due(
        self,
        bars: Sequence[Bar],
        timestamp: datetime,
        position: SimulatedPosition,
    ) -> StrategyDecision | None:
        if position.strategy != self.name:
            return self._decision(
                "no_trade", None, timestamp, "Position belongs to another strategy.",
                {}, position,
            )
        if _aware_utc(bars[-1].timestamp) <= _aware_utc(position.entry_timestamp):
            return self._decision(
                "no_trade", position.direction, timestamp,
                "Position is active; no completed bar since entry.", {}, position,
            )

        latest = bars[-1]
        direction = position.direction
        stop_hit = (
            latest.low <= position.stop_price
            if direction == "long"
            else latest.high >= position.stop_price
        )
        target_hit = (
            latest.high >= position.target_price
            if direction == "long"
            else latest.low <= position.target_price
        )
        elapsed = max(
            0,
            int(
                (
                    _aware_utc(latest.timestamp)
                    - _aware_utc(position.entry_timestamp)
                ).total_seconds()
                // 60
            ),
        )
        exit_reason = None
        exit_price = latest.close
        if stop_hit:
            exit_reason = "Stop reached; stop takes precedence if stop and target share a bar."
            if direction == "long" and latest.open < position.stop_price:
                exit_price = latest.open
            elif direction == "short" and latest.open > position.stop_price:
                exit_price = latest.open
            else:
                exit_price = position.stop_price
        elif target_hit:
            exit_reason = "Target reached."
            exit_price = position.target_price
        elif elapsed >= self.settings.max_holding_bars:
            exit_reason = "Maximum holding time reached."

        if exit_reason is None:
            return self._decision(
                "no_trade", direction, timestamp, "Position remains open.",
                {"bars_held": elapsed}, position,
            )
        return self._decision(
            "exit", direction, timestamp, exit_reason,
            {"exit_price": exit_price, "bars_held": elapsed},
        )

    def _evaluate(
        self,
        bars: Sequence[Bar],
        *,
        decision_time: datetime,
        market_health: Mapping[str, object],
        equity: float,
        position: SimulatedPosition | None,
    ) -> StrategyDecision:
        try:
            timestamp = _aware_utc(decision_time)
            normalized_timestamps = [_aware_utc(bar.timestamp) for bar in bars]
        except (AttributeError, TypeError, ValueError) as error:
            timestamp = decision_time if isinstance(decision_time, datetime) else datetime.now(timezone.utc)
            return self._decision("no_trade", None, timestamp, str(error), {})

        if any(item > timestamp for item in normalized_timestamps):
            return self._decision(
                "no_trade", None, timestamp,
                "Future-dated bar would introduce look-ahead bias.", {},
            )
        if normalized_timestamps != sorted(normalized_timestamps) or len(set(normalized_timestamps)) != len(normalized_timestamps):
            return self._decision(
                "no_trade", None, timestamp,
                "Bars must be strictly chronological.", {},
            )
        if not bars or any(not _valid_bar(bar) for bar in bars):
            return self._decision(
                "no_trade", None, timestamp, "Missing or invalid OHLC bar.", {},
            )
        if position is not None:
            return self._exit_if_due(bars, timestamp, position)
        if (
            not isinstance(market_health, Mapping)
            or market_health.get("healthy") is not True
            or market_health.get("entry_allowed") is not True
        ):
            return self._decision(
                "no_trade", None, timestamp,
                "Market health does not permit new entries.", {},
            )
        if not math.isfinite(equity) or equity <= 0:
            return self._decision(
                "no_trade", None, timestamp, "Equity must be finite and positive.", {},
            )

        quote = market_health
        try:
            bid = float(quote["bid"])
            ask = float(quote["ask"])
            spread = float(quote["spread"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return self._decision(
                "no_trade", None, timestamp, "Market health is missing a valid quote.", {},
            )
        if (
            any(
                isinstance(value, bool)
                for value in (quote["bid"], quote["ask"], quote["spread"])
            )
            or not all(
                math.isfinite(value) and value > 0
                for value in (bid, ask, spread)
            )
            or ask <= bid
            or not math.isclose(ask - bid, spread, rel_tol=1e-6, abs_tol=1e-8)
        ):
            return self._decision(
                "no_trade", None, timestamp, "Market health quote is invalid.", {},
            )
        mid = (bid + ask) / 2
        spread_fraction = spread / mid
        if spread_fraction > self.settings.max_spread_fraction:
            return self._decision(
                "no_trade", None, timestamp, "Spread exceeds configured limit.",
                {"spread_fraction": spread_fraction},
            )

        if len(bars) < self.minimum_history_bars:
            return self._decision(
                "no_trade", None, timestamp, "Insufficient completed bars.",
                {
                    "bars_available": len(bars),
                    "required_history_bars": self.minimum_history_bars,
                },
            )

        signal = self._entry_signal(bars, timestamp)
        if signal is None:
            return self._decision(
                "no_trade", None, timestamp, "No strategy entry condition.", {},
            )
        direction, signal_price, target_price, inputs = signal
        entry_price = ask if direction == "long" else bid
        expected_move = abs(target_price - entry_price) / entry_price
        estimated_cost = (
            2 * self.settings.fee_rate_per_side
            + 2 * self.settings.slippage_rate_per_side
            + spread_fraction
        )
        if expected_move <= estimated_cost + self.settings.minimum_net_edge_fraction:
            return self._decision(
                "no_trade", direction, timestamp,
                "Expected movement does not exceed estimated round-trip costs and minimum edge.",
                {**inputs, "expected_move_fraction": expected_move,
                 "estimated_cost_fraction": estimated_cost},
            )

        stop_price = entry_price * (
            1 - self.settings.stop_loss_fraction
            if direction == "long"
            else 1 + self.settings.stop_loss_fraction
        )
        risk_budget = equity * self.settings.risk_fraction
        risk_per_btc = abs(entry_price - stop_price) + entry_price * estimated_cost
        max_notional = equity / (1 + estimated_cost)
        quantity = min(
            risk_budget / risk_per_btc,
            max_notional / entry_price,
        )
        if not math.isfinite(quantity) or quantity <= 0:
            return self._decision(
                "no_trade", direction, timestamp,
                "Could not calculate a positive, unleveraged position size.",
                {**inputs, "risk_budget": risk_budget},
            )
        new_position = SimulatedPosition(
            self.name,
            direction,
            entry_price,
            quantity,
            stop_price,
            target_price,
            timestamp,
        )
        return self._decision(
            "entry", direction, timestamp, "Entry conditions and cost filter passed.",
            {**inputs, "expected_move_fraction": expected_move,
             "estimated_cost_fraction": estimated_cost,
             "risk_budget": risk_budget, "risk_per_btc": risk_per_btc,
             "quantity_btc": quantity, "equity": equity},
            new_position,
        )

    def _entry_signal(
        self, bars: Sequence[Bar], timestamp: datetime
    ) -> tuple[str, float, float, Mapping[str, object]] | None:
        raise NotImplementedError

    def evaluate(
        self,
        bars: Sequence[Bar],
        *,
        decision_time: datetime,
        market_health: Mapping[str, object],
        equity: float,
        position: SimulatedPosition | None = None,
    ) -> StrategyDecision:
        """Evaluate completed bars at a timestamp; simulate only, never execute."""
        return self._evaluate(
            bars,
            decision_time=decision_time,
            market_health=market_health,
            equity=equity,
            position=position,
        )


class MomentumBreakoutStrategy(_Strategy):
    """Enter on a close breaking the prior lookback high or low."""

    name = "momentum_breakout"

    def _entry_signal(self, bars, timestamp):
        prior = bars[-self.settings.lookback_bars - 1:-1]
        current = bars[-1]
        prior_high = max(bar.high for bar in prior)
        prior_low = min(bar.low for bar in prior)
        if current.close > prior_high * (1 + self.settings.breakout_buffer_fraction):
            direction = "long"
        elif current.close < prior_low * (1 - self.settings.breakout_buffer_fraction):
            direction = "short"
        else:
            return None
        entry = current.close
        target = entry * (
            1 + self.settings.momentum_target_fraction
            if direction == "long"
            else 1 - self.settings.momentum_target_fraction
        )
        return direction, entry, target, {
            "prior_high": prior_high,
            "prior_low": prior_low,
            "close": current.close,
            "breakout_buffer_fraction": self.settings.breakout_buffer_fraction,
        }


class RangeMeanReversionStrategy(_Strategy):
    """Fade range extremes only while the prior lookback is range-bound."""

    name = "range_mean_reversion"

    def _entry_signal(self, bars, timestamp):
        prior = bars[-self.settings.lookback_bars - 1:-1]
        current = bars[-1]
        range_high = max(bar.high for bar in prior)
        range_low = min(bar.low for bar in prior)
        width = range_high - range_low
        mean_close = sum(bar.close for bar in prior) / len(prior)
        width_fraction = width / mean_close
        if width_fraction > self.settings.range_max_width_fraction or width <= 0:
            return None
        lower_boundary = range_low + width * self.settings.range_entry_fraction
        upper_boundary = range_high - width * self.settings.range_entry_fraction
        midpoint = (range_high + range_low) / 2
        if current.close <= lower_boundary:
            direction = "long"
        elif current.close >= upper_boundary:
            direction = "short"
        else:
            return None
        return direction, current.close, midpoint, {
            "range_high": range_high,
            "range_low": range_low,
            "range_width_fraction": width_fraction,
            "range_midpoint": midpoint,
            "close": current.close,
        }


class MovingAverageCrossoverStrategy(_Strategy):
    """Enter long when the fast close average crosses above the slow average."""

    name = "moving_average_crossover"

    def __init__(
        self,
        settings: MovingAverageCrossoverSettings | None = None,
    ) -> None:
        super().__init__(settings or MovingAverageCrossoverSettings())

    @property
    def minimum_history_bars(self) -> int:
        return max(self.settings.lookback_bars, self.settings.slow_period) + 1

    def _entry_signal(self, bars, timestamp):
        settings = self.settings
        closes = [bar.close for bar in bars]
        fast_period = settings.fast_period
        slow_period = settings.slow_period
        previous_fast = sum(closes[-fast_period - 1:-1]) / fast_period
        previous_slow = sum(closes[-slow_period - 1:-1]) / slow_period
        current_fast = sum(closes[-fast_period:]) / fast_period
        current_slow = sum(closes[-slow_period:]) / slow_period
        if previous_fast > previous_slow or current_fast <= current_slow:
            return None
        current = bars[-1]
        target = current.close * (1 + settings.momentum_target_fraction)
        return "long", current.close, target, {
            "fast_period": fast_period,
            "slow_period": slow_period,
            "previous_fast_average": previous_fast,
            "previous_slow_average": previous_slow,
            "fast_average": current_fast,
            "slow_average": current_slow,
        }


class VolatilityTrendFollowingStrategy(_Strategy):
    """Enter long on an upward range break when ATR is within configured bounds."""

    name = "volatility_trend_following"

    def __init__(
        self,
        settings: VolatilityTrendSettings | None = None,
    ) -> None:
        super().__init__(settings or VolatilityTrendSettings())

    @property
    def minimum_history_bars(self) -> int:
        return max(self.settings.lookback_bars, self.settings.atr_period) + 1

    def _entry_signal(self, bars, timestamp):
        settings = self.settings
        prior = bars[-settings.lookback_bars - 1:-1]
        current = bars[-1]
        previous_close = bars[-settings.atr_period - 1].close
        true_ranges = []
        for bar in bars[-settings.atr_period:]:
            true_ranges.append(
                max(
                    bar.high - bar.low,
                    abs(bar.high - previous_close),
                    abs(bar.low - previous_close),
                )
            )
            previous_close = bar.close
        atr_fraction = (sum(true_ranges) / settings.atr_period) / current.close
        prior_high = max(bar.high for bar in prior)
        prior_mean_close = sum(bar.close for bar in prior) / len(prior)
        if not (
            current.close > prior_high * (1 + settings.breakout_buffer_fraction)
            and current.close > prior_mean_close
            and settings.minimum_atr_fraction
            <= atr_fraction
            <= settings.maximum_atr_fraction
        ):
            return None
        target = current.close * (1 + settings.momentum_target_fraction)
        return "long", current.close, target, {
            "lookback_bars": settings.lookback_bars,
            "atr_period": settings.atr_period,
            "atr_fraction": atr_fraction,
            "minimum_atr_fraction": settings.minimum_atr_fraction,
            "maximum_atr_fraction": settings.maximum_atr_fraction,
            "prior_high": prior_high,
            "prior_mean_close": prior_mean_close,
        }


class StrategyExperiment:
    """Coordinate both strategies while allowing only one simulated position."""

    def __init__(
        self,
        momentum: MomentumBreakoutStrategy | None = None,
        mean_reversion: RangeMeanReversionStrategy | None = None,
    ) -> None:
        self.strategies = (
            momentum or MomentumBreakoutStrategy(),
            mean_reversion or RangeMeanReversionStrategy(),
        )

    def evaluate(
        self,
        bars: Sequence[Bar],
        *,
        decision_time: datetime,
        market_health: Mapping[str, object],
        equity: float,
        position: SimulatedPosition | None = None,
    ) -> dict[str, StrategyDecision]:
        """Evaluate both rules with shared position exclusivity.

        Momentum is evaluated first when flat. Its entry, if any, prevents a
        simultaneous mean-reversion entry. Pass the active position back on
        later calls so the owning strategy can evaluate exits.
        """
        decisions = {}
        active_position = position
        for strategy in self.strategies:
            decision = strategy.evaluate(
                bars,
                decision_time=decision_time,
                market_health=market_health,
                equity=equity,
                position=active_position,
            )
            decisions[strategy.name] = decision
            if active_position is None and decision.action == "entry":
                active_position = decision.position
        return decisions
