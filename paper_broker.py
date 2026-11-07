"""Local, paper-only accounting for strategy decisions; never sends orders."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from config import (
    LIVE_EXECUTION_ENABLED,
    PAPER_TRADING_ONLY,
    STARTING_BALANCE_AMOUNT,
    STARTING_BALANCE_CURRENCY,
)
from strategy import SimulatedPosition, StrategyDecision

logger = logging.getLogger(__name__)

EXIT_FILL_EXECUTABLE_QUOTE = "executable_quote"
EXIT_FILL_DECISION_PRICE = "decision_exit_price"
EXIT_FILL_MODES = frozenset(
    {EXIT_FILL_EXECUTABLE_QUOTE, EXIT_FILL_DECISION_PRICE}
)


@dataclass(frozen=True)
class PaperBrokerSettings:
    """Broker assumptions; all rates are fractions per fill or per day.

    Starting capital and accounting units are USDT; no EUR conversion is
    performed. Long entries cross to the ask and default exits cross to the
    bid, then apply adverse slippage. ``exit_fill_mode=decision_exit_price``
    uses the strategy decision's ``exit_price`` as that exit reference
    instead of the bid; fees, half-spread estimates, and per-side slippage
    still apply. Fees are charged once per fill. Crossing a quote already
    accounts for spread in a quote-based fill price.
    """

    initial_cash: float = STARTING_BALANCE_AMOUNT
    fee_rate_per_side: float = 0.001
    slippage_rate_per_side: float = 0.0005
    risk_fraction: float = 0.005
    daily_loss_limit_fraction: float = 0.02
    max_drawdown_fraction: float = 0.10
    max_market_data_age_seconds: float = 3.0
    journal_path: Path | str = Path("paper_broker.jsonl")
    allow_simulated_shorts: bool = False
    exit_fill_mode: str = EXIT_FILL_EXECUTABLE_QUOTE

    def __post_init__(self) -> None:
        if self.allow_simulated_shorts:
            raise ValueError("Binance Spot paper trading does not support short positions.")
        if self.exit_fill_mode not in EXIT_FILL_MODES:
            raise ValueError(
                "exit_fill_mode must be executable_quote or decision_exit_price."
            )
        for name in (
            "initial_cash",
            "fee_rate_per_side",
            "slippage_rate_per_side",
            "risk_fraction",
            "daily_loss_limit_fraction",
            "max_drawdown_fraction",
            "max_market_data_age_seconds",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative.")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive.")
        if not 0 < self.risk_fraction < 1:
            raise ValueError("risk_fraction must be between 0 and 1.")
        if not 0 < self.daily_loss_limit_fraction < 1:
            raise ValueError("daily_loss_limit_fraction must be between 0 and 1.")
        if not 0 < self.max_drawdown_fraction < 1:
            raise ValueError("max_drawdown_fraction must be between 0 and 1.")
        if self.max_market_data_age_seconds <= 0:
            raise ValueError("max_market_data_age_seconds must be positive.")


@dataclass(frozen=True)
class BrokerResult:
    accepted: bool
    action: str
    reason: str
    fill_price: float | None = None
    quantity_btc: float | None = None


@dataclass
class _OpenPosition:
    strategy: str
    direction: str
    quantity_btc: float
    entry_price: float
    entry_reference_price: float
    entry_fee: float
    entry_spread_fraction: float
    stop_price: float
    target_price: float
    entry_timestamp: datetime
    entry_spread_cost: float
    entry_slippage_cost: float
    pending_exit_trigger: str | None = None
    pending_exit_reason: str | None = None
    pending_exit_timestamp: datetime | None = None


def _timestamp(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} timestamp is missing or invalid.")
    try:
        return _timestamp(datetime.fromisoformat(value))
    except ValueError as error:
        raise ValueError(f"{name} timestamp is missing or invalid.") from error


class PaperBroker:
    """Binance Spot, long-only USDT paper ledger with a persistent audit journal."""

    def __init__(self, settings: PaperBrokerSettings | None = None) -> None:
        self.settings = settings or PaperBrokerSettings()
        if PAPER_TRADING_ONLY is not True or LIVE_EXECUTION_ENABLED is not False:
            raise RuntimeError("Paper broker requires paper-only safety settings.")
        self._journal_path = Path(self.settings.journal_path)
        self.initial_cash = self.settings.initial_cash
        self.cash = self.settings.initial_cash
        self.realized_pnl = 0.0
        self.position: _OpenPosition | None = None
        self.peak_equity = self.settings.initial_cash
        self.day_key: str | None = None
        self.day_start_equity: float | None = None
        self.last_bid: float | None = None
        self.last_ask: float | None = None
        self._restore()
        if not self._journal_path.exists():
            self._record(
                "account_initialized",
                datetime.now(timezone.utc),
                {
                    "starting_cash": self.settings.initial_cash,
                    "currency_basis": STARTING_BALANCE_CURRENCY,
                    "reason": "Paper-only simulated account initialized.",
                },
            )

    @property
    def equity(self) -> float:
        if self.position is None:
            return self.cash
        if self.last_bid is None or self.last_ask is None:
            raise RuntimeError("Open position has no market mark.")
        if self.position.direction != "long":
            raise RuntimeError("Spot broker cannot value an unsupported short position.")
        return self.cash + self.position.quantity_btc * self.last_bid

    @property
    def unrealized_pnl(self) -> float:
        if self.position is None:
            return 0.0
        if self.last_bid is None or self.last_ask is None:
            raise RuntimeError("Open position has no market mark.")
        if self.position.direction != "long":
            raise RuntimeError("Spot broker cannot value an unsupported short position.")
        gross = (self.last_bid - self.position.entry_price) * self.position.quantity_btc
        return gross - self.position.entry_fee

    @property
    def daily_loss_fraction(self) -> float:
        if not self.day_start_equity:
            return 0.0
        return max(0.0, (self.day_start_equity - self.equity) / self.day_start_equity)

    @property
    def drawdown_fraction(self) -> float:
        if self.peak_equity <= 0:
            return 1.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def total_pnl(self) -> float:
        return self.equity - self.initial_cash

    @property
    def exit_pending(self) -> dict[str, object] | None:
        if self.position is None or self.position.pending_exit_trigger is None:
            return None
        return {
            "trigger": self.position.pending_exit_trigger,
            "reason": self.position.pending_exit_reason or "",
            "timestamp": (
                self.position.pending_exit_timestamp.isoformat()
                if self.position.pending_exit_timestamp is not None else ""
            ),
            "unresolved_simulated_exposure": True,
            "loss_bound_guaranteed": False,
        }

    def _position_dict(self) -> dict | None:
        if self.position is None:
            return None
        data = asdict(self.position)
        data["entry_timestamp"] = self.position.entry_timestamp.isoformat()
        if self.position.pending_exit_timestamp is not None:
            data["pending_exit_timestamp"] = (
                self.position.pending_exit_timestamp.isoformat()
            )
        return data

    def _snapshot(self) -> dict:
        return {
            "currency": STARTING_BALANCE_CURRENCY,
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "position": self._position_dict(),
            "peak_equity": self.peak_equity,
            "day_key": self.day_key,
            "day_start_equity": self.day_start_equity,
            "last_bid": self.last_bid,
            "last_ask": self.last_ask,
        }

    def _apply_snapshot(self, state: Mapping[str, object]) -> None:
        currency = state.get("currency", STARTING_BALANCE_CURRENCY)
        if currency != STARTING_BALANCE_CURRENCY:
            raise ValueError("Broker journal currency does not match USDT accounting.")
        self.initial_cash = float(state["initial_cash"])
        self.cash = float(state["cash"])
        self.realized_pnl = float(state["realized_pnl"])
        self.peak_equity = float(state["peak_equity"])
        self.day_key = state["day_key"]
        self.day_start_equity = (
            None if state["day_start_equity"] is None
            else float(state["day_start_equity"])
        )
        self.last_bid = None if state["last_bid"] is None else float(state["last_bid"])
        self.last_ask = None if state["last_ask"] is None else float(state["last_ask"])
        stored_position = state["position"]
        if stored_position is None:
            self.position = None
        else:
            position = dict(stored_position)
            position["entry_timestamp"] = _parse_timestamp(
                position["entry_timestamp"], "entry"
            )
            if position.get("pending_exit_timestamp") is not None:
                position["pending_exit_timestamp"] = _parse_timestamp(
                    position["pending_exit_timestamp"], "pending exit"
                )
            self.position = _OpenPosition(**position)

    def _restore(self) -> None:
        if not self._journal_path.exists():
            return
        latest_state = None
        with self._journal_path.open("rb") as journal:
            journal.seek(0, 2)
            size = journal.tell()
            if size:
                journal.seek(-1, 2)
                if journal.read(1) != b"\n":
                    raise ValueError(
                        "Broker journal final record is not newline-terminated."
                    )
        with self._journal_path.open("r", encoding="utf-8") as journal:
            try:
                for line_number, line in enumerate(journal, start=1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise ValueError(
                            f"Invalid broker journal JSON on line {line_number}."
                        ) from error
                    if not isinstance(event, dict) or not isinstance(event.get("state"), dict):
                        raise ValueError(
                            f"Invalid broker journal record on line {line_number}."
                        )
                    latest_state = event["state"]
            except UnicodeDecodeError as error:
                raise ValueError("Broker journal contains invalid UTF-8.") from error
        if latest_state is None:
            raise ValueError("Broker journal contains no account state.")
        try:
            stored_initial_cash = float(latest_state["initial_cash"])
            if not math.isclose(stored_initial_cash, self.settings.initial_cash):
                raise ValueError("Configured starting capital differs from journal.")
            self._apply_snapshot(latest_state)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError("Broker journal account state is invalid.") from error
        numeric_values = [
            self.initial_cash,
            self.cash,
            self.realized_pnl,
            self.peak_equity,
            *(
                [self.day_start_equity]
                if self.day_start_equity is not None else []
            ),
            *(value for value in (self.last_bid, self.last_ask) if value is not None),
        ]
        if not all(math.isfinite(value) for value in numeric_values):
            raise ValueError("Broker journal account state contains invalid numbers.")
        if self.position is not None:
            position_values = (
                self.position.quantity_btc,
                self.position.entry_price,
                self.position.entry_reference_price,
                self.position.entry_fee,
                self.position.entry_spread_fraction,
                self.position.stop_price,
                self.position.target_price,
                self.position.entry_spread_cost,
                self.position.entry_slippage_cost,
            )
            if (
                self.position.direction != "long"
                or not all(math.isfinite(value) for value in position_values)
                or self.position.quantity_btc <= 0
            ):
                raise ValueError(
                    "Broker journal contains an invalid or unsupported Spot position."
                )
            if self.position.pending_exit_timestamp is not None:
                _timestamp(self.position.pending_exit_timestamp)

    def _record(
        self, event_type: str, timestamp: datetime, details: Mapping[str, object]
    ) -> None:
        event = {
            "timestamp": _timestamp(timestamp).isoformat(),
            "event": event_type,
            "details": dict(details),
            "state": self._snapshot(),
        }
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self._journal_path.open("a", encoding="utf-8") as journal:
            journal.write(json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n")
            journal.flush()

    @staticmethod
    def _valid_quote(
        market_health: Mapping[str, object],
    ) -> tuple[float, float, float]:
        try:
            raw_bid = market_health["bid"]
            raw_ask = market_health["ask"]
            raw_spread = market_health["spread"]
            if any(isinstance(value, bool) for value in (raw_bid, raw_ask, raw_spread)):
                raise ValueError("Market quote contains a boolean price.")
            bid, ask, spread = float(raw_bid), float(raw_ask), float(raw_spread)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError("Market quote is missing or invalid.") from error
        if (
            not all(math.isfinite(value) and value > 0 for value in (bid, ask, spread))
            or ask <= bid
            or not math.isclose(ask - bid, spread, rel_tol=1e-6, abs_tol=1e-8)
        ):
            raise ValueError("Market quote is missing or invalid.")
        return bid, ask, spread

    def _fresh_quote(
        self,
        market_health: Mapping[str, object],
        timestamp: datetime,
        *,
        require_entry_allowed: bool = True,
    ) -> tuple[float, float, float]:
        if not isinstance(market_health, Mapping):
            raise ValueError("Market health is missing.")
        if (
            market_health.get("healthy") is not True
            or (
                require_entry_allowed
                and market_health.get("entry_allowed") is not True
            )
            or market_health.get("connection_status") != "connected"
        ):
            raise ValueError("Market health does not permit a fresh executable quote.")
        bid, ask, spread = self._valid_quote(market_health)
        last_update = _parse_timestamp(
            market_health.get("last_valid_update"), "last valid update"
        )
        message_time = _parse_timestamp(
            market_health.get("message_timestamp"), "message"
        )
        if message_time != last_update:
            raise ValueError("Market quote timestamps are inconsistent.")
        age = (timestamp - last_update).total_seconds()
        message_age = (timestamp - message_time).total_seconds()
        reported_age = market_health.get("age_seconds")
        if (
            age < 0
            or message_age < 0
            or age > self.settings.max_market_data_age_seconds
            or message_age > self.settings.max_market_data_age_seconds
            or isinstance(reported_age, bool)
            or not isinstance(reported_age, (int, float))
            or not math.isfinite(reported_age)
            or reported_age < 0
            or reported_age > self.settings.max_market_data_age_seconds
        ):
            raise ValueError("Market data is stale or has invalid timestamps.")
        return bid, ask, spread

    def _update_risk_marks(
        self, timestamp: datetime, bid: float, ask: float
    ) -> None:
        self.last_bid = bid
        self.last_ask = ask
        equity = self.equity
        self.peak_equity = max(self.peak_equity, equity)
        day = timestamp.date().isoformat()
        if day != self.day_key:
            self.day_key = day
            self.day_start_equity = equity

    def _reject(
        self,
        timestamp: datetime,
        reason: str,
        *,
        event: str = "risk_rejection",
        strategy: str | None = None,
    ) -> BrokerResult:
        logger.warning(
            "paper_broker_rejection strategy=%s timestamp=%s reason=%s",
            strategy, timestamp.isoformat(), reason,
        )
        self._record(event, timestamp, {"strategy": strategy, "reason": reason})
        return BrokerResult(False, "rejected", reason)

    def process(
        self,
        decision: StrategyDecision,
        *,
        market_health: Mapping[str, object],
    ) -> BrokerResult:
        """Apply a strategy entry/exit decision to the local ledger only."""
        try:
            timestamp = _timestamp(decision.timestamp)
        except (AttributeError, ValueError) as error:
            timestamp = datetime.now(timezone.utc)
            return self._reject(timestamp, str(error), strategy=getattr(decision, "strategy", None))

        if decision.action == "no_trade":
            return self._reject(
                timestamp,
                decision.reason,
                event="strategy_rejection",
                strategy=decision.strategy,
            )
        if decision.action == "entry":
            return self._open(decision, market_health, timestamp)
        if decision.action == "exit":
            return self._close(decision, market_health, timestamp)
        return self._reject(
            timestamp,
            f"Unsupported strategy action: {decision.action}.",
            strategy=decision.strategy,
        )

    def _open(
        self,
        decision: StrategyDecision,
        market_health: Mapping[str, object],
        timestamp: datetime,
    ) -> BrokerResult:
        if decision.direction == "short" or (
            decision.position is not None
            and decision.position.direction == "short"
        ):
            return self._reject(
                timestamp,
                "Short entry rejected: Binance Spot paper trading is long-only.",
                strategy=decision.strategy,
            )
        if self.position is not None:
            return self._reject(
                timestamp, "An open position already exists; duplicate entry rejected.",
                strategy=decision.strategy,
            )
        try:
            bid, ask, spread = self._fresh_quote(market_health, timestamp)
        except ValueError as error:
            return self._reject(timestamp, str(error), strategy=decision.strategy)

        requested = decision.position
        if (
            requested is None
            or requested.strategy != decision.strategy
            or requested.direction != decision.direction
            or requested.direction != "long"
        ):
            return self._reject(
                timestamp, "Entry decision has an invalid simulated position.",
                strategy=decision.strategy,
            )
        try:
            quantity = float(requested.quantity_btc)
            stop = float(requested.stop_price)
            target = float(requested.target_price)
        except (TypeError, ValueError, OverflowError):
            return self._reject(
                timestamp, "Entry position contains invalid sizing or levels.",
                strategy=decision.strategy,
            )
        if (
            not all(math.isfinite(value) and value > 0 for value in (quantity, stop, target))
            or not stop < ask < target
        ):
            return self._reject(
                timestamp, "Entry position has invalid sizing, stop, or target.",
                strategy=decision.strategy,
            )
        try:
            requested_timestamp = _timestamp(requested.entry_timestamp)
        except ValueError as error:
            return self._reject(timestamp, str(error), strategy=decision.strategy)
        if requested_timestamp != timestamp:
            return self._reject(
                timestamp, "Entry position timestamp does not match the decision.",
                strategy=decision.strategy,
            )

        old_state = self._snapshot()
        self._update_risk_marks(timestamp, bid, ask)
        equity_before = self.equity

        def reject_after_mark(reason: str) -> BrokerResult:
            try:
                return self._reject(
                    timestamp,
                    reason,
                    strategy=decision.strategy,
                )
            except BaseException:
                self._apply_snapshot(old_state)
                raise

        if self.daily_loss_fraction >= self.settings.daily_loss_limit_fraction:
            return reject_after_mark(
                "Daily-loss limit reached; new entry denied."
            )
        if self.drawdown_fraction >= self.settings.max_drawdown_fraction:
            return reject_after_mark(
                "Maximum drawdown limit reached; new entry denied."
            )

        mid = (bid + ask) / 2
        spread_fraction = spread / mid
        reference = ask
        fill = reference * (1 + self.settings.slippage_rate_per_side)
        if not stop < fill < target:
            return self._reject(
                timestamp, "Simulated fill falls outside the stop/target levels.",
                strategy=decision.strategy,
            )
        fee = fill * quantity * self.settings.fee_rate_per_side
        spread_cost = spread / 2 * quantity
        slippage_cost = reference * self.settings.slippage_rate_per_side * quantity
        estimated_round_trip_cost = (
            2 * self.settings.fee_rate_per_side
            + 2 * self.settings.slippage_rate_per_side
            + spread_fraction
        )
        risk_per_btc = abs(reference - stop) + reference * estimated_round_trip_cost
        planned_risk = quantity * risk_per_btc
        if planned_risk > equity_before * self.settings.risk_fraction:
            return self._reject(
                timestamp,
                "Position sizing exceeds the configured stop-and-cost risk budget.",
                strategy=decision.strategy,
            )
        notional = fill * quantity
        if notional + fee > equity_before:
            return self._reject(
                timestamp, "Position notional exceeds equity; leverage is disabled.",
                strategy=decision.strategy,
            )
        if notional + fee > self.cash:
            return self._reject(
                timestamp, "Insufficient cash for unleveraged long position.",
                strategy=decision.strategy,
            )

        cash_before = self.cash
        try:
            self.cash -= notional + fee
            self.position = _OpenPosition(
                strategy=decision.strategy,
                direction=requested.direction,
                quantity_btc=quantity,
                entry_price=fill,
                entry_reference_price=reference,
                entry_fee=fee,
                entry_spread_fraction=spread_fraction,
                stop_price=stop,
                target_price=target,
                entry_timestamp=timestamp,
                entry_spread_cost=spread_cost,
                entry_slippage_cost=slippage_cost,
            )
            self._update_risk_marks(timestamp, bid, ask)
            self._record(
                "entry_fill",
                timestamp,
                {
                    "strategy": decision.strategy,
                    "direction": requested.direction,
                    "fill_price": fill,
                    "reference_price": reference,
                    "quantity_btc": quantity,
                    "notional": notional,
                    "fee": fee,
                    "spread_cost_estimate": spread_cost,
                    "slippage_cost_estimate": slippage_cost,
                    "cash_before": cash_before,
                    "cash_after": self.cash,
                    "stop_price": stop,
                    "target_price": target,
                    "risk_budget": equity_before * self.settings.risk_fraction,
                    "planned_risk": planned_risk,
                    "reason": decision.reason,
                },
            )
        except BaseException:
            self._apply_snapshot(old_state)
            raise
        return BrokerResult(True, "entry", decision.reason, fill, quantity)

    def _close(
        self,
        decision: StrategyDecision,
        market_health: Mapping[str, object],
        timestamp: datetime,
    ) -> BrokerResult:
        if self.position is None:
            return self._reject(
                timestamp, "No open position to close.",
                strategy=decision.strategy,
            )
        position = self.position
        if position.strategy != decision.strategy or position.direction != decision.direction:
            return self._reject(
                timestamp, "Exit does not match the open simulated position.",
                strategy=decision.strategy,
            )
        if timestamp <= position.entry_timestamp:
            return self._reject(
                timestamp, "Exit timestamp must follow the entry timestamp.",
                strategy=decision.strategy,
            )
        try:
            requested_reference = float(decision.inputs["exit_price"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return self._reject(
                timestamp, "Exit decision is missing a valid reference price.",
                strategy=decision.strategy,
            )
        if not math.isfinite(requested_reference) or requested_reference <= 0:
            return self._reject(
                timestamp, "Exit reference price is invalid.",
                strategy=decision.strategy,
            )
        trigger = str(decision.inputs.get("trigger", ""))
        if not trigger:
            reason = decision.reason.casefold()
            trigger = (
                "stop_loss" if "stop" in reason
                else "take_profit" if "target" in reason
                else "strategy_exit"
            )
        try:
            bid, ask, spread = self._fresh_quote(
                market_health, timestamp, require_entry_allowed=False
            )
        except ValueError as error:
            return self._set_exit_pending(
                position,
                timestamp,
                trigger,
                decision.reason,
                str(error),
            )

        old_state = self._snapshot()
        close_reason = position.pending_exit_reason or decision.reason
        trigger = position.pending_exit_trigger or trigger
        self._update_risk_marks(timestamp, bid, ask)
        if self.settings.exit_fill_mode == EXIT_FILL_DECISION_PRICE:
            reference = requested_reference
        else:
            reference = bid
        spread_fraction = spread / ((bid + ask) / 2)
        fill = reference * (1 - self.settings.slippage_rate_per_side)
        exit_fee = fill * position.quantity_btc * self.settings.fee_rate_per_side
        exit_spread_cost = spread / 2 * position.quantity_btc
        exit_slippage_cost = reference * self.settings.slippage_rate_per_side * position.quantity_btc
        cash_before = self.cash
        try:
            self.cash += fill * position.quantity_btc - exit_fee
            gross_pnl = (fill - position.entry_price) * position.quantity_btc
            net_pnl = gross_pnl - position.entry_fee - exit_fee
            total_costs = (
                position.entry_fee
                + exit_fee
                + position.entry_spread_cost
                + exit_spread_cost
                + position.entry_slippage_cost
                + exit_slippage_cost
            )
            self.realized_pnl += net_pnl
            self.position = None
            self.peak_equity = max(self.peak_equity, self.equity)
            self._record(
                "trade_closed",
                timestamp,
                {
                    "strategy": decision.strategy,
                    "direction": position.direction,
                    "entry_timestamp": position.entry_timestamp.isoformat(),
                    "exit_timestamp": timestamp.isoformat(),
                    "entry_fill_price": position.entry_price,
                    "exit_fill_price": fill,
                    "quantity_btc": position.quantity_btc,
                    "entry_fee": position.entry_fee,
                    "exit_fee": exit_fee,
                    "entry_spread_cost_estimate": position.entry_spread_cost,
                    "exit_spread_cost_estimate": exit_spread_cost,
                    "entry_slippage_cost_estimate": position.entry_slippage_cost,
                    "exit_slippage_cost_estimate": exit_slippage_cost,
                    "total_costs": total_costs,
                    "gross_pnl": gross_pnl,
                    "net_realized_pnl": net_pnl,
                    "cash_before": cash_before,
                    "cash_after": self.cash,
                    "reason": close_reason,
                    "trigger": trigger,
                    "exit_fill_mode": self.settings.exit_fill_mode,
                    "exit_reference_price": reference,
                    "synthetic_bid": bid,
                    "synthetic_ask": ask,
                },
            )
        except BaseException:
            self._apply_snapshot(old_state)
            raise
        return BrokerResult(
            True,
            "exit",
            close_reason,
            fill,
            position.quantity_btc,
        )

    def _set_exit_pending(
        self,
        position: _OpenPosition,
        timestamp: datetime,
        trigger: str,
        reason: str,
        quote_reason: str,
    ) -> BrokerResult:
        if position.pending_exit_trigger is not None:
            return BrokerResult(
                False,
                "exit_pending",
                f"Exit pending fresh-quote verification ({quote_reason}).",
            )
        old_state = self._snapshot()
        position.pending_exit_trigger = trigger
        position.pending_exit_reason = reason
        position.pending_exit_timestamp = timestamp
        try:
            self._record(
                "exit_pending",
                timestamp,
                {
                    "strategy": position.strategy,
                    "direction": position.direction,
                    "trigger": trigger,
                    "reason": reason,
                    "awaiting": quote_reason,
                },
            )
        except BaseException:
            self._apply_snapshot(old_state)
            raise
        return BrokerResult(
            False,
            "exit_pending",
            f"Exit pending fresh-quote verification ({quote_reason}).",
        )

    def process_protective_quote(
        self,
        market_health: Mapping[str, object],
        *,
        timestamp: datetime,
    ) -> BrokerResult | None:
        """Check a fresh executable bid and resolve a triggered long exit once."""
        if self.position is None:
            return None
        try:
            observed_at = _timestamp(timestamp)
            bid, _ask, _spread = self._fresh_quote(
                market_health, observed_at, require_entry_allowed=False
            )
        except (TypeError, ValueError):
            return None
        position = self.position
        if observed_at <= position.entry_timestamp:
            return None
        if (
            position.pending_exit_timestamp is not None
            and observed_at <= position.pending_exit_timestamp
        ):
            return None
        trigger = position.pending_exit_trigger
        reason = position.pending_exit_reason
        if trigger is None:
            if bid <= position.stop_price:
                trigger = "stop_loss"
                reason = "Stop reached by fresh executable bid."
            elif bid >= position.target_price:
                trigger = "take_profit"
                reason = "Target reached by fresh executable bid."
            else:
                return None
        decision = StrategyDecision(
            action="exit",
            strategy=position.strategy,
            direction=position.direction,
            timestamp=observed_at,
            reason=reason or "Pending protective exit resolved by fresh quote.",
            inputs={"exit_price": bid, "trigger": trigger},
        )
        return self._close(decision, market_health, observed_at)

    def mark_to_market(
        self,
        market_health: Mapping[str, object],
        *,
        timestamp: datetime,
    ) -> BrokerResult:
        """Update valuation and risk peaks from a fresh, healthy market quote."""
        try:
            observed_at = _timestamp(timestamp)
            bid, ask, _spread = self._fresh_quote(
                market_health, observed_at, require_entry_allowed=False
            )
        except (TypeError, ValueError) as error:
            observed_at = (
                timestamp if isinstance(timestamp, datetime)
                and timestamp.tzinfo is not None
                else datetime.now(timezone.utc)
            )
            return self._reject(observed_at, str(error), event="valuation_rejection")
        old_state = self._snapshot()
        self._update_risk_marks(observed_at, bid, ask)
        equity = self.equity
        if self._snapshot() == old_state:
            return BrokerResult(
                True,
                "mark",
                "Fresh market valuation unchanged; journal snapshot skipped.",
            )
        try:
            self._record(
                "mark_to_market",
                observed_at,
                {
                    "bid": bid,
                    "ask": ask,
                    "equity": equity,
                    "realized_pnl": self.realized_pnl,
                    "unrealized_pnl": self.unrealized_pnl,
                    "daily_loss_fraction": self.daily_loss_fraction,
                    "drawdown_fraction": self.drawdown_fraction,
                },
            )
        except BaseException:
            self._apply_snapshot(old_state)
            raise
        return BrokerResult(True, "mark", "Fresh market valuation recorded.")
