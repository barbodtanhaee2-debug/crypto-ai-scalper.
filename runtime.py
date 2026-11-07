"""Local paper-only runtime; public market data in, simulated ledger out."""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from config import LIVE_EXECUTION_ENABLED, PAPER_TRADING_ONLY
from market_data import (
    BINANCE_CLOCK_RESYNC_INTERVAL_SECONDS,
    BinanceServerClock,
    BinanceBookTicker,
    BinanceKlineStream,
    MarketDataSettings,
    binance_kline_interval_label,
    fetch_binance_server_time,
    fetch_recent_binance_klines,
)
from paper_broker import PaperBroker, PaperBrokerSettings
from runtime_status import write_runtime_status
from strategy import (
    Bar,
    MomentumBreakoutStrategy,
    RangeMeanReversionStrategy,
    SimulatedPosition,
    StrategyDecision,
    StrategyExperiment,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeSettings:
    journal_path: Path | str = Path("paper_broker.jsonl")
    status_path: Path | str = Path("runtime_status.json")
    candle_interval_seconds: int = 60
    candle_max_age_seconds: float = 90.0
    max_completed_bars: int = 1000
    heartbeat_interval_seconds: float = 1.0
    mark_interval_seconds: float = 5.0
    market_stale_after_seconds: float = 3.0
    max_reconnect_attempts: int = 5
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0

    def __post_init__(self) -> None:
        binance_kline_interval_label(self.candle_interval_seconds)
        for name in (
            "candle_max_age_seconds",
            "heartbeat_interval_seconds",
            "mark_interval_seconds",
            "market_stale_after_seconds",
            "base_backoff_seconds",
            "max_backoff_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be positive.")
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("max_backoff_seconds cannot be below base_backoff_seconds.")
        if (
            isinstance(self.max_completed_bars, bool)
            or not isinstance(self.max_completed_bars, int)
            or self.max_completed_bars < 2
        ):
            raise ValueError("max_completed_bars must be at least 2.")
        if (
            isinstance(self.max_reconnect_attempts, bool)
            or not isinstance(self.max_reconnect_attempts, int)
            or self.max_reconnect_attempts < 0
        ):
            raise ValueError("max_reconnect_attempts cannot be negative.")


def _aware_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Runtime timestamp must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _parse_runtime_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _aware_datetime(datetime.fromisoformat(value))
    except ValueError:
        return None


class _CandleSeries:
    """Maintain a strictly validated, contiguous series of exchange candles."""

    def __init__(self, interval_seconds: int, maximum_bars: int) -> None:
        self.interval_seconds = interval_seconds
        self.maximum_bars = maximum_bars
        self.bars: list[Bar] = []

    def clear(self) -> None:
        self.bars.clear()

    def seed(
        self,
        bars: list[Bar] | tuple[Bar, ...],
        *,
        now: datetime,
        maximum_age_seconds: float,
    ) -> None:
        from backtester import validate_candles

        if not bars:
            raise ValueError("No historical completed candles are available.")
        validated = list(
            validate_candles(bars, interval_seconds=self.interval_seconds)
        )
        age = (now - validated[-1].timestamp).total_seconds()
        if age < 0 or age > maximum_age_seconds:
            raise ValueError("Historical completed candles are stale or future-dated.")
        self.bars = validated[-self.maximum_bars:]

    def append(self, bar: Bar) -> str:
        from backtester import validate_candles

        validated = validate_candles(
            [bar], interval_seconds=self.interval_seconds
        )[0]
        if not self.bars:
            self.bars.append(validated)
            return "added"

        previous = self.bars[-1]
        if validated.timestamp <= previous.timestamp:
            overlapping = next(
                (
                    existing for existing in self.bars
                    if existing.timestamp == validated.timestamp
                ),
                None,
            )
            if overlapping == validated:
                return "duplicate"
            self.clear()
            if overlapping is not None:
                raise ValueError("Conflicting duplicate completed candle rejected.")
            raise ValueError("Out-of-order completed candle rejected.")
        elapsed = (validated.timestamp - previous.timestamp).total_seconds()
        if elapsed != self.interval_seconds:
            self.bars = [validated]
            return "gap"
        self.bars.append(validated)
        if len(self.bars) > self.maximum_bars:
            del self.bars[:-self.maximum_bars]
        return "added"


class _ObservedFeed(BinanceBookTicker):
    def __init__(
        self,
        *args,
        on_message,
        monotonic: Callable[[], float] = time.monotonic,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._on_message = on_message
        self._monotonic = monotonic
        self.messages_received = 0
        self.messages_accepted = 0
        self.messages_rejected = 0
        self.connection_attempts = 0
        self.reconnect_count = 0
        self.connection_messages_received = 0
        self.connection_messages_accepted = 0
        self.connection_messages_rejected = 0
        self.rejection_reasons: dict[str, int] = {}
        self.last_rejection_reason: str | None = None
        self.last_message_timestamp: datetime | None = None
        self.last_message_accepted: bool | None = None
        self.last_receive_monotonic: float | None = None
        self.last_accepted_monotonic: float | None = None
        self.last_processing_duration_seconds: float | None = None

    def process_message(self, raw_message: str | bytes) -> bool:
        received_monotonic = self._monotonic()
        try:
            received_at = _aware_datetime(self._clock())
        except ValueError:
            received_at = None
        self.messages_received += 1
        self.connection_messages_received += 1
        self.last_receive_monotonic = received_monotonic
        self.last_message_timestamp = received_at
        self.last_message_accepted = None
        try:
            accepted = super().process_message(raw_message)
        finally:
            finished_monotonic = self._monotonic()
            self.last_processing_duration_seconds = max(
                0.0, finished_monotonic - received_monotonic
            )
        if accepted:
            self.messages_accepted += 1
            self.connection_messages_accepted += 1
            self.last_accepted_monotonic = received_monotonic
            self.last_rejection_reason = None
        else:
            self.messages_rejected += 1
            self.connection_messages_rejected += 1
            self.last_rejection_reason = self._rejection_category(self.last_error)
            self.rejection_reasons[self.last_rejection_reason] = (
                self.rejection_reasons.get(self.last_rejection_reason, 0) + 1
            )
        self.last_message_accepted = accepted
        self._on_message(accepted)
        return accepted

    def mark_connecting(self) -> None:
        super().mark_connecting()
        self.connection_attempts += 1

    def mark_connected(self) -> None:
        super().mark_connected()
        self.connection_messages_received = 0
        self.connection_messages_accepted = 0
        self.connection_messages_rejected = 0

    def mark_reconnecting(self, reason: str) -> None:
        super().mark_reconnecting(reason)
        self.reconnect_count += 1

    @staticmethod
    def _rejection_category(reason: str | None) -> str:
        value = (reason or "").casefold()
        if value.startswith("malformed json"):
            return "malformed_json"
        if value.startswith("message must") or value.startswith("message data must"):
            return "invalid_message_shape"
        if value.startswith("message symbol"):
            return "unexpected_symbol"
        if value.startswith("ask must"):
            return "crossed_market"
        if value.startswith("bid ") or value.startswith("ask "):
            return "invalid_price"
        if value.startswith("exchange event timestamp"):
            return "invalid_event_timestamp"
        return "invalid_quote"

    def diagnostic_snapshot(self) -> dict[str, object]:
        now = self._monotonic()

        def age(timestamp: float | None) -> float | None:
            return (
                max(0.0, now - timestamp)
                if timestamp is not None
                else None
            )

        return {
            "connection_state": self.connection_status,
            "connection_attempts": self.connection_attempts,
            "reconnect_count": self.reconnect_count,
            "messages_received": self.messages_received,
            "valid_quotes_accepted": self.messages_accepted,
            "invalid_messages_rejected": self.messages_rejected,
            "connection_messages_received": self.connection_messages_received,
            "connection_messages_accepted": self.connection_messages_accepted,
            "connection_messages_rejected": self.connection_messages_rejected,
            "rejection_reasons": dict(self.rejection_reasons),
            "last_rejection_reason": self.last_rejection_reason,
            "last_receive_at": (
                self.last_message_timestamp.isoformat()
                if self.last_message_timestamp is not None else None
            ),
            "last_receive_age_seconds": age(self.last_receive_monotonic),
            "last_accepted_quote_at": (
                self.last_valid_update.isoformat()
                if self.last_valid_update is not None else None
            ),
            "last_accepted_quote_age_seconds": age(self.last_accepted_monotonic),
            "last_processing_duration_seconds": self.last_processing_duration_seconds,
        }


class PaperRuntime:
    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        *,
        broker: PaperBroker | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        status_writer: Callable[[str | Path, Mapping[str, object]], None] = write_runtime_status,
    ) -> None:
        self.settings = settings or RuntimeSettings()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._exchange_clock = BinanceServerClock(
            clock=self._clock,
            monotonic=monotonic,
        )
        self._status_writer = status_writer
        self.broker = broker or PaperBroker(
            PaperBrokerSettings(
                journal_path=self.settings.journal_path,
                max_market_data_age_seconds=self.settings.market_stale_after_seconds,
            )
        )
        self._updates: asyncio.Queue[
            tuple[str, object, object | None, float | None]
        ] = asyncio.Queue()
        self._monotonic = monotonic
        self._last_quote_queue_delay_seconds: float | None = None
        self._max_quote_queue_delay_seconds = 0.0
        self._last_event_loop_lag_seconds = 0.0
        self._max_event_loop_lag_seconds = 0.0
        self._feed = _ObservedFeed(
            MarketDataSettings(
                stale_after_seconds=self.settings.market_stale_after_seconds,
                max_reconnect_attempts=self.settings.max_reconnect_attempts,
                base_backoff_seconds=self.settings.base_backoff_seconds,
                max_backoff_seconds=self.settings.max_backoff_seconds,
            ),
            clock=self._clock,
            on_message=self._on_feed_message,
            monotonic=monotonic,
        )
        self._candle_feed = BinanceKlineStream(
            MarketDataSettings(
                stale_after_seconds=self.settings.candle_max_age_seconds,
                max_reconnect_attempts=self.settings.max_reconnect_attempts,
                base_backoff_seconds=self.settings.base_backoff_seconds,
                max_backoff_seconds=self.settings.max_backoff_seconds,
            ),
            interval_seconds=self.settings.candle_interval_seconds,
            clock=self._clock,
            exchange_clock=self._exchange_clock.now,
            on_candle=self._on_candle_message,
            on_status=self._on_candle_status,
        )
        self._candle_series = _CandleSeries(
            self.settings.candle_interval_seconds,
            self.settings.max_completed_bars,
        )
        self._bars: list[Bar] = []
        self._latest_completed_bar: Bar | None = None
        self._candle_history_ready = False
        self._candle_integrity_error = False
        self._pending_candles: list[Bar] = []
        self._candle_generation = 0
        self._latest_valid_market_timestamp: datetime | None = None
        self._last_broker_mark: datetime | None = None
        self._last_feed_status: str | None = None
        self._last_candle_feed_status: str | None = None
        self._journal_ok = True
        self._status_ok = True
        self._running = False
        self._last_decision: StrategyDecision | None = None
        self._last_decision_reason: str | None = None
        self._last_strategy_evaluation_timestamp: datetime | None = None
        self._strategy_decisions: dict[str, dict[str, object]] = {}
        self._entry_permission = False
        self._risk_lockout: bool | None = None
        self.experiment = StrategyExperiment(
            MomentumBreakoutStrategy(),
            RangeMeanReversionStrategy(),
        )

    def _on_feed_message(self, accepted: bool) -> None:
        now = self._clock()
        self._updates.put_nowait(
            (
                "quote",
                accepted,
                self._feed.health(now=now),
                self._monotonic(),
            )
        )

    def _on_candle_message(self, accepted: bool, bar: Bar | None) -> None:
        self._updates.put_nowait(("candle", accepted, bar, None))

    def _on_candle_status(self, status: str) -> None:
        self._updates.put_nowait(("candle_status", status, None, None))

    def _connection_status(self) -> str:
        return self._feed.connection_status

    def _risk_state(self) -> tuple[bool | None, float | None, float | None]:
        try:
            daily_loss = self.broker.daily_loss_fraction
            drawdown = self.broker.drawdown_fraction
        except (ArithmeticError, RuntimeError, TypeError, ValueError):
            return None, None, None
        if not math.isfinite(daily_loss) or not math.isfinite(drawdown):
            return None, None, None
        lockout = (
            daily_loss >= self.broker.settings.daily_loss_limit_fraction
            or drawdown >= self.broker.settings.max_drawdown_fraction
        )
        return lockout, daily_loss, drawdown

    def _current_health(self, now: datetime) -> dict:
        return self._feed.health(now=now)

    def _required_bar_count(self) -> int:
        return max(
            strategy.settings.lookback_bars + 1
            for strategy in self.experiment.strategies
        )

    def _candle_fresh(self) -> bool:
        if self._latest_completed_bar is None:
            return False
        try:
            exchange_now = self._candle_feed.exchange_now()
        except ValueError:
            return False
        age = (exchange_now - self._latest_completed_bar.timestamp).total_seconds()
        return (
            0 <= age <= self.settings.candle_max_age_seconds
            and self._candle_feed.health(now=exchange_now)["healthy"] is True
        )

    def _entry_diagnostics(
        self, now: datetime, health: Mapping[str, object]
    ) -> dict[str, object]:
        required_bars = self._required_bar_count()
        candle_fresh = self._candle_fresh()
        candle_health = self._candle_feed.health()
        market_age = (
            (now - self._latest_valid_market_timestamp).total_seconds()
            if self._latest_valid_market_timestamp is not None else None
        )
        feed_age = health.get("age_seconds")
        market_fresh = (
            market_age is not None
            and 0 <= market_age <= self.settings.market_stale_after_seconds
        )
        lockout, daily_loss, drawdown = self._risk_state()
        blockers = []
        if not self._running:
            blockers.append("Runtime is not running.")
        if PAPER_TRADING_ONLY is not True or LIVE_EXECUTION_ENABLED is not False:
            blockers.append("Paper-only safety configuration is invalid.")
        if not self._journal_ok:
            blockers.append("Broker journal state is uncertain.")
        if not self._status_ok:
            blockers.append("Runtime status could not be written atomically.")
        if self.broker.position is not None:
            if self.broker.exit_pending is not None:
                blockers.append(
                    "Protective exit is pending fresh-quote verification; "
                    "new entries remain blocked."
                )
            else:
                blockers.append("An open simulated position blocks another entry.")
        if health.get("healthy") is not True or health.get("entry_allowed") is not True:
            blockers.append(str(health.get("reason") or "Market feed health is not valid."))
        if not market_fresh:
            blockers.append("Latest valid market update is stale, missing, or future-dated.")
        if not candle_fresh:
            blockers.append(
                str(candle_health.get("reason")
                    or "No fresh completed exchange candle is available.")
            )
        if len(self._bars) < required_bars:
            blockers.append(
                f"Strategy warm-up needs {required_bars} contiguous completed bars; "
                f"{len(self._bars)} available."
            )
        if lockout is None:
            blockers.append("Broker risk status is unavailable.")
        elif lockout:
            blockers.append("Broker risk limit lockout is active.")
        gate_allowed, gate_reason = self._entry_gate(now, health)
        if not gate_allowed and gate_reason not in blockers:
            blockers.insert(0, gate_reason)
        return {
            "readiness_blockers": blockers,
            "market_fresh": market_fresh,
            "entry_gate_market_data_age_seconds": market_age,
            "feed_reported_market_data_age_seconds": feed_age,
            "completed_candle_fresh": candle_fresh,
            "candle_connection_status": self._candle_feed.connection_status,
            "candle_data_healthy": candle_health["healthy"],
            "completed_candle_count": len(self._bars),
            "required_completed_candle_count": required_bars,
            "warmup_candles_remaining": max(0, required_bars - len(self._bars)),
            "current_candle_samples": None,
            "required_candle_samples": None,
            "candle_history_contiguous": bool(self._bars),
            "strategy_warmup_ready": len(self._bars) >= required_bars,
            "open_position": self.broker.position is not None,
            "risk_lockout": lockout,
            "daily_loss_fraction": daily_loss,
            "drawdown_fraction": drawdown,
            "strategy_evaluation_timestamp": (
                self._last_strategy_evaluation_timestamp.isoformat()
                if self._last_strategy_evaluation_timestamp is not None else None
            ),
            "strategy_decisions": self._strategy_decisions or None,
        }

    def _entry_gate(self, now: datetime, health: Mapping[str, object]) -> tuple[bool, str]:
        if not self._running:
            return False, "Runtime is not running."
        if PAPER_TRADING_ONLY is not True or LIVE_EXECUTION_ENABLED is not False:
            return False, "Paper-only safety configuration is invalid."
        if not self._journal_ok:
            return False, "Broker journal state is uncertain."
        if not self._status_ok:
            return False, "Runtime status could not be written atomically."
        if self.broker.position is not None:
            if self.broker.exit_pending is not None:
                return False, (
                    "Protective exit is pending fresh-quote verification; "
                    "new entries remain blocked."
                )
            return False, "An open simulated position blocks another entry."
        if health.get("healthy") is not True or health.get("entry_allowed") is not True:
            return False, str(health.get("reason") or "Market feed health is not valid.")
        if self._latest_valid_market_timestamp is None:
            return False, "No fresh valid market update has been received."
        data_age = (now - self._latest_valid_market_timestamp).total_seconds()
        if data_age < 0 or data_age > self.settings.market_stale_after_seconds:
            return False, "Latest market update is stale or future-dated."
        if not self._candle_fresh():
            candle_health = self._candle_feed.health()
            return False, str(
                candle_health.get("reason")
                or "No fresh completed exchange candle is available."
            )
        required_bars = self._required_bar_count()
        if len(self._bars) < required_bars:
            return False, (
                f"Strategy warm-up needs {required_bars} contiguous completed bars; "
                f"{len(self._bars)} available."
            )
        lockout = self._risk_state()[0]
        if lockout is None:
            return False, "Broker risk status is unavailable."
        if lockout:
            return False, "Broker risk limit lockout is active."
        return True, "Fresh market quote and completed exchange candles validated."

    def _status_record(
        self,
        *,
        now: datetime,
        health: Mapping[str, object],
        reason: str,
        running: bool | None = None,
    ) -> dict[str, object]:
        permission, gate_reason = self._entry_gate(now, health)
        if running is False:
            permission = False
        lockout, daily_loss, drawdown = self._risk_state()
        diagnostics = self._entry_diagnostics(now, health)
        candle_health = self._candle_feed.health()
        heartbeat = now.isoformat() if running is not False else None
        quote_diagnostics = self._feed.diagnostic_snapshot()
        quote_diagnostics.update({
            "runtime_queue_delay_seconds": self._last_quote_queue_delay_seconds,
            "max_runtime_queue_delay_seconds": self._max_quote_queue_delay_seconds,
            "runtime_event_loop_lag_seconds": self._last_event_loop_lag_seconds,
            "max_runtime_event_loop_lag_seconds": self._max_event_loop_lag_seconds,
            "runtime_update_queue_depth": self._updates.qsize(),
        })
        pending = self.broker.exit_pending
        if pending is not None:
            pending = dict(pending)
            pending_at = _parse_runtime_timestamp(pending.get("timestamp"))
            pending["age_seconds"] = (
                max(0.0, (now - pending_at).total_seconds())
                if pending_at is not None else None
            )
            pending["fill_fabricated_from_stale_quote"] = False
            pending["unresolved_loss_possible"] = True
        return {
            "process_running": self._running if running is None else running,
            "process_heartbeat": heartbeat,
            "connection_status": health.get("connection_status"),
            "candle_connection_status": candle_health.get("connection_status"),
            "candle_data_healthy": candle_health.get("healthy"),
            "candle_data_age_seconds": candle_health.get("age_seconds"),
            "candle_data_stale_limit_seconds": self.settings.candle_max_age_seconds,
            "candle_data_error": candle_health.get("last_error"),
            "websocket_messages_received": self._feed.messages_received,
            "websocket_messages_accepted": self._feed.messages_accepted,
            "websocket_messages_rejected": self._feed.messages_rejected,
            "last_websocket_message_timestamp": (
                self._feed.last_message_timestamp.isoformat()
                if self._feed.last_message_timestamp is not None else None
            ),
            "last_websocket_message_accepted": self._feed.last_message_accepted,
            "last_market_data_error": self._feed.last_error,
            "latest_valid_market_data_timestamp": (
                health.get("last_valid_update")
                if isinstance(health.get("last_valid_update"), str) else None
            ),
            "latest_processed_market_data_timestamp": (
                self._latest_valid_market_timestamp.isoformat()
                if self._latest_valid_market_timestamp is not None else None
            ),
            "market_data_healthy": (
                health.get("healthy")
                if isinstance(health.get("healthy"), bool) else None
            ),
            "market_data_age_seconds": health.get("age_seconds"),
            "market_data_stale_limit_seconds": self.settings.market_stale_after_seconds,
            "spread": health.get("spread"),
            "quote_diagnostics": quote_diagnostics,
            "entry_permission": permission,
            "entry_permission_reason": gate_reason if not permission else reason,
            "entry_diagnostics": diagnostics,
            "paper_only": (
                PAPER_TRADING_ONLY is True and LIVE_EXECUTION_ENABLED is False
            ),
            "risk_lockout": lockout,
            "open_position": self.broker.position is not None,
            "exit_pending": pending,
            "daily_loss_fraction": daily_loss,
            "drawdown_fraction": drawdown,
            "daily_loss_limit_fraction": (
                self.broker.settings.daily_loss_limit_fraction
                if self.broker is not None else None
            ),
            "max_drawdown_fraction": (
                self.broker.settings.max_drawdown_fraction
                if self.broker is not None else None
            ),
            "journal_ok": self._journal_ok,
            "status_write_ok": self._status_ok,
            "latest_completed_candle_timestamp": (
                self._latest_completed_bar.timestamp.isoformat()
                if self._latest_completed_bar is not None else None
            ),
            "candle_interval_seconds": self.settings.candle_interval_seconds,
            "candle_interval": binance_kline_interval_label(
                self.settings.candle_interval_seconds
            ),
            "candle_source": "Binance public BTCUSDT kline stream and REST history",
            "candle_fresh": self._candle_fresh(),
            "last_decision": self._last_decision.action
            if self._last_decision is not None else None,
            "last_decision_reason": self._last_decision_reason,
            "runtime_reason": reason,
        }

    def _write_status(
        self,
        now: datetime,
        health: Mapping[str, object],
        reason: str = "Runtime status heartbeat.",
        *,
        running: bool | None = None,
    ) -> bool:
        record = self._status_record(
            now=now, health=health, reason=reason, running=running
        )
        try:
            self._status_writer(self.settings.status_path, record)
        except (OSError, TypeError, ValueError):
            self._status_ok = False
            self._entry_permission = False
            logger.exception("Could not atomically write runtime status.")
            return False
        self._status_ok = True
        self._entry_permission = bool(record["entry_permission"])
        return True

    def _valid_current_quote(self, health: Mapping[str, object]) -> tuple[float, float, datetime]:
        if health.get("healthy") is not True:
            raise ValueError("Market update was not healthy.")
        bid = health.get("bid")
        ask = health.get("ask")
        if (
            isinstance(bid, bool)
            or isinstance(ask, bool)
            or not isinstance(bid, (int, float))
            or not isinstance(ask, (int, float))
            or not math.isfinite(bid)
            or not math.isfinite(ask)
            or bid <= 0
            or ask <= bid
        ):
            raise ValueError("Market quote is inconsistent or invalid.")
        timestamp = _parse_runtime_timestamp(health.get("last_valid_update"))
        message_time = _parse_runtime_timestamp(health.get("message_timestamp"))
        if timestamp is None or message_time is None or timestamp != message_time:
            raise ValueError("Market timestamp is missing or inconsistent.")
        return float(bid), float(ask), timestamp

    def _observe_connection_status(self, status: str) -> None:
        if status == self._last_feed_status:
            return
        is_unavailable = status in ("disconnected", "reconnecting", "connecting")
        if is_unavailable:
            self._entry_permission = False
            self._last_decision_reason = (
                "Quote feed connection changed; waiting for a fresh valid quote."
            )
        self._last_feed_status = status

    def _observe_candle_connection_status(self, status: str) -> bool:
        if status == self._last_candle_feed_status:
            return False
        was_unavailable = self._last_candle_feed_status in (
            "disconnected", "reconnecting", "connecting"
        ) or self._last_candle_feed_status is None
        is_unavailable = status in ("disconnected", "reconnecting", "connecting")
        if is_unavailable:
            self._candle_generation += 1
        if is_unavailable or (was_unavailable and status == "connected"):
            self._candle_series.clear()
            self._bars.clear()
            self._latest_completed_bar = None
            self._pending_candles.clear()
            self._candle_history_ready = False
            self._entry_permission = False
            self._last_decision_reason = (
                "Kline feed connection changed; contiguous candle history reset."
            )
        self._last_candle_feed_status = status
        return status == "connected" and was_unavailable

    async def _load_candle_history(self, loader, generation: int) -> None:
        required_bars = self._required_bar_count()
        try:
            exchange_now = self._candle_feed.exchange_now()
            bars = await asyncio.to_thread(
                loader,
                self.settings.candle_interval_seconds,
                min(required_bars + 1, 1000),
                now=exchange_now,
            )
        except Exception as error:
            logger.warning("Could not load Binance candle warm-up history: %s", error)
            self._updates.put_nowait(
                ("history", (None, generation), str(error), None)
            )
            return
        self._updates.put_nowait(("history", (bars, generation), None, None))

    def _handle_history(
        self,
        bars: object,
        error: object,
        now: datetime,
        *,
        generation: int | None = None,
    ) -> None:
        if (
            generation is not None
            and (
                generation != self._candle_generation
                or self._candle_feed.connection_status != "connected"
            )
        ):
            return
        self._candle_history_ready = True
        if (
            error is None
            and not self._candle_integrity_error
            and self._candle_feed.connection_status == "connected"
        ):
            try:
                if (
                    not isinstance(bars, (list, tuple))
                    or len(bars) < self._required_bar_count()
                ):
                    raise ValueError(
                        "Historical data does not contain the required contiguous warm-up."
                    )
                self._candle_series.seed(
                    bars,
                    now=now,
                    maximum_age_seconds=self.settings.candle_max_age_seconds,
                )
                self._bars = list(self._candle_series.bars)
                self._latest_completed_bar = self._bars[-1]
            except (TypeError, ValueError) as history_error:
                self._candle_series.clear()
                self._bars.clear()
                self._latest_completed_bar = None
                self._last_decision_reason = (
                    f"Historical candle warm-up rejected: {history_error}"
                )
                logger.warning("%s", self._last_decision_reason)
        elif self._candle_integrity_error:
            self._last_decision_reason = (
                "Historical candle warm-up rejected after invalid live kline data."
            )
        elif error is not None:
            self._last_decision_reason = (
                f"Historical candle warm-up unavailable: {error}"
            )
        self._candle_integrity_error = False

        pending = self._pending_candles
        self._pending_candles = []
        health = self._current_health(now)
        for bar in pending:
            existing = next(
                (
                    candle for candle in self._candle_series.bars
                    if candle.timestamp == bar.timestamp
                ),
                None,
            )
            if existing is not None:
                if existing != bar:
                    self._candle_series.clear()
                    self._bars.clear()
                    self._latest_completed_bar = None
                    self._candle_history_ready = True
                    self._last_decision_reason = (
                        "Conflicting overlapping REST and WebSocket candles rejected."
                    )
                    break
                continue
            self._on_completed_bar(bar, now, health)

    def _on_completed_bar(
        self,
        bar: Bar,
        now: datetime,
        health: Mapping[str, object],
    ) -> None:
        candle_age = (now - bar.timestamp).total_seconds()
        if (
            candle_age < 0
            or candle_age > self.settings.candle_max_age_seconds
            or not math.isfinite(bar.open)
            or not math.isfinite(bar.high)
            or not math.isfinite(bar.low)
            or not math.isfinite(bar.close)
            or min(bar.open, bar.high, bar.low, bar.close) <= 0
        ):
            self._candle_series.clear()
            self._bars.clear()
            self._latest_completed_bar = None
            self._last_decision_reason = "Completed candle is stale, future-dated, or invalid."
            return
        try:
            result = self._candle_series.append(bar)
        except ValueError as error:
            self._candle_series.clear()
            self._bars.clear()
            self._last_completed_bar = None
            self._last_decision_reason = f"Candle history rejected: {error}"
            logger.warning("Rejected runtime candle history: %s", error)
            return

        if result == "duplicate":
            return
        self._bars = list(self._candle_series.bars)
        self._latest_completed_bar = bar
        if result == "gap":
            self._last_decision_reason = (
                "Missing exchange candle interval; contiguous history reset."
            )
            logger.warning("%s", self._last_decision_reason)
            return
        gate, gate_reason = self._entry_gate(now, health)
        scoped_health = dict(health)
        scoped_health["healthy"] = bool(gate)
        scoped_health["entry_allowed"] = bool(gate)
        if not gate and self.broker.position is None:
            self._last_decision_reason = gate_reason
            return
        preflight_health = dict(health)
        preflight_health["entry_allowed"] = False
        if gate and not self._write_status(
            now, preflight_health, "Strategy decision is being evaluated."
        ):
            scoped_health["healthy"] = False
            scoped_health["entry_allowed"] = False
            self._last_decision_reason = "Status persistence failed; entry blocked."
            return

        position = None
        if self.broker.position is not None:
            current = self.broker.position
            position = SimulatedPosition(
                strategy=current.strategy,
                direction=current.direction,
                entry_price=current.entry_price,
                quantity_btc=current.quantity_btc,
                stop_price=current.stop_price,
                target_price=current.target_price,
                entry_timestamp=current.entry_timestamp,
            )
        decisions = self.experiment.evaluate(
            self._bars,
            decision_time=now,
            market_health=scoped_health,
            equity=self.broker.equity,
            position=position,
        )
        self._last_strategy_evaluation_timestamp = now
        self._strategy_decisions = {
            name: {
                "action": decision.action,
                "direction": decision.direction,
                "reason": decision.reason,
            }
            for name, decision in decisions.items()
        }
        actionable = next(
            (decision for decision in decisions.values()
             if decision.action in ("entry", "exit")),
            None,
        )
        if actionable is None:
            self._last_decision_reason = next(
                (decision.reason for decision in decisions.values()
                 if decision.action == "no_trade"),
                "No strategy signal.",
            )
            self._write_status(now, health, self._last_decision_reason)
            return
        try:
            result = self.broker.process(actionable, market_health=health)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            self._journal_ok = False
            self._entry_permission = False
            self._last_decision = actionable
            self._last_decision_reason = f"Broker persistence/processing failed: {error}"
            logger.exception("Paper broker rejected uncertain journal operation.")
            self._write_status(now, health, self._last_decision_reason)
            return
        self._last_decision = actionable
        self._last_decision_reason = (
            result.reason if result.accepted else f"Rejected by broker: {result.reason}"
        )
        if not result.accepted and actionable.action == "entry":
            self._entry_permission = False
        self._write_status(now, health, self._last_decision_reason)

    def _handle_update(
        self,
        accepted: bool,
        event_health: Mapping[str, object],
        *,
        queued_monotonic: float | None = None,
    ) -> None:
        if queued_monotonic is not None:
            queue_delay = max(0.0, self._monotonic() - queued_monotonic)
            self._last_quote_queue_delay_seconds = queue_delay
            self._max_quote_queue_delay_seconds = max(
                self._max_quote_queue_delay_seconds, queue_delay
            )
        now = _aware_datetime(self._clock())
        health = self._current_health(now)
        status = str(health.get("connection_status"))
        self._observe_connection_status(status)
        if not accepted:
            if health.get("healthy") is True:
                return
            self._entry_permission = False
            self._last_decision_reason = str(health.get("reason") or "Invalid market-data message.")
            logger.warning("Runtime received rejected market data: %s", self._last_decision_reason)
            return
        if (
            event_health.get("healthy") is not True
            or health.get("healthy") is not True
        ):
            self._entry_permission = False
            self._last_decision_reason = str(
                health.get("reason") or event_health.get("reason")
                or "Market data became unhealthy before processing."
            )
            return
        try:
            bid, ask, timestamp = self._valid_current_quote(event_health)
            current_bid, current_ask, current_timestamp = self._valid_current_quote(health)
        except ValueError as error:
            self._entry_permission = False
            self._last_decision_reason = str(error)
            logger.warning("Runtime market-data validation failed: %s", error)
            return
        self._latest_valid_market_timestamp = timestamp
        self._entry_permission = False
        try:
            protective_exit = self.broker.process_protective_quote(
                health, timestamp=current_timestamp
            )
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            self._journal_ok = False
            self._entry_permission = False
            self._last_decision_reason = (
                f"Protective exit persistence/processing failed: {error}"
            )
            logger.exception("Could not persist a protective paper-broker exit.")
            self._write_status(now, health, self._last_decision_reason)
            return
        if protective_exit is not None:
            self._last_decision_reason = protective_exit.reason
        should_mark = (
            timestamp == current_timestamp
            and bid == current_bid
            and ask == current_ask
            and (
                self._last_broker_mark is None
                or (timestamp - self._last_broker_mark).total_seconds()
                >= self.settings.mark_interval_seconds
            )
        )
        if should_mark:
            try:
                mark = self.broker.mark_to_market(health, timestamp=timestamp)
            except Exception as error:
                self._journal_ok = False
                self._last_decision_reason = f"Broker mark persistence failed: {error}"
                logger.exception("Could not persist a fresh paper-broker mark.")
                self._write_status(now, health, self._last_decision_reason)
                return
            if not mark.accepted:
                self._last_decision_reason = (
                    f"Broker rejected fresh market mark: {mark.reason}"
                )
                logger.error("%s", self._last_decision_reason)
                self._write_status(now, health, self._last_decision_reason)
                return
            self._last_broker_mark = timestamp
        gate, reason = self._entry_gate(now, health)
        if protective_exit is None:
            self._last_decision_reason = reason
        if gate:
            self._entry_permission = True
        elif protective_exit is not None:
            self._write_status(now, health, self._last_decision_reason or reason)

    def _handle_candle_update(self, accepted: bool, bar: object, now: datetime) -> None:
        if not accepted:
            self._candle_integrity_error = True
            self._candle_series.clear()
            self._bars.clear()
            self._latest_completed_bar = None
            self._pending_candles.clear()
            self._entry_permission = False
            self._last_decision_reason = str(
                self._candle_feed.health().get("reason")
                or "Invalid exchange candle received."
            )
            return
        if not isinstance(bar, Bar):
            return
        if not self._candle_history_ready:
            self._pending_candles.append(bar)
            return
        self._candle_integrity_error = False
        try:
            exchange_now = self._candle_feed.exchange_now()
        except ValueError as error:
            self._entry_permission = False
            self._last_decision_reason = (
                f"Binance server-time calibration unavailable: {error}"
            )
            return
        self._on_completed_bar(bar, exchange_now, self._current_health(now))

    async def run(
        self,
        *,
        connector=None,
        candle_connector=None,
        history_loader=fetch_recent_binance_klines,
        server_time_loader=fetch_binance_server_time,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Run quote and exchange-candle feeds until stopped or exhausted."""
        if PAPER_TRADING_ONLY is not True or LIVE_EXECUTION_ENABLED is not False:
            raise RuntimeError("Runtime refuses to start without paper-only configuration.")
        stop_event = stop_event or asyncio.Event()
        self._running = True
        initial_time = _aware_datetime(self._clock())
        initial_health = self._feed.health(now=initial_time)
        self._write_status(initial_time, initial_health, "Starting; waiting for market data.")

        def synchronize_exchange_time() -> None:
            try:
                self._exchange_clock.synchronize(server_time_loader)
            except (OSError, TimeoutError, TypeError, ValueError) as error:
                logger.warning(
                    "Could not synchronize Binance server time; candle validation "
                    "and entries remain fail-closed: %s",
                    error,
                )

        await asyncio.to_thread(synchronize_exchange_time)

        async def refresh_exchange_time() -> None:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=BINANCE_CLOCK_RESYNC_INTERVAL_SECONDS,
                    )
                    return
                except asyncio.TimeoutError:
                    await asyncio.to_thread(synchronize_exchange_time)

        exchange_time_task = asyncio.create_task(refresh_exchange_time())
        quote_task = asyncio.create_task(self._feed.run(connector=connector))
        candle_task = asyncio.create_task(
            self._candle_feed.run(
                connector=(
                    candle_connector if candle_connector is not None else connector
                )
            )
        )
        feed_tasks = (quote_task, candle_task)
        history_tasks: set[asyncio.Task] = set()
        loop = asyncio.get_running_loop()
        next_heartbeat = loop.time() + self.settings.heartbeat_interval_seconds
        feed_failure: Exception | None = None

        def observe_candle_status(status: str) -> None:
            if not self._observe_candle_connection_status(status):
                return
            task = asyncio.create_task(
                self._load_candle_history(
                    history_loader, self._candle_generation
                )
            )
            history_tasks.add(task)
            task.add_done_callback(history_tasks.discard)

        try:
            while not stop_event.is_set():
                try:
                    event = await asyncio.wait_for(
                        self._updates.get(),
                        timeout=max(0.0, next_heartbeat - loop.time()),
                    )
                except asyncio.TimeoutError:
                    event = None
                now = _aware_datetime(self._clock())
                if event is not None:
                    kind, first, second, queued_monotonic = event
                    if kind == "quote":
                        self._handle_update(
                            bool(first),
                            second or {},
                            queued_monotonic=queued_monotonic,
                        )
                    elif kind == "candle_status":
                        observe_candle_status(str(first))
                    elif kind == "candle":
                        self._handle_candle_update(bool(first), second, now)
                    elif kind == "history":
                        bars, generation = first
                        try:
                            candle_now = self._candle_feed.exchange_now()
                        except ValueError as error:
                            self._handle_history(
                                None,
                                f"Binance server-time calibration unavailable: {error}",
                                now,
                                generation=generation,
                            )
                        else:
                            self._handle_history(
                                bars,
                                second,
                                candle_now,
                                generation=generation,
                            )
                if loop.time() >= next_heartbeat:
                    loop_lag = max(0.0, loop.time() - next_heartbeat)
                    self._last_event_loop_lag_seconds = loop_lag
                    self._max_event_loop_lag_seconds = max(
                        self._max_event_loop_lag_seconds, loop_lag
                    )
                    current_health = self._current_health(now)
                    self._observe_connection_status(
                        str(current_health.get("connection_status"))
                    )
                    allowed, reason = self._entry_gate(now, current_health)
                    self._entry_permission = allowed
                    self._write_status(now, current_health, reason)
                    next_heartbeat = (
                        loop.time() + self.settings.heartbeat_interval_seconds
                    )
                if (
                    any(task.done() for task in feed_tasks)
                    and not history_tasks
                    and self._updates.empty()
                ):
                    break
        except asyncio.CancelledError:
            logger.info("Paper runtime shutdown requested by task cancellation.")
            raise
        finally:
            self._running = False
            tasks = (*feed_tasks, *history_tasks, exchange_time_task)
            for task in tasks:
                if not task.done():
                    task.cancel()
            task_results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in task_results:
                if isinstance(result, asyncio.CancelledError):
                    continue
                if isinstance(result, Exception):
                    logger.error("Market-data task failed during shutdown: %s", result)
                    self._journal_ok = False
                    if feed_failure is None:
                        feed_failure = result
            shutdown_time = _aware_datetime(self._clock())
            shutdown_health = self._current_health(shutdown_time)
            self._write_status(
                shutdown_time,
                shutdown_health,
                "Paper runtime stopped; new entries disabled.",
                running=False,
            )
        if feed_failure is not None:
            raise RuntimeError("Market-data runtime task failed.") from feed_failure


async def _run_from_cli(args: argparse.Namespace) -> None:
    stop_event = asyncio.Event()
    previous_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda *_: stop_event.set())
        except (ValueError, OSError):
            logger.warning("Could not register signal handler for %s.", sig)
    runtime = PaperRuntime(
        RuntimeSettings(
            journal_path=args.journal,
            status_path=args.status,
            candle_interval_seconds=args.candle_interval_seconds,
            candle_max_age_seconds=args.candle_max_age_seconds,
            market_stale_after_seconds=args.market_stale_after_seconds,
            max_reconnect_attempts=args.max_reconnect_attempts,
        )
    )
    try:
        await runtime.run(stop_event=stop_event)
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", default="paper_broker.jsonl")
    parser.add_argument("--status", default="runtime_status.json")
    parser.add_argument("--candle-interval-seconds", type=int, default=60)
    parser.add_argument("--candle-max-age-seconds", type=float, default=90.0)
    parser.add_argument("--market-stale-after-seconds", type=float, default=3.0)
    parser.add_argument("--max-reconnect-attempts", type=int, default=5)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_run_from_cli(args))
    except KeyboardInterrupt:
        logger.info("Paper runtime stopped by keyboard interrupt.")
    except (OSError, ValueError, RuntimeError):
        logger.exception("Paper runtime could not start or shut down safely.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
