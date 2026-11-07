"""Demonstration strategy: proves the lab interface. Not a candidate. Not optimized."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Mapping, Sequence

from strategy import Bar, SimulatedPosition, StrategyDecision

from research_lab.strategies.protocol import StrategySpec


@dataclass(frozen=True)
class DemoSettings:
    warmup_bars: int = 3
    max_holding_bars: int = 3
    risk_fraction: float = 0.005
    fee_rate_per_side: float = 0.001
    slippage_rate_per_side: float = 0.0005
    stop_loss_fraction: float = 0.002
    target_fraction: float = 0.004


class DemoCloseFollowStrategy:
    """Long-only: after warmup, enter if the last close is above the previous close.

    Exists only to validate evaluate → PaperBroker wiring. Do not rank or trade live.
    """

    name = "demo_close_follow"

    def __init__(self, settings: DemoSettings | None = None) -> None:
        self.settings = settings or DemoSettings()
        self.spec = StrategySpec(
            name=self.name,
            required_inputs=("completed_1m_ohlc", "recorded_bid_ask"),
            warmup_bars=self.settings.warmup_bars,
            signal_generation="Last close greater than previous close.",
            entry_conditions="Warmup complete, market health allows entry, long-only.",
            exit_conditions="Max holding bars or stop/target on the completed bar.",
            position_constraints="Spot long-only, one position, unleveraged risk fraction.",
            max_holding_bars=self.settings.max_holding_bars,
            indicators=(),
            parameters={
                "warmup_bars": self.settings.warmup_bars,
                "max_holding_bars": self.settings.max_holding_bars,
                "risk_fraction": self.settings.risk_fraction,
            },
            hypothesis_family="A.short_term_momentum",
            is_demonstration=True,
        )

    @property
    def minimum_history_bars(self) -> int:
        return self.settings.warmup_bars

    def evaluate(
        self,
        bars: Sequence[Bar],
        *,
        decision_time: datetime,
        market_health: Mapping[str, object],
        equity: float,
        position: SimulatedPosition | None = None,
    ) -> StrategyDecision:
        if position is not None:
            return self._exit_if_due(bars, decision_time, position)
        if len(bars) < self.settings.warmup_bars:
            return self._decision(
                "no_trade", None, decision_time, "Warmup bars not satisfied.", {}
            )
        if market_health.get("entry_allowed") is not True:
            return self._decision(
                "no_trade",
                None,
                decision_time,
                "Market health does not permit new entries.",
                {"market_health": dict(market_health)},
            )
        previous = bars[-2].close
        last = bars[-1].close
        if not (math.isfinite(previous) and math.isfinite(last) and last > previous):
            return self._decision(
                "no_trade", None, decision_time, "No demonstration entry condition.", {}
            )
        ask = float(market_health["ask"])
        bid = float(market_health["bid"])
        if not (math.isfinite(ask) and ask > 0):
            return self._decision(
                "no_trade", None, decision_time, "Ask is not usable.", {}
            )
        fill = ask * (1 + self.settings.slippage_rate_per_side)
        stop = ask * (1 - self.settings.stop_loss_fraction)
        target = ask * (1 + self.settings.target_fraction)
        if not stop < fill < target:
            return self._decision(
                "no_trade", None, decision_time, "Fill would fall outside stop/target.", {}
            )
        spread = float(market_health.get("spread") or (ask - bid))
        mid = (bid + ask) / 2
        estimated_cost = (
            2 * self.settings.fee_rate_per_side
            + 2 * self.settings.slippage_rate_per_side
            + (spread / mid if mid else 0.0)
        )
        risk_budget = equity * self.settings.risk_fraction
        risk_per_btc = abs(fill - stop) + fill * estimated_cost
        quantity = min(
            risk_budget / risk_per_btc,
            equity / ((1 + estimated_cost) * fill),
        ) * 0.999
        if not math.isfinite(quantity) or quantity <= 0:
            return self._decision(
                "no_trade", None, decision_time, "Could not size demonstration position.", {}
            )
        new_position = SimulatedPosition(
            self.name,
            "long",
            ask,
            quantity,
            stop,
            target,
            decision_time,
        )
        return self._decision(
            "entry",
            "long",
            decision_time,
            "Demonstration close-follow entry.",
            {"previous_close": previous, "close": last, "quantity_btc": quantity},
            new_position,
        )

    def _exit_if_due(
        self,
        bars: Sequence[Bar],
        timestamp: datetime,
        position: SimulatedPosition,
    ) -> StrategyDecision:
        latest = bars[-1]
        stop_hit = latest.low <= position.stop_price
        target_hit = latest.high >= position.target_price
        elapsed = max(
            0,
            int((latest.timestamp - position.entry_timestamp).total_seconds() // 60),
        )
        if stop_hit:
            reason = "Stop reached; stop takes precedence if stop and target share a bar."
            exit_price = position.stop_price
        elif target_hit:
            reason = "Target reached."
            exit_price = position.target_price
        elif elapsed >= self.settings.max_holding_bars:
            reason = "Maximum holding time reached."
            exit_price = latest.close
        else:
            return self._decision(
                "no_trade",
                position.direction,
                timestamp,
                "Demonstration position held.",
                {},
                position,
            )
        return StrategyDecision(
            "exit",
            self.name,
            position.direction,
            timestamp,
            reason,
            {"exit_price": exit_price, "holding_bars": elapsed},
            position,
        )

    def _decision(
        self,
        action: str,
        direction: str | None,
        timestamp: datetime,
        reason: str,
        inputs: Mapping[str, object],
        position: SimulatedPosition | None = None,
    ) -> StrategyDecision:
        return StrategyDecision(
            action, self.name, direction, timestamp, reason, dict(inputs), position
        )


def with_cost_settings(strategy: DemoCloseFollowStrategy, **changes) -> DemoCloseFollowStrategy:
    return DemoCloseFollowStrategy(replace(strategy.settings, **changes))
