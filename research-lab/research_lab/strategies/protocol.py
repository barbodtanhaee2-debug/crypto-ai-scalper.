"""Research strategy interface. Engines consume this; production strategy.py is not edited."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Protocol, Sequence

from strategy import Bar, SimulatedPosition, StrategyDecision


@dataclass(frozen=True)
class StrategySpec:
    name: str
    required_inputs: tuple[str, ...]
    warmup_bars: int
    signal_generation: str
    entry_conditions: str
    exit_conditions: str
    position_constraints: str
    max_holding_bars: int
    indicators: tuple[str, ...]
    parameters: Mapping[str, object] = field(default_factory=dict)
    hypothesis_family: str = "unspecified"
    is_demonstration: bool = False


class ResearchStrategy(Protocol):
    spec: StrategySpec

    def evaluate(
        self,
        bars: Sequence[Bar],
        *,
        decision_time: datetime,
        market_health: Mapping[str, object],
        equity: float,
        position: SimulatedPosition | None = None,
    ) -> StrategyDecision:
        """Use only bars and quotes available at decision_time. Never read future data."""
        ...
