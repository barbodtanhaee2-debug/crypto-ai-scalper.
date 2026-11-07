"""Read-only adapters around production strategies. strategy.py is not modified."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Mapping, Sequence

from historical_replay import make_strategy
from strategy import Bar, SimulatedPosition, StrategyDecision

from research_lab.strategies.protocol import StrategySpec

_SPECS = {
    "momentum_breakout": StrategySpec(
        name="momentum_breakout",
        required_inputs=("completed_1m_ohlc", "recorded_bid_ask"),
        warmup_bars=21,
        signal_generation="Close breaks prior lookback high/low plus buffer.",
        entry_conditions="Cost filter, risk budget, market health, long-only broker.",
        exit_conditions="Stop, target, or max holding bars.",
        position_constraints="Production StrategySettings; Spot long-only at the broker.",
        max_holding_bars=10,
        indicators=("lookback_high", "lookback_low"),
        parameters={"source": "production MomentumBreakoutStrategy, frozen defaults"},
        hypothesis_family="A.short_term_momentum",
        is_demonstration=False,
    ),
}


class ProductionStrategyAdapter:
    def __init__(self, name: str, inner=None) -> None:
        self._inner = inner or make_strategy(name)
        self.name = self._inner.name
        self.settings = self._inner.settings
        self.spec = _SPECS.get(
            name,
            StrategySpec(
                name=name,
                required_inputs=("completed_1m_ohlc", "recorded_bid_ask"),
                warmup_bars=self._inner.minimum_history_bars,
                signal_generation="Production strategy evaluate().",
                entry_conditions="Production entry filters.",
                exit_conditions="Production exits.",
                position_constraints="Production settings plus long-only broker.",
                max_holding_bars=self._inner.settings.max_holding_bars,
                indicators=(),
                parameters={"source": "production adapter"},
            ),
        )

    @property
    def minimum_history_bars(self) -> int:
        return self._inner.minimum_history_bars

    def evaluate(
        self,
        bars: Sequence[Bar],
        *,
        decision_time: datetime,
        market_health: Mapping[str, object],
        equity: float,
        position: SimulatedPosition | None = None,
    ) -> StrategyDecision:
        return self._inner.evaluate(
            bars,
            decision_time=decision_time,
            market_health=market_health,
            equity=equity,
            position=position,
        )


def adapt_production(name: str, **setting_changes) -> ProductionStrategyAdapter:
    inner = make_strategy(name)
    if setting_changes:
        inner = type(inner)(replace(inner.settings, **setting_changes))
    return ProductionStrategyAdapter(name, inner=inner)
