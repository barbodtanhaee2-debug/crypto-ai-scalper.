"""Read-only parsers and summaries for paper-broker and backtest files."""

from __future__ import annotations

import csv
import json
import math
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class JournalData:
    exists: bool
    events: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeStatusData:
    exists: bool
    data: dict[str, Any] | None
    process_running: bool | None
    heartbeat_age_seconds: float | None
    errors: tuple[str, ...]


@dataclass
class _JournalCacheEntry:
    identity: tuple[int, int]
    signature: tuple[int, int, int, int, int]
    observed_size: int
    offset: int = 0
    next_line_number: int = 1
    events: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pending_line: bool = False
    pending_bytes: bytes = b""
    verified_tail: bytes = b""
    data: JournalData | None = None


_journal_cache: dict[str, _JournalCacheEntry] = {}
_journal_summary_cache: dict[str, tuple[JournalData, dict[str, Any]]] = {}
_journal_cache_lock = threading.RLock()


def runtime_status_label(status: RuntimeStatusData) -> str:
    """Describe whether the runtime heartbeat is fresh, stale, or stopped."""
    if status.process_running is True:
        return "Running (fresh heartbeat)"
    if status.process_running is False:
        if any("heartbeat" in error.lower() for error in status.errors):
            return "Stale or invalid heartbeat"
        return "Stopped"
    return "Unavailable"


