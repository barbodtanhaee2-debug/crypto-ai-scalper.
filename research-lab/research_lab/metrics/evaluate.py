"""After-cost research metrics. Gross profit and win rate are not ranking keys."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Sequence


MIN_CLOSED_TRADES_TO_RANK = 30


def _float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def experiment_metrics(
    result: Mapping[str, object],
    *,
    initial_equity: float = 100.0,
    min_closed_trades_to_rank: int = MIN_CLOSED_TRADES_TO_RANK,
) -> dict[str, object]:
    trades = list(result.get("trades") or [])
    nets = [_float(row.get("net_realized_pnl")) for row in trades]
    gross = [_float(row.get("gross_pnl")) for row in trades]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value < 0]
    fees = sum(
        _float(row.get("entry_fee")) + _float(row.get("exit_fee")) for row in trades
    )
    spread = sum(
        _float(row.get("entry_spread_cost_estimate"))
        + _float(row.get("exit_spread_cost_estimate"))
        for row in trades
    )
    slippage = sum(
        _float(row.get("entry_slippage_cost_estimate"))
        + _float(row.get("exit_slippage_cost_estimate"))
        for row in trades
    )
    hold_seconds: list[float] = []
    for row in trades:
        start = _parse(row.get("entry_timestamp"))
        end = _parse(row.get("exit_timestamp"))
        if start is not None and end is not None:
            hold_seconds.append((end - start).total_seconds())
    final_equity = _float(result.get("equity"), initial_equity)
    net_pnl = sum(nets)
    gross_wins = sum(value for value in gross if value > 0)
    gross_losses = abs(sum(value for value in gross if value < 0))
    profit_factor = (
        (sum(wins) / abs(sum(losses))) if wins and losses else None
    )
    expectancy = (sum(nets) / len(nets)) if nets else None
    drawdown = _max_drawdown_from_result(result, initial_equity)
    candles = int(result.get("candles_processed") or 0)
    closed = len(trades)
    sample_ok = closed >= min_closed_trades_to_rank
    return {
        "net_profit_and_loss": net_pnl,
        "net_return_percentage": (final_equity / initial_equity - 1.0) * 100.0,
        "completed_trades": closed,
        "win_rate": (len(wins) / closed) if closed else None,
        "average_winning_trade": (sum(wins) / len(wins)) if wins else None,
        "average_losing_trade": (sum(losses) / len(losses)) if losses else None,
        "profit_factor": profit_factor,
        "expectancy_per_trade": expectancy,
        "maximum_drawdown": drawdown,
        "average_holding_seconds": (
            sum(hold_seconds) / len(hold_seconds) if hold_seconds else None
        ),
        "fees_paid": fees,
        "estimated_spread_costs": spread,
        "estimated_slippage": slippage,
        "rejected_entries": int(result.get("entries_rejected") or 0),
        "unresolved_exits": int(result.get("unresolved_positions") or 0),
        "exposure_time_seconds": sum(hold_seconds),
        "candles_processed": candles,
        "stale_candle_decisions": int(result.get("stale_candle_decisions") or 0),
        "stale_protective": int(result.get("stale_protective") or 0),
        "final_equity": final_equity,
        "insufficient_sample": not sample_ok,
        "min_closed_trades_to_rank": min_closed_trades_to_rank,
        "ranking_allowed": sample_ok,
        "ranking_note": (
            "Sample is large enough to rank after costs."
            if sample_ok
            else "Insufficient closed trades; do not rank this strategy."
        ),
        "primary_score": "risk_adjusted_after_costs_not_gross_or_win_rate",
    }


def _max_drawdown_from_result(
    result: Mapping[str, object], initial_equity: float
) -> float | None:
    events = result.get("events")
    if not isinstance(events, Sequence):
        equity = _float(result.get("equity"), initial_equity)
        if initial_equity <= 0:
            return None
        return max(0.0, (initial_equity - equity) / initial_equity)
    peak = initial_equity
    max_dd = 0.0
    # Research replay journals mark_to_market on the broker; closed-trade cash
    # is not used as equity (Stage 2.4 lesson). Here we only have the final
    # equity plus trade nets as a conservative path.
    equity = initial_equity
    for trade in result.get("trades") or []:
        equity += _float(trade.get("net_realized_pnl"))
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    final = _float(result.get("equity"), equity)
    peak = max(peak, initial_equity, final)
    if peak > 0:
        max_dd = max(max_dd, (peak - final) / peak)
    return max_dd
