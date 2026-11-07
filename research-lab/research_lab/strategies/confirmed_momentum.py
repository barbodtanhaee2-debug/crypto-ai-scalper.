"""H1: next-bar close confirmation of frozen momentum_breakout. Lab-only."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from strategy import Bar, MomentumBreakoutStrategy, SimulatedPosition, StrategyDecision, StrategySettings

from research_lab.strategies.adapters import ProductionStrategyAdapter, adapt_production
from research_lab.strategies.protocol import StrategySpec

NAME = "h1_confirmed_momentum_breakout"
NEXT_MINUTE_MIN_SECONDS = 55.0
NEXT_MINUTE_MAX_SECONDS = 65.0


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def is_next_minute(previous_close: datetime, current_close: datetime) -> bool:
    delta = (_utc(current_close) - _utc(previous_close)).total_seconds()
    return NEXT_MINUTE_MIN_SECONDS <= delta <= NEXT_MINUTE_MAX_SECONDS


def quote_available_at_decision(
    market_health: Mapping[str, object], decision_time: datetime
) -> bool:
    raw = market_health.get("host_received_at_utc") or market_health.get(
        "last_valid_update"
    )
    if not isinstance(raw, str):
        return False
    try:
        received = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if received.tzinfo is None or received.utcoffset() is None:
        return False
    return received.astimezone(timezone.utc) <= _utc(decision_time)


def _relabel(
    decision: StrategyDecision,
    name: str,
    *,
    position: SimulatedPosition | None = None,
    action: str | None = None,
    reason: str | None = None,
    extra_inputs: Mapping[str, object] | None = None,
) -> StrategyDecision:
    inputs = dict(decision.inputs)
    if extra_inputs:
        inputs.update(extra_inputs)
    pos = position if position is not None else decision.position
    if pos is not None and pos.strategy != name:
        pos = SimulatedPosition(
            name,
            pos.direction,
            pos.entry_price,
            pos.quantity_btc,
            pos.stop_price,
            pos.target_price,
            pos.entry_timestamp,
        )
    return StrategyDecision(
        action if action is not None else decision.action,
        name,
        decision.direction,
        decision.timestamp,
        reason if reason is not None else decision.reason,
        inputs,
        pos,
    )


def _inner_position(position: SimulatedPosition, inner_name: str) -> SimulatedPosition:
    return SimulatedPosition(
        inner_name,
        position.direction,
        position.entry_price,
        position.quantity_btc,
        position.stop_price,
        position.target_price,
        position.entry_timestamp,
    )


@dataclass
class PendingBreakout:
    signal_bar_close: datetime
    prior_high: float
    buffer: float
    signal_decision_time: datetime


class ConfirmedMomentumBreakoutStrategy:
    """Delay frozen long breakout until the next completed close holds the breakout."""

    name = NAME

    def __init__(self, settings: StrategySettings | None = None) -> None:
        if settings is None:
            inner = adapt_production("momentum_breakout")
        else:
            inner = ProductionStrategyAdapter(
                "momentum_breakout",
                inner=MomentumBreakoutStrategy(settings),
            )
        self._inner = inner
        self.settings = inner.settings
        self.pending: PendingBreakout | None = None
        self.decision_log: list[dict[str, object]] = []
        self.spec = StrategySpec(
            name=self.name,
            required_inputs=("completed_1m_ohlc", "recorded_bid_ask"),
            warmup_bars=inner.minimum_history_bars,
            signal_generation=(
                "Frozen momentum_breakout long at bar t; enter only if bar t+1 "
                "close remains above prior_high(t) * (1 + breakout_buffer)."
            ),
            entry_conditions=(
                "Next-minute close confirmation, then frozen cost/size filters on t+1 ask."
            ),
            exit_conditions="Frozen stop, target, or max holding from actual entry time.",
            position_constraints="Spot long-only; one position; frozen risk fraction.",
            max_holding_bars=self.settings.max_holding_bars,
            indicators=("lookback_high", "lookback_low"),
            parameters={
                "inner": "momentum_breakout frozen defaults",
                "confirmation": "next_bar_close",
                "breakout_buffer_fraction": self.settings.breakout_buffer_fraction,
            },
            hypothesis_family="A.short_term_momentum",
            is_demonstration=False,
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
        audit = self._audit(bars, decision_time, market_health)
        if position is not None:
            inner_decision = self._inner.evaluate(
                bars,
                decision_time=decision_time,
                market_health=market_health,
                equity=equity,
                position=_inner_position(position, self._inner.name),
            )
            decision = _relabel(inner_decision, self.name, position=position)
            self._log(decision, audit, event="exit_path")
            return decision

        latest = bars[-1] if bars else None
        if self.pending is not None and latest is not None:
            pending = self.pending
            if is_next_minute(pending.signal_bar_close, latest.timestamp):
                level = pending.prior_high * (1 + pending.buffer)
                if latest.close > level:
                    decision = self._enter_long(
                        bars,
                        decision_time=decision_time,
                        market_health=market_health,
                        equity=equity,
                        prior_high=pending.prior_high,
                        signal_bar_close=pending.signal_bar_close,
                    )
                    self.pending = None
                    self._log(decision, audit, event="confirmation_pass")
                    return decision
                self.pending = None
                decision = StrategyDecision(
                    "no_trade",
                    self.name,
                    "long",
                    decision_time,
                    "H1 next-bar close failed to hold the breakout.",
                    {"breakout_level": level, "confirm_close": latest.close},
                    None,
                )
                self._log(decision, audit, event="confirmation_fail")
            else:
                self.pending = None
                cancelled = StrategyDecision(
                    "no_trade",
                    self.name,
                    "long",
                    decision_time,
                    "H1 pending signal cancelled; next-minute candle missing.",
                    {
                        "pending_close": pending.signal_bar_close.isoformat(),
                        "current_close": latest.timestamp.isoformat(),
                    },
                    None,
                )
                self._log(cancelled, audit, event="pending_cancelled_missing_minute")

        inner_decision = self._inner.evaluate(
            bars,
            decision_time=decision_time,
            market_health=market_health,
            equity=equity,
            position=None,
        )
        if inner_decision.action == "entry" and inner_decision.direction == "long":
            prior_high = float(inner_decision.inputs.get("prior_high") or 0.0)
            assert latest is not None
            self.pending = PendingBreakout(
                signal_bar_close=latest.timestamp,
                prior_high=prior_high,
                buffer=float(self.settings.breakout_buffer_fraction),
                signal_decision_time=decision_time,
            )
            decision = _relabel(
                inner_decision,
                self.name,
                position=None,
                action="no_trade",
                reason="H1 pending next-bar confirmation.",
                extra_inputs={"h1_pending": True},
            )
            self._log(decision, audit, event="pending_breakout")
            return decision
        decision = _relabel(inner_decision, self.name, position=None)
        self._log(decision, audit, event="inner_passthrough")
        return decision

    def _enter_long(
        self,
        bars: Sequence[Bar],
        *,
        decision_time: datetime,
        market_health: Mapping[str, object],
        equity: float,
        prior_high: float,
        signal_bar_close: datetime,
    ) -> StrategyDecision:
        settings = self.settings
        try:
            bid = float(market_health["bid"])
            ask = float(market_health["ask"])
            spread = float(market_health["spread"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return StrategyDecision(
                "no_trade",
                self.name,
                "long",
                decision_time,
                "Market health is missing a valid quote.",
                {},
                None,
            )
        if (
            market_health.get("healthy") is not True
            or market_health.get("entry_allowed") is not True
        ):
            return StrategyDecision(
                "no_trade",
                self.name,
                "long",
                decision_time,
                "Market health does not permit new entries.",
                {},
                None,
            )
        mid = (bid + ask) / 2
        spread_fraction = spread / mid
        stop_price = ask * (1 - settings.stop_loss_fraction)
        target_price = ask * (1 + settings.momentum_target_fraction)
        expected_move = abs(target_price - ask) / ask
        estimated_cost = (
            2 * settings.fee_rate_per_side
            + 2 * settings.slippage_rate_per_side
            + spread_fraction
        )
        if expected_move <= estimated_cost + settings.minimum_net_edge_fraction:
            return StrategyDecision(
                "no_trade",
                self.name,
                "long",
                decision_time,
                "Expected movement does not exceed estimated round-trip costs and minimum edge.",
                {
                    "expected_move_fraction": expected_move,
                    "estimated_cost_fraction": estimated_cost,
                },
                None,
            )
        risk_budget = equity * settings.risk_fraction
        risk_per_btc = abs(ask - stop_price) + ask * estimated_cost
        max_notional = equity / (1 + estimated_cost)
        quantity = min(risk_budget / risk_per_btc, max_notional / ask)
        if not math.isfinite(quantity) or quantity <= 0:
            return StrategyDecision(
                "no_trade",
                self.name,
                "long",
                decision_time,
                "Could not calculate a positive, unleveraged position size.",
                {},
                None,
            )
        fill = ask * (1 + settings.slippage_rate_per_side)
        if not stop_price < fill < target_price:
            return StrategyDecision(
                "no_trade",
                self.name,
                "long",
                decision_time,
                "Fill would fall outside stop/target.",
                {},
                None,
            )
        new_position = SimulatedPosition(
            self.name,
            "long",
            ask,
            quantity,
            stop_price,
            target_price,
            decision_time,
        )
        return StrategyDecision(
            "entry",
            self.name,
            "long",
            decision_time,
            "H1 next-bar close confirmation passed.",
            {
                "prior_high": prior_high,
                "signal_bar_close": signal_bar_close.isoformat(),
                "confirm_close": bars[-1].close,
                "breakout_level": prior_high * (1 + settings.breakout_buffer_fraction),
                "expected_move_fraction": expected_move,
                "estimated_cost_fraction": estimated_cost,
                "quantity_btc": quantity,
            },
            new_position,
        )

    def _audit(
        self,
        bars: Sequence[Bar],
        decision_time: datetime,
        market_health: Mapping[str, object],
    ) -> dict[str, object]:
        last = bars[-1] if bars else None
        bar_ok = last is not None and _utc(last.timestamp) <= _utc(decision_time)
        return {
            "decision_time": _utc(decision_time).isoformat(),
            "bar_close": last.timestamp.isoformat() if last is not None else None,
            "bar_available_at_decision": bar_ok,
            "quote_received_at": market_health.get("host_received_at_utc"),
            "quote_available_at_decision": quote_available_at_decision(
                market_health, decision_time
            ),
            "health_age_seconds": market_health.get("age_seconds"),
            "healthy": market_health.get("healthy"),
        }

    def _log(
        self,
        decision: StrategyDecision,
        audit: Mapping[str, object],
        *,
        event: str,
    ) -> None:
        self.decision_log.append(
            {
                "event": event,
                "action": decision.action,
                "direction": decision.direction,
                "reason": decision.reason,
                "timestamp": decision.timestamp.isoformat(),
                **audit,
                "inputs": dict(decision.inputs),
            }
        )


class DecisionLoggingAdapter:
    """Record production-adapter decisions without changing production code."""

    def __init__(self, settings: StrategySettings | None = None) -> None:
        if settings is None:
            inner = adapt_production("momentum_breakout")
        else:
            inner = ProductionStrategyAdapter(
                "momentum_breakout",
                inner=MomentumBreakoutStrategy(settings),
            )
        self._inner = inner
        self.name = inner.name
        self.settings = inner.settings
        self.spec = inner.spec
        self.decision_log = []

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
        decision = self._inner.evaluate(
            bars,
            decision_time=decision_time,
            market_health=market_health,
            equity=equity,
            position=position,
        )
        last = bars[-1] if bars else None
        self.decision_log.append(
            {
                "event": "baseline_decision",
                "action": decision.action,
                "direction": decision.direction,
                "reason": decision.reason,
                "timestamp": decision.timestamp.isoformat(),
                "bar_close": last.timestamp.isoformat() if last is not None else None,
                "bar_available_at_decision": (
                    last is not None and _utc(last.timestamp) <= _utc(decision_time)
                ),
                "quote_received_at": market_health.get("host_received_at_utc"),
                "quote_available_at_decision": quote_available_at_decision(
                    market_health, decision_time
                ),
                "health_age_seconds": market_health.get("age_seconds"),
                "healthy": market_health.get("healthy"),
                "inputs": dict(decision.inputs),
            }
        )
        return decision
