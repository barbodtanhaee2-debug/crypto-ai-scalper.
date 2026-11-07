"""Lab-only chronological candle replay with next-candle-open fills.

Separate from quote_tape_replay.py and production historical_replay.py.
Does not modify H1 confirmation rules or frozen baseline parameters.

Execution model (Stage 3.2.4.2):
- Synthetic candle-derived quotes only (mid ± spread_fraction); not L2 / bookTicker.
- Signal after candle N close; earliest fill at candle N+1 open.
- No intra-bar protective stop simulation on this path.
- Tape and historical candle results must remain separate dual tracks.
- Historical results are not equivalent to live execution.
- Formal scoring: use run_formal_historical_replay + CutoffValidatedCandles only;
  force_close_at_end=True is rejected.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from backtester import BacktestSettings, _position_for_strategy
from historical_replay import resolve_replay_journal
from paper_broker import PaperBroker, PaperBrokerSettings
from quote_tape_replay import LIVE_JOURNAL, journal_trade_fingerprint, settings_equity
from strategy import Bar, SimulatedPosition, StrategyDecision

from research_lab.backtesting.engine import _apply_cost_settings
from research_lab.datasets.historical_catalog import (
    SYNTHETIC_QUOTE_REASON,
    CutoffValidatedCandles,
    HistoricalCandle,
    assert_before_cutoff,
    parse_utc,
)
from research_lab import LAB_ROOT
from research_lab.events.schema import is_risk_limit_reason
from research_lab.events.writer import ResearchEventWriter
from research_lab.historical.constants import PROVENANCE, RESEARCH_CUTOFF
from research_lab.isolation import assert_isolated_path
from research_lab.strategies.adapters import ProductionStrategyAdapter
from research_lab.strategies.registry import make_research_strategy


def _assert_journal_under_runs(journal_path: Path) -> Path:
    resolved = assert_isolated_path(journal_path)
    runs_root = (LAB_ROOT / "experiments" / "runs").resolve()
    try:
        resolved.relative_to(runs_root)
        return resolved
    except ValueError:
        pass
    parts = {part.lower() for part in resolved.parts}
    if "experiments" in parts and "runs" in parts:
        return resolved
    raise ValueError(
        "HistoricalCandleReplay journals must live under "
        f"{runs_root} (or a temporary .../experiments/runs/ path for tests)."
    )

EXECUTION_MODEL = "next_available_candle_open"
EXECUTION_NOTES = (
    "Signal is generated only after candle N closes. "
    "Earliest simulated fill uses candle N+1 open as mid, then frozen synthetic "
    "spread and slippage. Missing N+1 cancels the pending fill (no fabrication). "
    "If N+1 open ask falls outside the signal stop/target band, the pending entry "
    "is cancelled (no stop/target rewrite) and candle N+1 is still evaluated. "
    "Observation quotes at decision time use candle N close mid and are labelled synthetic. "
    "Decision/position timestamps are aligned to the fill instant for PaperBroker validation. "
    "Quotes are synthetic candle-derived mid±spread — not historical L2 / bookTicker. "
    "Intra-bar protective stop execution is not simulated on this path. "
    "Tape (BINANCE_SPOT_LIVE_CAPTURE) and historical candle results must stay separate; "
    "historical results are not equivalent to live execution."
)
GAP_TOLERANCE_SECONDS = 90.0
EXPECTED_INTERVAL_SECONDS = 60.0
_JOURNAL_BUFFER_FLUSH_LINES = 2_048


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _install_buffered_broker_journal(
    broker: PaperBroker,
    *,
    record_mark_to_market: bool,
) -> Callable[[], None]:
    """Buffer PaperBroker journal writes (research-lab only; no paper_broker.py edits).

    Opening/flushing the journal on every mark/reject dominates multi-year formal runs.
    Economics and in-memory risk state are unchanged; only durable I/O is deferred.
    """

    buffer: list[str] = []
    journal_path = broker._journal_path
    original_record = broker._record

    def flush() -> None:
        if not buffer:
            return
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with journal_path.open("a", encoding="utf-8") as handle:
            handle.writelines(buffer)
        buffer.clear()

    def buffered_record(
        event_type: str, timestamp: datetime, details: Mapping[str, object]
    ) -> None:
        if not record_mark_to_market and event_type == "mark_to_market":
            # Risk peaks/equity already updated in memory before _record is called.
            return
        event = {
            "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
            "event": event_type,
            "details": dict(details),
            "state": broker._snapshot(),
        }
        buffer.append(json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n")
        if len(buffer) >= _JOURNAL_BUFFER_FLUSH_LINES:
            flush()

    broker._record = buffered_record  # type: ignore[method-assign]
    # Keep a reference so callers can restore if needed; flush closes the buffer.
    broker._flush_research_journal = flush  # type: ignore[attr-defined]
    broker._original_record = original_record  # type: ignore[attr-defined]
    return flush


def synthetic_market_health(
    *,
    mid: float,
    spread_fraction: float,
    quote_time: datetime,
    decision_or_fill_time: datetime,
    price_basis: str,
) -> dict[str, object]:
    if mid <= 0:
        raise ValueError("Synthetic mid must be positive.")
    spread = mid * spread_fraction
    bid = mid - spread / 2.0
    ask = mid + spread / 2.0
    age = max(0.0, (decision_or_fill_time - quote_time).total_seconds())
    return {
        "healthy": True,
        "entry_allowed": True,
        "connection_status": "connected",
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "message_timestamp": _iso(quote_time),
        "last_valid_update": _iso(quote_time),
        "host_received_at_utc": _iso(quote_time),
        "age_seconds": age,
        "reason": SYNTHETIC_QUOTE_REASON,
        "synthetic": True,
        "price_basis": price_basis,
        "provenance": PROVENANCE,
        "execution_model": EXECUTION_MODEL,
    }


def is_contiguous_next_minute(previous_close: datetime, next_open: datetime) -> bool:
    """True when next candle open follows the prior close within one-minute tolerance."""
    prev = parse_utc(previous_close)
    nxt = parse_utc(next_open)
    # Expected: next open ≈ previous close + 1ms (Binance inclusive close).
    delta = (nxt - prev).total_seconds()
    return -0.002 <= delta <= 1.0


def gap_seconds(previous: HistoricalCandle, current: HistoricalCandle) -> float:
    return (parse_utc(current.open_time) - parse_utc(previous.open_time)).total_seconds()


def _evaluate_bar_window(strategy: object, bars: Sequence[Bar]) -> Sequence[Bar]:
    """Trailing window sufficient for lookback/hold checks.

    Production `_evaluate` re-validates every bar each call; passing the full
    multi-year history is O(n²). Strategies only read a short trailing lookback
    plus holding-time math from timestamps, so a bounded window is equivalent.
    """

    need = 64
    min_hist = getattr(strategy, "minimum_history_bars", None)
    if isinstance(min_hist, int) and min_hist > 0:
        need = max(need, min_hist + 8)
    settings = getattr(strategy, "settings", None)
    if settings is not None:
        lookback = int(getattr(settings, "lookback_bars", 0) or 0)
        max_hold = int(getattr(settings, "max_holding_bars", 0) or 0)
        atr = int(getattr(settings, "atr_period", 0) or 0)
        slow = int(getattr(settings, "slow_period", 0) or 0)
        need = max(need, lookback + max_hold + atr + slow + 8)
    spec = getattr(strategy, "spec", None)
    if spec is not None:
        need = max(need, int(getattr(spec, "warmup_bars", 0) or 0) + 8)
    if len(bars) <= need:
        return bars
    return bars[-need:]


@dataclass
class HistoricalCandleReplay:
    candles: Sequence[HistoricalCandle]
    journal_path: Path
    strategy_name: str = "momentum_breakout"
    settings: BacktestSettings | None = None
    strategy_object: object | None = None
    research_cutoff: datetime = RESEARCH_CUTOFF
    force_close_at_end: bool = False
    # When False, omit routine no_trade decision rows from the in-memory event log
    # (fills/cancels/entry/exit decisions are still retained). Formal scoring uses False.
    retain_verbose_events: bool = True
    # When False, skip durable mark_to_market journal rows (in-memory marks still apply).
    # Formal multi-year runs set False; smoke/unit defaults keep True for audit parity.
    journal_mark_to_market: bool = True
    # Optional Stage 3.2.8 research event capture. None => no instrumentation I/O.
    # When set, trading decisions/fills/accounting must remain unchanged.
    research_event_capture: ResearchEventWriter | None = None
    # Optional fingerprints recorded on run_completed only (not used in trading).
    config_fingerprint: str | None = None
    dataset_manifest_fingerprint: str | None = None

    _strategy: object = field(init=False, default=None, repr=False)
    broker: PaperBroker | None = field(init=False, default=None, repr=False)
    _events: list[dict[str, object]] = field(init=False, default_factory=list, repr=False)
    _flush_journal: Callable[[], None] | None = field(
        init=False, default=None, repr=False
    )

    def __post_init__(self) -> None:
        if self.settings is None:
            self.settings = BacktestSettings(candle_interval_seconds=60)
        self.journal_path = _assert_journal_under_runs(self.journal_path)

    def _prepare(self) -> None:
        assert self.settings is not None
        # Console rejection warnings dominate wall time on long formal runs.
        logging.getLogger("paper_broker").setLevel(logging.ERROR)
        if self.journal_path.exists():
            self.journal_path.unlink()
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_path = resolve_replay_journal(
            self.journal_path, live_journal=LIVE_JOURNAL
        )
        raw = self.strategy_object or make_research_strategy(self.strategy_name)
        raw = _apply_cost_settings(raw, self.settings)
        self._strategy = raw
        if hasattr(raw, "name"):
            self.strategy_name = str(raw.name)
        self.broker = PaperBroker(
            PaperBrokerSettings(
                initial_cash=self.settings.initial_equity,
                fee_rate_per_side=self.settings.fee_rate_per_side,
                slippage_rate_per_side=self.settings.effective_slippage_rate_per_side,
                risk_fraction=self.settings.risk_fraction,
                daily_loss_limit_fraction=self.settings.daily_loss_limit_fraction,
                max_drawdown_fraction=self.settings.max_drawdown_fraction,
                max_market_data_age_seconds=max(
                    float(self.settings.candle_interval_seconds) * 2.0, 120.0
                ),
                journal_path=self.journal_path,
                allow_simulated_shorts=False,
                exit_fill_mode=self.settings.exit_fill_mode,
            )
        )
        self._flush_journal = _install_buffered_broker_journal(
            self.broker,
            record_mark_to_market=self.journal_mark_to_market,
        )
        self._events = []

    def _emit(
        self,
        event_type: str,
        *,
        event_time: datetime | None,
        candle_open_time: datetime | None = None,
        signal_time: datetime | None = None,
        signal_price: float | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        capture = self.research_event_capture
        if capture is None:
            return
        capture.emit(
            event_type,
            event_time=event_time,
            candle_open_time=candle_open_time,
            signal_time=signal_time,
            signal_price=signal_price,
            payload=payload,
        )

    def run(self) -> dict[str, object]:
        self._prepare()
        broker = self.broker
        settings = self.settings
        strategy = self._strategy
        capture = self.research_event_capture
        assert broker is not None and settings is not None and strategy is not None

        bars: list[Bar] = []
        pending: StrategyDecision | None = None
        pending_signal_close: datetime | None = None
        pending_signal_price: float | None = None
        pending_lifecycle_id: str | None = None
        decision_counts: Counter[str] = Counter()
        rejection_reasons: Counter[str] = Counter()
        entries_accepted = 0
        entries_rejected = 0
        fills_deferred = 0
        fills_executed = 0
        fills_cancelled_missing_next = 0
        fills_cancelled_incompatible = 0
        missing_intervals = 0
        candles_processed = 0
        look_ahead_rejections = 0
        failed: BaseException | None = None

        ordered = sorted(self.candles, key=lambda item: item.open_time)
        try:
            for index, candle in enumerate(ordered):
                assert_before_cutoff(
                    candle.close_time, cutoff=self.research_cutoff, label="close_time"
                )
                assert_before_cutoff(
                    candle.open_time, cutoff=self.research_cutoff, label="open_time"
                )

                if index > 0:
                    previous = ordered[index - 1]
                    delta = gap_seconds(previous, candle)
                    if delta > GAP_TOLERANCE_SECONDS:
                        missing_intervals += 1
                        if pending is not None:
                            self._events.append(
                                {
                                    "event": "pending_fill_cancelled_missing_next_candle",
                                    "timestamp": _iso(candle.open_time),
                                    "pending_action": pending.action,
                                    "signal_close": _iso(pending_signal_close)
                                    if pending_signal_close
                                    else None,
                                    "gap_seconds": delta,
                                }
                            )
                            if pending.action == "entry":
                                self._emit(
                                    "entry_cancelled_missing_candle",
                                    event_time=parse_utc(candle.open_time),
                                    candle_open_time=candle.open_time,
                                    signal_time=pending_signal_close,
                                    signal_price=pending_signal_price,
                                    payload={
                                        "lifecycle_id": pending_lifecycle_id,
                                        "pending_action": pending.action,
                                        "gap_seconds": delta,
                                        "reason": "missing_next_candle",
                                    },
                                )
                            elif pending.action == "exit":
                                self._emit(
                                    "exit_cancelled_missing_candle",
                                    event_time=parse_utc(candle.open_time),
                                    candle_open_time=candle.open_time,
                                    signal_time=pending_signal_close,
                                    signal_price=pending_signal_price,
                                    payload={
                                        "lifecycle_id": pending_lifecycle_id,
                                        "pending_action": pending.action,
                                        "gap_seconds": delta,
                                        "reason": "missing_next_candle",
                                    },
                                )
                            fills_cancelled_missing_next += 1
                            pending = None
                            pending_signal_close = None
                            pending_signal_price = None
                            pending_lifecycle_id = None
                            if capture is not None:
                                capture.set_pending_lifecycle_id(None)

                # --- Execute deferred fill at this candle's OPEN ---
                if pending is not None:
                    fill_time = parse_utc(candle.open_time)
                    fill_health = synthetic_market_health(
                        mid=candle.open,
                        spread_fraction=settings.spread_fraction,
                        quote_time=fill_time,
                        decision_or_fill_time=fill_time,
                        price_basis="next_candle_open",
                    )
                    if "synthetic" not in str(fill_health.get("reason", "")).lower():
                        raise RuntimeError("Fill quote must remain synthetic-labelled.")
                    synced_position = pending.position
                    if synced_position is not None:
                        synced_position = SimulatedPosition(
                            synced_position.strategy,
                            synced_position.direction,
                            synced_position.entry_price,
                            synced_position.quantity_btc,
                            synced_position.stop_price,
                            synced_position.target_price,
                            fill_time,
                        )
                    fill_decision = replace(
                        pending,
                        timestamp=fill_time,
                        position=synced_position,
                        inputs={
                            **dict(pending.inputs),
                            "signal_close_time": _iso(pending_signal_close)
                            if pending_signal_close
                            else None,
                            "fill_price_basis": "next_candle_open",
                            "execution_model": EXECUTION_MODEL,
                        },
                    )
                    ask = float(fill_health["ask"])
                    bid = float(fill_health["bid"])
                    if (
                        synced_position is not None
                        and fill_decision.action == "entry"
                    ):
                        compatible = (
                            synced_position.stop_price
                            < ask
                            < synced_position.target_price
                        )
                        cancel_reason = None
                        if ask <= synced_position.stop_price:
                            cancel_reason = "next_open_ask_at_or_below_stop"
                        elif ask >= synced_position.target_price:
                            cancel_reason = "next_open_ask_at_or_above_target"
                        self._emit(
                            "entry_compatibility_check",
                            event_time=fill_time,
                            candle_open_time=candle.open_time,
                            signal_time=pending_signal_close,
                            signal_price=pending_signal_price,
                            payload={
                                "lifecycle_id": pending_lifecycle_id,
                                "signal_close": (
                                    _iso(pending_signal_close)
                                    if pending_signal_close
                                    else None
                                ),
                                "next_open_mid": candle.open,
                                "next_open_bid": bid,
                                "next_open_ask": ask,
                                "stop_price": synced_position.stop_price,
                                "target_price": synced_position.target_price,
                                "compatible": compatible,
                                "cancellation_reason": cancel_reason,
                            },
                        )
                        if not compatible:
                            self._events.append(
                                {
                                    "event": "pending_fill_cancelled_incompatible_next_open",
                                    "timestamp": _iso(fill_time),
                                    "pending_action": pending.action,
                                    "signal_close": _iso(pending_signal_close)
                                    if pending_signal_close
                                    else None,
                                    "next_open_mid": candle.open,
                                    "ask": ask,
                                    "stop": synced_position.stop_price,
                                    "target": synced_position.target_price,
                                }
                            )
                            self._emit(
                                "entry_cancelled_incompatible",
                                event_time=fill_time,
                                candle_open_time=candle.open_time,
                                signal_time=pending_signal_close,
                                signal_price=pending_signal_price,
                                payload={
                                    "lifecycle_id": pending_lifecycle_id,
                                    "signal_close": (
                                        _iso(pending_signal_close)
                                        if pending_signal_close
                                        else None
                                    ),
                                    "next_open_mid": candle.open,
                                    "next_open_bid": bid,
                                    "next_open_ask": ask,
                                    "stop_price": synced_position.stop_price,
                                    "target_price": synced_position.target_price,
                                    "reason": cancel_reason,
                                },
                            )
                            fills_cancelled_incompatible += 1
                            pending = None
                            pending_signal_close = None
                            pending_signal_price = None
                            pending_lifecycle_id = None
                            if capture is not None:
                                capture.set_pending_lifecycle_id(None)
                            # Do not continue: still evaluate this candle after close.
                        else:
                            result = broker.process(
                                fill_decision, market_health=fill_health
                            )
                            self._events.append(
                                {
                                    "event": "deferred_fill",
                                    "timestamp": _iso(fill_time),
                                    "accepted": result.accepted,
                                    "action": result.action,
                                    "reason": result.reason,
                                    "fill_price": result.fill_price,
                                    "signal_close": _iso(pending_signal_close)
                                    if pending_signal_close
                                    else None,
                                    "price_basis": "next_candle_open",
                                    "synthetic_quote": True,
                                    "mid": candle.open,
                                }
                            )
                            fills_executed += 1
                            if result.accepted:
                                entries_accepted += 1
                                self._emit(
                                    "entry_filled",
                                    event_time=fill_time,
                                    candle_open_time=candle.open_time,
                                    signal_time=pending_signal_close,
                                    signal_price=pending_signal_price,
                                    payload={
                                        "lifecycle_id": pending_lifecycle_id,
                                        "fill_price": result.fill_price,
                                        "reason": result.reason,
                                        "stop_price": synced_position.stop_price,
                                        "target_price": synced_position.target_price,
                                        "next_open_mid": candle.open,
                                        "next_open_ask": ask,
                                        "next_open_bid": bid,
                                    },
                                )
                            else:
                                entries_rejected += 1
                                rejection_reasons[str(result.reason)] += 1
                                reject_type = (
                                    "risk_limit_rejected"
                                    if is_risk_limit_reason(result.reason)
                                    else "entry_rejected"
                                )
                                self._emit(
                                    reject_type,
                                    event_time=fill_time,
                                    candle_open_time=candle.open_time,
                                    signal_time=pending_signal_close,
                                    signal_price=pending_signal_price,
                                    payload={
                                        "lifecycle_id": pending_lifecycle_id,
                                        "reason": result.reason,
                                        "next_open_mid": candle.open,
                                        "next_open_ask": ask,
                                        "next_open_bid": bid,
                                        "stop_price": synced_position.stop_price,
                                        "target_price": synced_position.target_price,
                                    },
                                )
                            pending = None
                            pending_signal_close = None
                            pending_signal_price = None
                            pending_lifecycle_id = None
                            if capture is not None:
                                capture.set_pending_lifecycle_id(None)
                    else:
                        result = broker.process(
                            fill_decision, market_health=fill_health
                        )
                        self._events.append(
                            {
                                "event": "deferred_fill",
                                "timestamp": _iso(fill_time),
                                "accepted": result.accepted,
                                "action": result.action,
                                "reason": result.reason,
                                "fill_price": result.fill_price,
                                "signal_close": _iso(pending_signal_close)
                                if pending_signal_close
                                else None,
                                "price_basis": "next_candle_open",
                                "synthetic_quote": True,
                                "mid": candle.open,
                            }
                        )
                        fills_executed += 1
                        # Exit fills: flush broker journal so trade_closed is durable,
                        # then emit position_closed from the latest closed trade.
                        if pending.action == "exit" and capture is not None:
                            if result.accepted:
                                if self._flush_journal is not None:
                                    self._flush_journal()
                                closed = self._closed_trades()
                                if closed:
                                    trade = closed[-1]
                                    entry_ts = trade.get("entry_timestamp")
                                    exit_ts = trade.get("exit_timestamp")
                                    self._emit(
                                        "position_closed",
                                        event_time=(
                                            parse_utc(str(exit_ts))
                                            if isinstance(exit_ts, str)
                                            else fill_time
                                        ),
                                        candle_open_time=candle.open_time,
                                        signal_time=(
                                            parse_utc(str(entry_ts))
                                            if isinstance(entry_ts, str)
                                            else pending_signal_close
                                        ),
                                        signal_price=(
                                            float(trade["entry_fill_price"])
                                            if trade.get("entry_fill_price")
                                            is not None
                                            else pending_signal_price
                                        ),
                                        payload={
                                            "lifecycle_id": pending_lifecycle_id,
                                            "entry_timestamp": entry_ts,
                                            "exit_timestamp": exit_ts,
                                            "entry_fill_price": trade.get(
                                                "entry_fill_price"
                                            ),
                                            "exit_fill_price": trade.get(
                                                "exit_fill_price"
                                            ),
                                            "quantity_btc": trade.get("quantity_btc"),
                                            "gross_pnl": trade.get("gross_pnl"),
                                            "net_realized_pnl": trade.get(
                                                "net_realized_pnl"
                                            ),
                                            "total_costs": trade.get("total_costs"),
                                            "reason": trade.get("reason"),
                                            "direction": trade.get("direction"),
                                        },
                                    )
                            else:
                                reject_type = (
                                    "risk_limit_rejected"
                                    if is_risk_limit_reason(result.reason)
                                    else "exit_rejected"
                                )
                                self._emit(
                                    reject_type,
                                    event_time=fill_time,
                                    candle_open_time=candle.open_time,
                                    signal_time=pending_signal_close,
                                    signal_price=pending_signal_price,
                                    payload={
                                        "lifecycle_id": pending_lifecycle_id,
                                        "accepted": False,
                                        "action": result.action,
                                        "reason": result.reason,
                                        "pending_action": "exit",
                                    },
                                )
                        pending = None
                        pending_signal_close = None
                        pending_signal_price = None
                        pending_lifecycle_id = None
                        if capture is not None:
                            capture.set_pending_lifecycle_id(None)

                # --- Decision after THIS candle closes (observation only) ---
                candles_processed += 1
                bar = candle.as_bar()
                # Bars are append-only chronological; O(1) check replaces full-list scans.
                if bars and parse_utc(bars[-1].timestamp) > parse_utc(candle.close_time):
                    look_ahead_rejections += 1
                    raise ValueError("Look-ahead: bar list contains future timestamps.")
                bars.append(bar)
                decision_time = parse_utc(candle.close_time)
                observe_health = synthetic_market_health(
                    mid=candle.close,
                    spread_fraction=settings.spread_fraction,
                    quote_time=decision_time,
                    decision_or_fill_time=decision_time,
                    price_basis="signal_candle_close",
                )
                broker.mark_to_market(observe_health, timestamp=decision_time)

                # Append-only bars never contain future timestamps at decision_time.
                if parse_utc(bars[-1].timestamp) > decision_time:
                    look_ahead_rejections += 1
                    raise ValueError("Look-ahead: refused future bars at decision.")

                decision = strategy.evaluate(
                    _evaluate_bar_window(strategy, bars),
                    decision_time=decision_time,
                    market_health=observe_health,
                    equity=settings_equity(broker),
                    position=_position_for_strategy(broker),
                )
                decision_counts[decision.action] += 1
                decision_event = {
                    "event": "strategy_decision",
                    "timestamp": _iso(decision_time),
                    "action": decision.action,
                    "reason": decision.reason,
                    "direction": decision.direction,
                    "observation_price_basis": "signal_candle_close",
                    "fill_deferred": decision.action in ("entry", "exit"),
                    "synthetic_quote": True,
                }
                if self.retain_verbose_events or decision.action in ("entry", "exit"):
                    self._events.append(decision_event)
                self._emit(
                    "strategy_decision",
                    event_time=decision_time,
                    candle_open_time=candle.open_time,
                    signal_time=decision_time,
                    signal_price=float(candle.close),
                    payload={
                        "action": decision.action,
                        "reason": decision.reason,
                        "direction": decision.direction,
                        "observation_price_basis": "signal_candle_close",
                        "fill_deferred": decision.action in ("entry", "exit"),
                    },
                )
                if decision.action in ("entry", "exit"):
                    pending = decision
                    pending_signal_close = decision_time
                    pending_signal_price = float(candle.close)
                    pending_lifecycle_id = (
                        f"{capture.arm if capture else 'na'}:"
                        f"{decision.action}:{_iso(decision_time)}"
                    )
                    fills_deferred += 1
                    if decision.action == "entry":
                        stop = (
                            decision.position.stop_price
                            if decision.position is not None
                            else None
                        )
                        target = (
                            decision.position.target_price
                            if decision.position is not None
                            else None
                        )
                        self._emit(
                            "entry_signal",
                            event_time=decision_time,
                            candle_open_time=candle.open_time,
                            signal_time=decision_time,
                            signal_price=pending_signal_price,
                            payload={
                                "lifecycle_id": pending_lifecycle_id,
                                "direction": decision.direction,
                                "reason": decision.reason,
                                "stop_price": stop,
                                "target_price": target,
                            },
                        )
                        self._emit(
                            "entry_pending",
                            event_time=decision_time,
                            candle_open_time=candle.open_time,
                            signal_time=decision_time,
                            signal_price=pending_signal_price,
                            payload={
                                "lifecycle_id": pending_lifecycle_id,
                                "direction": decision.direction,
                                "stop_price": stop,
                                "target_price": target,
                                "execution_model": EXECUTION_MODEL,
                            },
                        )
                    else:
                        self._emit(
                            "position_exit_signal",
                            event_time=decision_time,
                            candle_open_time=candle.open_time,
                            signal_time=decision_time,
                            signal_price=pending_signal_price,
                            payload={
                                "lifecycle_id": pending_lifecycle_id,
                                "direction": decision.direction,
                                "reason": decision.reason,
                            },
                        )
                    if capture is not None:
                        capture.set_pending_lifecycle_id(pending_lifecycle_id)
                elif decision.action == "no_trade":
                    rejection_reasons[str(decision.reason)] += 1

            if pending is not None:
                self._events.append(
                    {
                        "event": "pending_fill_cancelled_end_of_series",
                        "timestamp": _iso(ordered[-1].close_time) if ordered else None,
                        "pending_action": pending.action,
                        "signal_close": _iso(pending_signal_close)
                        if pending_signal_close
                        else None,
                    }
                )
                end_time = (
                    parse_utc(ordered[-1].close_time) if ordered else None
                )
                if pending.action == "entry":
                    self._emit(
                        "entry_cancelled_missing_candle",
                        event_time=end_time,
                        candle_open_time=ordered[-1].open_time if ordered else None,
                        signal_time=pending_signal_close,
                        signal_price=pending_signal_price,
                        payload={
                            "lifecycle_id": pending_lifecycle_id,
                            "pending_action": pending.action,
                            "reason": "end_of_series",
                        },
                    )
                elif pending.action == "exit":
                    self._emit(
                        "exit_cancelled_missing_candle",
                        event_time=end_time,
                        candle_open_time=ordered[-1].open_time if ordered else None,
                        signal_time=pending_signal_close,
                        signal_price=pending_signal_price,
                        payload={
                            "lifecycle_id": pending_lifecycle_id,
                            "pending_action": pending.action,
                            "reason": "end_of_series",
                        },
                    )
                fills_cancelled_missing_next += 1
                pending = None
                pending_lifecycle_id = None
                if capture is not None:
                    capture.set_pending_lifecycle_id(None)

            if self.force_close_at_end and broker.position is not None and ordered:
                final = ordered[-1]
                # Conservative: liquidate at last close synthetic quote only when forced
                # and explicitly requested (smoke fixtures may enable).
                close_time = parse_utc(final.close_time) + timedelta(milliseconds=1)
                if close_time < self.research_cutoff:
                    health = synthetic_market_health(
                        mid=final.close,
                        spread_fraction=settings.spread_fraction,
                        quote_time=parse_utc(final.close_time),
                        decision_or_fill_time=close_time,
                        price_basis="forced_end_close",
                    )
                    exit_decision = StrategyDecision(
                        action="exit",
                        strategy=broker.position.strategy,
                        direction=broker.position.direction,
                        timestamp=close_time,
                        reason="End of fixture; forced paper liquidation.",
                        inputs={"exit_price": final.close},
                    )
                    broker.process(exit_decision, market_health=health)
                    if capture is not None and self._flush_journal is not None:
                        self._flush_journal()
                        closed = self._closed_trades()
                        if closed:
                            trade = closed[-1]
                            self._emit(
                                "position_closed",
                                event_time=close_time,
                                candle_open_time=final.open_time,
                                signal_time=close_time,
                                signal_price=float(final.close),
                                payload={
                                    "forced_end_close": True,
                                    "entry_timestamp": trade.get("entry_timestamp"),
                                    "exit_timestamp": trade.get("exit_timestamp"),
                                    "entry_fill_price": trade.get("entry_fill_price"),
                                    "exit_fill_price": trade.get("exit_fill_price"),
                                    "quantity_btc": trade.get("quantity_btc"),
                                    "gross_pnl": trade.get("gross_pnl"),
                                    "net_realized_pnl": trade.get("net_realized_pnl"),
                                    "total_costs": trade.get("total_costs"),
                                    "reason": trade.get("reason"),
                                    "direction": trade.get("direction"),
                                },
                            )

            if self._flush_journal is not None:
                self._flush_journal()

            trades = self._closed_trades()
        except BaseException as error:
            failed = error
            raise
        finally:
            if capture is not None:
                try:
                    if failed is None:
                        # Flush prior events first so the terminal marker is not
                        # batched with unrecovered earlier payloads.
                        capture.flush()
                        capture.emit(
                            "run_completed",
                            event_time=(
                                parse_utc(ordered[-1].close_time) if ordered else None
                            ),
                            payload={
                                "candles_processed": candles_processed,
                                "entries_accepted": entries_accepted,
                                "entries_rejected": entries_rejected,
                                "fills_cancelled_incompatible": (
                                    fills_cancelled_incompatible
                                ),
                                "fills_cancelled_missing_next": (
                                    fills_cancelled_missing_next
                                ),
                                "config_fingerprint": self.config_fingerprint,
                                "dataset_manifest_fingerprint": (
                                    self.dataset_manifest_fingerprint
                                ),
                                "research_cutoff": _iso(self.research_cutoff),
                                "event_counts_so_far": capture.event_counts,
                                # Payload claim is only valid after the flush below
                                # succeeds; writer.persistence_complete is authoritative.
                                "persistence_complete": True,
                            },
                        )
                        # Establish durability of run_completed before sealing.
                        capture.flush()
                        if capture.persistence_uncertain or capture.buffer_len:
                            raise RuntimeError(
                                "Research event run_completed marker was not durably "
                                "persisted; refusing to mark the run complete."
                            )
                        capture.close(mark_complete=True)
                    else:
                        try:
                            capture.emit(
                                "run_failed",
                                event_time=None,
                                payload={
                                    "error_type": type(failed).__name__,
                                    "error": str(failed),
                                    "candles_processed": candles_processed,
                                    "persistence_complete": False,
                                },
                            )
                            if not capture.persistence_uncertain:
                                capture.flush()
                        finally:
                            if not capture.closed:
                                capture.close(mark_complete=False)
                except Exception:
                    # Fail closed: do not leave an open handle after a failed run.
                    if capture is not None and not capture.closed:
                        if capture.persistence_uncertain:
                            capture.close(mark_complete=False)
                        else:
                            capture.abandon()
                    if failed is None:
                        raise

        if capture is not None and not capture.persistence_complete:
            raise RuntimeError(
                "Research event persistence incomplete; refusing to report a complete run."
            )

        summary = {
            "path": "research_historical_candle_next_open",
            "provenance": PROVENANCE,
            "execution_model": EXECUTION_MODEL,
            "execution_notes": EXECUTION_NOTES,
            "strategy": self.strategy_name,
            "journal_path": str(self.journal_path),
            "research_cutoff": _iso(self.research_cutoff),
            "cash": broker.cash,
            "equity": (
                broker.equity
                if broker.position is None or broker.last_bid is not None
                else broker.cash
            ),
            "open_position": broker.position is not None,
            "unresolved_positions": int(broker.position is not None),
            "trades": trades,
            "trade_count": len(trades),
            "events": list(self._events),
            "candles_processed": candles_processed,
            "decision_counts": dict(decision_counts),
            "rejection_reasons": dict(rejection_reasons),
            "entries_accepted": entries_accepted,
            "entries_rejected": entries_rejected,
            "fills_deferred": fills_deferred,
            "fills_executed": fills_executed,
            "fills_cancelled_missing_next": fills_cancelled_missing_next,
            "fills_cancelled_incompatible": fills_cancelled_incompatible,
            "missing_intervals": missing_intervals,
            "look_ahead_rejections": look_ahead_rejections,
            "synthetic_quotes_only": True,
            "journal_mark_to_market": self.journal_mark_to_market,
            "assumptions": [
                SYNTHETIC_QUOTE_REASON,
                EXECUTION_NOTES,
                "No aggregate-trade fill simulator.",
                "No sealed-holdout observations loaded.",
                "Research-lab buffered PaperBroker journal writes (flush at end / batch).",
                (
                    "mark_to_market journal rows omitted (in-memory marks retained)."
                    if not self.journal_mark_to_market
                    else "mark_to_market journal rows retained."
                ),
                "Trailing evaluate bar window (lookback/hold sized); full-history O(n²) validation avoided.",
                f"fee_rate_per_side={settings.fee_rate_per_side}",
                f"slippage_rate_per_side={settings.effective_slippage_rate_per_side}",
                f"spread_fraction={settings.spread_fraction}",
            ],
        }
        if capture is not None:
            summary["research_event_capture"] = {
                **capture.status_payload(),
                "persistence_complete": capture.persistence_complete,
            }
        spec = getattr(strategy, "spec", None)
        if spec is not None:
            summary["strategy_spec"] = {
                "name": spec.name,
                "warmup_bars": spec.warmup_bars,
                "max_holding_bars": spec.max_holding_bars,
                "hypothesis_family": spec.hypothesis_family,
            }
        if isinstance(strategy, ProductionStrategyAdapter) or hasattr(strategy, "name"):
            summary["strategy_object_type"] = type(strategy).__name__
        return summary

    def _closed_trades(self) -> list[dict[str, object]]:
        if not self.journal_path.exists():
            return []
        closed: list[dict[str, object]] = []
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") == "trade_closed":
                closed.append(event["details"])
        return closed


def fingerprint_journal(path: Path) -> str:
    import hashlib

    payload = journal_trade_fingerprint(path)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def run_formal_historical_replay(
    *,
    candles: CutoffValidatedCandles,
    journal_path: Path,
    strategy_name: str = "momentum_breakout",
    settings: BacktestSettings | None = None,
    strategy_object: object | None = None,
    force_close_at_end: bool = False,
) -> dict[str, object]:
    """Formal historical scoring entrypoint (not authorized to run full scores here).

    Rejects force_close_at_end=True and requires cutoff-validated candle bundles.
    Does not invent end-of-sample fills; unresolved positions stay open in the result.
    """

    if force_close_at_end:
        raise ValueError(
            "Formal historical scoring rejects force_close_at_end=True; "
            "do not silently liquidate at the final candle close."
        )
    if not isinstance(candles, CutoffValidatedCandles):
        raise TypeError(
            "Formal historical scoring requires CutoffValidatedCandles from "
            "load_formal_research_candles / bind_historical_dataset(load_candles=True). "
            "Raw iter_klines_from_csv output is not accepted."
        )
    if candles.source not in {"load_klines", "bind_historical_dataset"}:
        # load_formal uses load_klines; allow explicit formal sources only.
        if not candles.source.startswith("load_klines"):
            raise ValueError(
                f"Unsupported formal candle source {candles.source!r}; "
                "use the cutoff-enforcing catalog loader."
            )

    replay = HistoricalCandleReplay(
        candles=candles.candles,
        journal_path=journal_path,
        strategy_name=strategy_name,
        settings=settings or BacktestSettings(candle_interval_seconds=60),
        strategy_object=strategy_object,
        research_cutoff=candles.research_cutoff,
        force_close_at_end=False,
        retain_verbose_events=False,
        journal_mark_to_market=False,
    )
    result = replay.run()
    result["formal_scoring_entrypoint"] = True
    result["force_close_at_end"] = False
    result["candle_source"] = candles.source
    result["excluded_at_or_after_cutoff"] = candles.excluded_at_or_after_cutoff
    result["completed_scored_trades"] = list(result.get("trades") or [])
    result["unresolved_positions"] = int(result.get("unresolved_positions") or 0)
    result["unresolved_reported_separately"] = True
    result["execution_model_limitations"] = list(result.get("assumptions") or [])
    return result