def runtime_health_label(status: RuntimeStatusData) -> str:
    """Classify feed and candle readiness only while the heartbeat is fresh."""
    heartbeat_label = runtime_status_label(status)
    if heartbeat_label != "Running (fresh heartbeat)":
        return heartbeat_label

    data = status.data or {}
    quote_status = data.get("connection_status")
    if quote_status != "connected":
        return (
            "Disconnected or unhealthy quote feed"
            if isinstance(quote_status, str) else "Readiness unavailable"
        )
    if data.get("market_data_healthy") is not True:
        return "Stale or unhealthy quote feed"

    diagnostics = data.get("entry_diagnostics")
    completed = (
        _finite_number(diagnostics.get("completed_candle_count"))
        if isinstance(diagnostics, dict) else None
    )
    required = (
        _finite_number(diagnostics.get("required_completed_candle_count"))
        if isinstance(diagnostics, dict) else None
    )
    if completed is None or required is None or required < 1:
        return "Readiness unavailable"
    candle_status = data.get("candle_connection_status")
    if candle_status != "connected":
        return (
            "Disconnected or unhealthy candle feed"
            if isinstance(candle_status, str) else "Readiness unavailable"
        )
    if data.get("latest_completed_candle_timestamp") is not None:
        candle_age = _finite_number(data.get("candle_data_age_seconds"))
        candle_limit = _finite_number(data.get("candle_data_stale_limit_seconds"))
        if (
            candle_age is None
            or candle_limit is None
            or candle_age < 0
            or candle_age > candle_limit
        ):
            return "Stale candle data"
    if completed < required:
        return "Warming up candles"
    if data.get("candle_data_healthy") is not True:
        return "Stale or unhealthy candle feed"
    if data.get("candle_fresh") is not True:
        return "Stale candle data"
    if (
        not isinstance(diagnostics, dict)
        or diagnostics.get("candle_history_contiguous") is not True
        or diagnostics.get("strategy_warmup_ready") is not True
    ):
        return "Candle readiness unavailable"
    return "Healthy (quote and candle feeds ready)"


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid non-finite JSON value: {value}.")


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def load_runtime_status(
    path: str | Path,
    *,
    now: datetime | None = None,
    stale_after_seconds: float = 5.0,
) -> RuntimeStatusData:
    """Read a runtime record and mask live values when its heartbeat is stale."""
    status_path = Path(path)
    if not status_path.exists():
        return RuntimeStatusData(
            False, None, None, None, ("Runtime status file is unavailable.",)
        )
    try:
        with status_path.open("r", encoding="utf-8") as source:
            record = json.load(source, parse_constant=_reject_json_constant)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return RuntimeStatusData(
            True, None, None, None, (f"Runtime status could not be read: {error}",)
        )
    if not isinstance(record, dict):
        return RuntimeStatusData(
            True, None, None, None, ("Runtime status must be a JSON object.",)
        )
    if not isinstance(stale_after_seconds, (int, float)) or (
        isinstance(stale_after_seconds, bool)
        or not math.isfinite(stale_after_seconds)
        or stale_after_seconds <= 0
    ):
        raise ValueError("stale_after_seconds must be finite and positive.")
    observed_at = now or datetime.now(timezone.utc)
    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise ValueError("now must be timezone-aware.")

    errors = []
    heartbeat = _parse_datetime(record.get("process_heartbeat"))
    age = (
        (observed_at.astimezone(timezone.utc) - heartbeat).total_seconds()
        if heartbeat is not None else None
    )
    recorded_running = record.get("process_running")
    if not isinstance(recorded_running, bool):
        errors.append("Runtime process state is unavailable.")
        running = None
    elif not recorded_running:
        running = False
    elif heartbeat is None:
        errors.append("Running state has no valid heartbeat.")
        running = False
    elif age is None or age < 0 or age > stale_after_seconds:
        errors.append("Runtime heartbeat is stale or future-dated.")
        running = False
    else:
        running = True

    clean = dict(record)
    clean["process_running"] = running
    clean["heartbeat_age_seconds"] = age if running else None
    recorded_market_timestamp = _parse_datetime(
        record.get("latest_valid_market_data_timestamp")
    )
    clean["last_recorded_market_data_timestamp"] = (
        recorded_market_timestamp.isoformat()
        if recorded_market_timestamp is not None else None
    )
    for key in (
        "market_data_healthy",
        "candle_data_healthy",
        "entry_permission",
        "paper_only",
        "risk_lockout",
        "open_position",
        "journal_ok",
        "status_write_ok",
        "candle_fresh",
    ):
        if not isinstance(clean.get(key), bool):
            clean[key] = None
        elif running is not True and key != "paper_only":
            clean[key] = (
                False if key == "entry_permission" and running is False else None
            )
    if running is not True:
        clean["connection_status"] = None
        clean["candle_connection_status"] = None
        clean["candle_data_age_seconds"] = None
        clean["candle_data_error"] = None
        clean["latest_valid_market_data_timestamp"] = None
        clean["market_data_age_seconds"] = None
        clean["spread"] = None
        for key in (
            "websocket_messages_received",
            "websocket_messages_accepted",
            "websocket_messages_rejected",
            "last_websocket_message_timestamp",
            "last_websocket_message_accepted",
            "last_market_data_error",
            "latest_processed_market_data_timestamp",
            "latest_completed_candle_timestamp",
            "candle_interval_seconds",
            "candle_interval",
            "candle_source",
            "exit_pending",
            "entry_permission_reason",
            "entry_diagnostics",
            "last_decision",
            "last_decision_reason",
            "runtime_reason",
        ):
            clean[key] = None
        for key in (
            "daily_loss_fraction",
            "drawdown_fraction",
            "daily_loss_limit_fraction",
            "max_drawdown_fraction",
        ):
            clean[key] = None
    else:
        observed_utc = observed_at.astimezone(timezone.utc)
        reported_entry_permission = clean.get("entry_permission") is True
        if not isinstance(clean.get("connection_status"), str):
            clean["connection_status"] = None
        if not isinstance(clean.get("candle_connection_status"), str):
            clean["candle_connection_status"] = None
        if not isinstance(clean.get("candle_data_healthy"), bool):
            clean["candle_data_healthy"] = None
        market_timestamp = _parse_datetime(
            clean.get("latest_valid_market_data_timestamp")
        )
        if market_timestamp is None:
            clean["latest_valid_market_data_timestamp"] = None
            clean["market_data_healthy"] = None
        for key in (
            "market_data_stale_limit_seconds",
            "spread",
            "candle_data_stale_limit_seconds",
        ):
            clean[key] = _finite_number(clean.get(key))
        data_age = (
            (observed_utc - market_timestamp).total_seconds()
            if market_timestamp is not None else None
        )
        clean["market_data_age_seconds"] = data_age
        stale_limit = clean["market_data_stale_limit_seconds"]
        valid_spread = (
            clean["spread"] is not None and clean["spread"] > 0
        )
        candle_timestamp = _parse_datetime(
            clean.get("latest_completed_candle_timestamp")
        )
        candle_age = (
            (observed_utc - candle_timestamp).total_seconds()
            if candle_timestamp is not None else None
        )
        clean["candle_data_age_seconds"] = candle_age
        candle_stale_limit = clean["candle_data_stale_limit_seconds"]
        candle_fresh = (
            candle_age is not None
            and candle_stale_limit is not None
            and 0 <= candle_age <= candle_stale_limit
        )
        clean["candle_fresh"] = (
            clean.get("candle_fresh") is True and candle_fresh
        )
        if clean.get("candle_data_healthy") is True and not candle_fresh:
            clean["candle_data_healthy"] = False
        feed_valid = (
            clean.get("connection_status") == "connected"
            and clean.get("market_data_healthy") is True
            and market_timestamp is not None
            and data_age is not None
            and stale_limit is not None
            and 0 <= data_age <= stale_limit
            and valid_spread
            and clean.get("paper_only") is True
            and clean.get("candle_fresh") is True
            and clean.get("candle_connection_status") == "connected"
            and clean.get("candle_data_healthy") is True
            and clean.get("spread") is not None
            and clean.get("spread") > 0
            and clean.get("open_position") is False
            and clean.get("risk_lockout") is False
            and clean.get("journal_ok") is True
            and clean.get("status_write_ok") is True
        )
        if clean.get("market_data_healthy") is True and not (
            clean.get("connection_status") == "connected"
            and market_timestamp is not None
            and data_age is not None
            and stale_limit is not None
            and 0 <= data_age <= stale_limit
            and valid_spread
        ):
            clean["market_data_healthy"] = False
            feed_valid = False
        if clean.get("entry_permission") is True and not feed_valid:
            clean["entry_permission"] = False
        diagnostics = clean.get("entry_diagnostics")
        completed = (
            _finite_number(diagnostics.get("completed_candle_count"))
            if isinstance(diagnostics, dict) else None
        )
        required = (
            _finite_number(diagnostics.get("required_completed_candle_count"))
            if isinstance(diagnostics, dict) else None
        )
        if (
            diagnostics is None
            or not isinstance(diagnostics, dict)
            or completed is None
            or required is None
            or required < 1
            or completed < required
            or diagnostics.get("candle_history_contiguous") is not True
            or diagnostics.get("strategy_warmup_ready") is not True
            or clean.get("candle_fresh") is not True
        ):
            clean["entry_permission"] = False
        pending_exit = clean.get("exit_pending")
        if pending_exit is not None and not isinstance(pending_exit, dict):
            errors.append("Runtime exit-pending state is malformed.")
            clean["exit_pending"] = None
            clean["entry_permission"] = False
        elif pending_exit is not None:
            clean["entry_permission"] = False
        if reported_entry_permission and clean.get("entry_permission") is False:
            if pending_exit is not None and isinstance(pending_exit, dict):
                reason = (
                    "Protective exit is pending fresh-quote verification; "
                    "new entries remain blocked."
                )
            elif pending_exit is not None:
                reason = "Runtime exit-pending state is malformed; entries are blocked."
            elif clean.get("connection_status") != "connected":
                reason = "Quote feed is not connected; entries are blocked."
            elif (
                data_age is None
                or stale_limit is None
                or data_age < 0
                or data_age > stale_limit
                or clean.get("market_data_healthy") is not True
            ):
                reason = "Latest quote is stale or unhealthy; entries are blocked."
            elif clean.get("candle_connection_status") != "connected":
                reason = "Kline feed is not connected; entries are blocked."
            elif (
                candle_age is None
                or candle_stale_limit is None
                or candle_age < 0
                or candle_age > candle_stale_limit
            ):
                reason = "Latest completed candle is stale or future-dated."
            elif clean.get("candle_data_healthy") is not True:
                reason = str(
                    clean.get("candle_data_error")
                    or "Kline feed has not validated a completed candle."
                )
            elif (
                completed is None
                or required is None
                or required < 1
                or completed < required
                or not isinstance(diagnostics, dict)
                or diagnostics.get("candle_history_contiguous") is not True
                or diagnostics.get("strategy_warmup_ready") is not True
                or clean.get("candle_fresh") is not True
            ):
                reason = "Candle warm-up or readiness validation failed."
            elif clean.get("open_position") is not False:
                reason = "An open paper position blocks new entries."
            elif clean.get("risk_lockout") is not False:
                reason = "Paper-broker risk status blocks new entries."
            elif clean.get("journal_ok") is not True:
                reason = "Broker journal state is uncertain; entries are blocked."
            elif clean.get("status_write_ok") is not True:
                reason = "Runtime status persistence is unavailable."
            elif clean.get("paper_only") is not True:
                reason = "Paper-only safety status is invalid."
            else:
                reason = "Runtime readiness checks failed; entries are blocked."
            clean["entry_permission_reason"] = reason
            if isinstance(diagnostics, dict):
                blockers = diagnostics.get("readiness_blockers")
                if isinstance(blockers, list) and reason not in blockers:
                    blockers.append(reason)
        for key in (
            "daily_loss_fraction",
            "drawdown_fraction",
            "daily_loss_limit_fraction",
            "max_drawdown_fraction",
        ):
            clean[key] = _finite_number(clean.get(key))
    return RuntimeStatusData(
        True, clean, running, age if running is True else None, tuple(errors)
    )


