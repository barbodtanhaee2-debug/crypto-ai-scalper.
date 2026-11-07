from __future__ import annotations

from research_lab.strategies.adapters import adapt_production
from research_lab.strategies.confirmed_momentum import (
    ConfirmedMomentumBreakoutStrategy,
    DecisionLoggingAdapter,
)
from research_lab.strategies.demo import DemoCloseFollowStrategy

RESEARCH_STRATEGIES = {
    "demo_close_follow": DemoCloseFollowStrategy,
    "h1_confirmed_momentum_breakout": ConfirmedMomentumBreakoutStrategy,
    "momentum_breakout_logged": DecisionLoggingAdapter,
}


def make_research_strategy(name: str):
    if name in RESEARCH_STRATEGIES:
        return RESEARCH_STRATEGIES[name]()
    return adapt_production(name)