def _journal_file_signature(
    stat_result: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def _append_journal_line(
    raw_line: bytes,
    line_number: int,
    events: list[dict[str, Any]],
    errors: list[str],
) -> None:
    line = raw_line.removesuffix(b"\n").removesuffix(b"\r")
    if not line.strip():
        return
    try:
        decoded = line.decode("utf-8")
        event = json.loads(decoded, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        errors.append(f"Line {line_number}: malformed JSON ({error}).")
        return
    if (
        not isinstance(event, dict)
        or not isinstance(event.get("event"), str)
        or _parse_datetime(event.get("timestamp")) is None
        or not isinstance(event.get("details"), dict)
        or not isinstance(event.get("state"), dict)
    ):
        errors.append(f"Line {line_number}: invalid journal record shape.")
        return
    events.append(event)


def load_broker_journal(path: str | Path) -> JournalData:
    """Read complete JSONL records, incrementally for append-only journal growth."""
    journal_path = Path(path)
    try:
        cache_key = str(journal_path.resolve())
        with journal_path.open("rb") as source:
            file_stat = os.fstat(source.fileno())
            signature = _journal_file_signature(file_stat)
            identity = signature[:2]
            with _journal_cache_lock:
                cached = _journal_cache.get(cache_key)
                if cached is not None and cached.signature == signature:
                    return cached.data or JournalData(
                        True, (), ("Journal cache is unavailable.",)
                    )

                append_only = (
                    cached is not None
                    and cached.identity == identity
                    and signature[2] > cached.observed_size
                )
                if append_only:
                    entry = cached
                    tail_start = entry.offset - len(entry.verified_tail)
                    source.seek(tail_start)
                    unchanged_tail = (
                        source.read(len(entry.verified_tail)) == entry.verified_tail
                    )
                    source.seek(entry.offset)
                    unchanged_pending = (
                        source.read(entry.observed_size - entry.offset)
                        == entry.pending_bytes
                    )
                    append_only = unchanged_tail and unchanged_pending
                    if not append_only:
                        entry = _JournalCacheEntry(
                            identity=identity,
                            signature=signature,
                            observed_size=0,
                        )
                else:
                    entry = _JournalCacheEntry(
                        identity=identity,
                        signature=signature,
                        observed_size=0,
                    )

                if entry.offset > file_stat.st_size:
                    entry = _JournalCacheEntry(
                        identity=identity,
                        signature=signature,
                        observed_size=0,
                    )
                source.seek(entry.offset)
                added_bytes = source.read(file_stat.st_size - entry.offset)
                complete_length = added_bytes.rfind(b"\n") + 1
                complete_data = added_bytes[:complete_length]
                if complete_data:
                    entry.verified_tail = (
                        entry.verified_tail + complete_data
                    )[-4096:]
                for raw_line in complete_data.split(b"\n")[:-1]:
                    raw_line += b"\n"
                    _append_journal_line(
                        raw_line,
                        entry.next_line_number,
                        entry.events,
                        entry.errors,
                    )
                    entry.next_line_number += 1
                entry.offset += complete_length
                entry.identity = identity
                entry.signature = signature
                entry.observed_size = file_stat.st_size
                entry.pending_line = len(added_bytes) > complete_length
                entry.pending_bytes = added_bytes[complete_length:]

                errors = list(entry.errors)
                if entry.pending_line:
                    errors.append(
                        f"Line {entry.next_line_number}: incomplete final journal "
                        "record is excluded until its newline is written."
                    )
                if not entry.events and not errors:
                    errors.append("Journal file is empty.")
                entry.data = JournalData(
                    True,
                    tuple(entry.events),
                    tuple(errors),
                )
                _journal_cache[cache_key] = entry
                _journal_summary_cache.pop(cache_key, None)
                while len(_journal_cache) > 8:
                    oldest_key = next(iter(_journal_cache))
                    _journal_cache.pop(oldest_key)
                    _journal_summary_cache.pop(oldest_key, None)
                return entry.data
    except FileNotFoundError:
        with _journal_cache_lock:
            _journal_cache.pop(str(journal_path.resolve()), None)
            _journal_summary_cache.pop(str(journal_path.resolve()), None)
        return JournalData(False, (), ("Journal file is unavailable.",))
    except OSError as error:
        return JournalData(True, (), (f"Could not read journal: {error}",))


def _equity_from_state(state: dict[str, Any]) -> float | None:
    cash = _finite_number(state.get("cash"))
    position = state.get("position")
    if cash is None:
        return None
    if position is None:
        return cash
    if not isinstance(position, dict):
        return None
    quantity = _finite_number(position.get("quantity_btc"))
    bid = _finite_number(state.get("last_bid"))
    ask = _finite_number(state.get("last_ask"))
    direction = position.get("direction")
    if quantity is None or bid is None or ask is None or quantity <= 0:
        return None
    if direction == "long":
        return cash + quantity * bid
    return None


def summarize_journal(journal: JournalData) -> dict[str, Any]:
    """Summarize only fields present in saved broker journal events."""
    events = journal.events
    errors = list(journal.errors)
    unsupported_index = next(
        (
            index
            for index, event in enumerate(events)
            if event.get("details", {}).get("direction") == "short"
            or (
                isinstance(event.get("state", {}).get("position"), dict)
                and event["state"]["position"].get("direction") == "short"
            )
        ),
        None,
    )
    if unsupported_index is not None:
        events = events[:unsupported_index]
        errors.append(
            "Journal contains unsupported short-position data; Spot summary stops "
            f"before journal event {unsupported_index + 1}."
        )
    latest = events[-1] if events else None
    state = latest.get("state", {}) if latest else {}
    if not isinstance(state, dict):
        state = {}
    initial_cash = _finite_number(state.get("initial_cash"))
    cash = _finite_number(state.get("cash"))
    realized = _finite_number(state.get("realized_pnl"))
    equity = _equity_from_state(state)
    position = state.get("position")
    if not isinstance(position, dict):
        position = None
    unrealized = None
    if position is None:
        unrealized = 0.0 if latest is not None else None
    else:
        quantity = _finite_number(position.get("quantity_btc"))
        entry = _finite_number(position.get("entry_price"))
        entry_fee = _finite_number(position.get("entry_fee"))
        bid = _finite_number(state.get("last_bid"))
        ask = _finite_number(state.get("last_ask"))
        if None not in (quantity, entry, entry_fee, bid, ask):
            if position.get("direction") == "long":
                unrealized = (bid - entry) * quantity - entry_fee

    closed_trades = [
        event["details"]
        for event in events
        if event["event"] == "trade_closed"
    ]
    fees = []
    fees_complete = True
    for trade in closed_trades:
        entry_fee = _finite_number(trade.get("entry_fee"))
        exit_fee = _finite_number(trade.get("exit_fee"))
        if entry_fee is None or exit_fee is None:
            fees_complete = False
        else:
            fees.append(entry_fee + exit_fee)
    if position is not None:
        open_fee = _finite_number(position.get("entry_fee"))
        if open_fee is None:
            fees_complete = False
        else:
            fees.append(open_fee)
    total_fees = sum(fees) if fees_complete and latest is not None else None

    rejection_events = [
        {
            "timestamp": event["timestamp"],
            "type": event["event"],
            "strategy": event["details"].get("strategy"),
            "reason": event["details"].get("reason", "Reason not recorded."),
        }
        for event in journal.events
        if event["event"] in (
            "risk_rejection",
            "strategy_rejection",
            "valuation_rejection",
        )
    ]
    trade_history = []
    for trade in closed_trades:
        trade_history.append({
            "entry_timestamp": trade.get("entry_timestamp"),
            "exit_timestamp": trade.get("exit_timestamp"),
            "strategy": trade.get("strategy"),
            "direction": trade.get("direction"),
            "entry_fill_price": trade.get("entry_fill_price"),
            "exit_fill_price": trade.get("exit_fill_price"),
            "quantity_btc": trade.get("quantity_btc"),
            "entry_fee": trade.get("entry_fee"),
            "exit_fee": trade.get("exit_fee"),
            "total_costs": trade.get("total_costs"),
            "net_realized_pnl": trade.get("net_realized_pnl"),
            "reason": trade.get("reason"),
        })
    curve = []
    for event in events:
        event_equity = _equity_from_state(event["state"])
        if event_equity is not None:
            curve.append({"timestamp": event["timestamp"], "equity": event_equity})
    if curve and initial_cash is not None:
        curve.insert(
            0,
            {"timestamp": curve[0]["timestamp"], "equity": initial_cash},
        )
    drawdown = None
    if curve:
        peak = curve[0]["equity"]
        maximum = 0.0
        for point in curve:
            peak = max(peak, point["equity"])
            if peak > 0:
                maximum = max(maximum, (peak - point["equity"]) / peak)
        drawdown = maximum

    wins = [
        _finite_number(trade.get("net_realized_pnl"))
        for trade in closed_trades
    ]
    valid_pnls = [value for value in wins if value is not None]
    profits = sum(value for value in valid_pnls if value > 0)
    losses = sum(-value for value in valid_pnls if value < 0)
    complete_pnls = len(valid_pnls) == len(closed_trades)
    metrics = {
        "trade_count": len(closed_trades),
        "win_rate": (
            sum(value > 0 for value in valid_pnls) / len(valid_pnls)
            if closed_trades and complete_pnls
            else None
        ),
        "profit_factor": profits / losses if complete_pnls and losses > 0 else None,
        "profit_factor_note": (
            "No losing closed trades; profit factor is undefined."
            if closed_trades and complete_pnls and losses == 0
            else None
        ),
        "net_return_fraction": (
            (equity - initial_cash) / initial_cash
            if equity is not None and initial_cash is not None and initial_cash > 0
            else None
        ),
        "maximum_drawdown_fraction": drawdown,
    }
    latest_mark = next(
        (event for event in reversed(events) if event["event"] == "mark_to_market"),
        None,
    )
    mark_details = latest_mark.get("details", {}) if latest_mark else {}
    latest_timestamp = latest_mark.get("timestamp") if latest_mark else None
    quote_state = latest_mark.get("state", state) if latest_mark else state
    if not isinstance(quote_state, dict):
        quote_state = state
    mark_age = None
    mark_time = _parse_datetime(latest_timestamp)
    if mark_time is not None:
        age = (datetime.now(timezone.utc) - mark_time).total_seconds()
        mark_age = age if age >= 0 else None
    daily_loss = _finite_number(mark_details.get("daily_loss_fraction"))
    if daily_loss is None:
        day_start = _finite_number(state.get("day_start_equity"))
        if day_start is not None and day_start > 0 and equity is not None:
            daily_loss = max(0.0, (day_start - equity) / day_start)
    drawdown_state = _finite_number(mark_details.get("drawdown_fraction"))
    if drawdown_state is None:
        peak_equity = _finite_number(state.get("peak_equity"))
        if peak_equity is not None and peak_equity > 0 and equity is not None:
            drawdown_state = max(0.0, (peak_equity - equity) / peak_equity)
    return {
        "account_currency": "USDT",
        "file_exists": journal.exists,
        "record_count": len(events),
        "errors": errors,
        "latest_event_timestamp": latest.get("timestamp") if latest else None,
        "cash": cash,
        "equity": equity,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_fees": total_fees,
        "open_position": position,
        "trade_history": trade_history,
        "rejections": rejection_events,
        "equity_curve": curve,
        "metrics": metrics,
        "market_record": {
            "connection_status": None,
            "entry_allowed": None,
            "last_mark_timestamp": latest_timestamp,
            "seconds_since_last_mark": mark_age,
            "bid": _finite_number(quote_state.get("last_bid")),
            "ask": _finite_number(quote_state.get("last_ask")),
            "spread": (
                _finite_number(quote_state.get("last_ask"))
                - _finite_number(quote_state.get("last_bid"))
                if _finite_number(quote_state.get("last_ask")) is not None
                and _finite_number(quote_state.get("last_bid")) is not None
                else None
            ),
        },
        "risk": {
            "daily_loss_fraction": daily_loss,
            "drawdown_fraction": drawdown_state,
            "peak_equity": _finite_number(state.get("peak_equity")),
            "daily_loss_limit": None,
            "max_drawdown_limit": None,
            "trading_lockout": None,
        },
    }


def load_broker_portfolio(path: str | Path) -> tuple[JournalData, dict[str, Any]]:
    """Reuse a journal summary until a complete journal record is appended."""
    journal = load_broker_journal(path)
    cache_key = str(Path(path).resolve())
    with _journal_cache_lock:
        cached = _journal_summary_cache.get(cache_key)
        if cached is not None and cached[0] is journal:
            summary = cached[1]
        else:
            summary = summarize_journal(journal)
            _journal_summary_cache[cache_key] = (journal, summary)
            while len(_journal_summary_cache) > 8:
                _journal_summary_cache.pop(next(iter(_journal_summary_cache)))

        mark_timestamp = _parse_datetime(
            summary["market_record"].get("last_mark_timestamp")
        )
        summary["market_record"]["seconds_since_last_mark"] = (
            max(
                0.0,
                (datetime.now(timezone.utc) - mark_timestamp).total_seconds(),
            )
            if mark_timestamp is not None else None
        )
        return journal, summary


def load_backtest_period(directory: str | Path) -> dict[str, Any]:
    """Read one saved backtest period without merging it with another period."""
    root = Path(directory)
    result_path = root / "results.json"
    trades_path = root / "trades.csv"
    errors = []
    results = None
    trades = []
    if not result_path.is_file():
        errors.append("results.json is unavailable.")
    else:
        try:
            with result_path.open("r", encoding="utf-8") as source:
                loaded = json.load(source, parse_constant=_reject_json_constant)
            if isinstance(loaded, dict):
                results = loaded
            else:
                errors.append("results.json must contain a JSON object.")
        except (OSError, json.JSONDecodeError, ValueError) as error:
            errors.append(f"Could not read results.json: {error}")
    if not trades_path.is_file():
        errors.append("trades.csv is unavailable.")
    else:
        try:
            with trades_path.open("r", encoding="utf-8-sig", newline="") as source:
                reader = csv.DictReader(source)
                if reader.fieldnames is None:
                    errors.append("trades.csv is empty or has no header.")
                else:
                    required = {
                        "entry_timestamp",
                        "exit_timestamp",
                        "direction",
                        "entry_fill_price",
                        "exit_fill_price",
                        "entry_fee",
                        "exit_fee",
                        "net_realized_pnl",
                    }
                    missing = sorted(required.difference(reader.fieldnames))
                    if missing:
                        errors.append(
                            "trades.csv is missing expected fields: "
                            + ", ".join(missing)
                            + "."
                        )
                    for line_number, row in enumerate(reader, start=2):
                        if not any(value not in (None, "") for value in row.values()):
                            continue
                        if None in row or not all(
                            value is None or isinstance(value, str)
                            for value in row.values()
                        ):
                            errors.append(
                                f"trades.csv line {line_number} has an invalid row shape."
                            )
                            continue
                        direction = row.get("direction")
                        normalized_direction = (
                            direction.strip().casefold()
                            if isinstance(direction, str) else None
                        )
                        if normalized_direction != "long":
                            direction_name = (
                                "short trade"
                                if normalized_direction == "short"
                                else "non-long trade with invalid or unsupported direction"
                            )
                            errors.append(
                                f"trades.csv line {line_number} contains an unsupported "
                                f"{direction_name}; Spot results are excluded."
                            )
                            continue
                        trades.append(dict(row))
        except (OSError, csv.Error, UnicodeError) as error:
            errors.append(f"Could not read trades.csv: {error}")
    if isinstance(results, dict) and "short" in results:
        short_metrics = results["short"]
        short_trade_count = (
            _finite_number(short_metrics.get("trade_count"))
            if isinstance(short_metrics, dict) else None
        )
        if short_trade_count is None or short_trade_count < 0:
            errors.append(
                "results.json contains invalid unsupported-short metrics; "
                "Spot results are excluded."
            )
        elif short_trade_count > 0:
            errors.append(
                "results.json contains unsupported short trades; Spot results are excluded."
            )
    if any("Spot results are excluded." in error for error in errors):
        results = None
    return {
        "directory": str(root),
        "results": results,
        "trades": trades,
        "errors": errors,
    }


def available_metric(results: object, *keys: str) -> object | None:
    """Return a nested saved metric only when every source field is present."""
    current = results
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current
