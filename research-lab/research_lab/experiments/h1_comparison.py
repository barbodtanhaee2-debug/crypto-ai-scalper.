"""H1 overlay (primary) and full-path comparison (secondary)."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence

from research_lab.backtesting.holdout import in_score_window, parse_aware
from research_lab.backtesting.splits import ChronologicalSplit

MATCH_WINDOW_SECONDS = 120.0


def _bar_time(row: Mapping[str, object]) -> datetime | None:
    return parse_aware(row.get("bar_close")) or parse_aware(row.get("timestamp"))


def opportunity_overlay(
    baseline_log: Sequence[Mapping[str, object]],
    candidate_log: Sequence[Mapping[str, object]],
    split: ChronologicalSplit,
) -> list[dict[str, object]]:
    """Match baseline long signals to confirmation outcomes. No occupancy effects."""
    rows: list[dict[str, object]] = []
    for index, signal in enumerate(baseline_log):
        if signal.get("action") != "entry" or signal.get("direction") != "long":
            continue
        bar_time = _bar_time(signal)
        if bar_time is None or not in_score_window(bar_time, split):
            continue
        bar_key = str(signal.get("bar_close") or "")
        pending_index = next(
            (
                cursor
                for cursor, row in enumerate(candidate_log)
                if row.get("event") == "pending_breakout"
                and str(row.get("bar_close") or "") == bar_key
            ),
            None,
        )
        outcome = "no_candidate_pending"
        confirm_row: Mapping[str, object] | None = None
        if pending_index is not None:
            outcome = "pending_unresolved_end_of_window"
            for later in candidate_log[pending_index + 1 :]:
                event = later.get("event")
                if event in {
                    "confirmation_pass",
                    "confirmation_fail",
                    "pending_cancelled_missing_minute",
                }:
                    confirm_row = later
                    outcome = str(event)
                    break
                if event == "pending_breakout":
                    break
        rows.append(
            {
                "index": index,
                "baseline_bar_close": signal.get("bar_close"),
                "baseline_decision_time": signal.get("timestamp"),
                "baseline_reason": signal.get("reason"),
                "prior_high": (signal.get("inputs") or {}).get("prior_high"),
                "close": (signal.get("inputs") or {}).get("close"),
                "bar_available_at_decision": signal.get("bar_available_at_decision"),
                "quote_available_at_decision": signal.get("quote_available_at_decision"),
                "quote_received_at": signal.get("quote_received_at"),
                "candidate_outcome": outcome,
                "candidate_decision_time": (
                    confirm_row.get("timestamp") if confirm_row is not None else None
                ),
                "candidate_reason": (
                    confirm_row.get("reason") if confirm_row is not None else None
                ),
                "candidate_quote_available": (
                    confirm_row.get("quote_available_at_decision")
                    if confirm_row is not None
                    else None
                ),
                "executable_on_quote_path": outcome == "confirmation_pass",
            }
        )
    return rows


def match_full_path_trades(
    baseline_trades: Sequence[Mapping[str, object]],
    candidate_trades: Sequence[Mapping[str, object]],
    split: ChronologicalSplit,
) -> dict[str, object]:
    """Secondary occupancy-aware pairing. Unmatched trades stay unmatched."""
    def _eligible(trade: Mapping[str, object]) -> bool:
        entry = parse_aware(trade.get("entry_timestamp"))
        exit_at = parse_aware(trade.get("exit_timestamp"))
        return (
            entry is not None
            and exit_at is not None
            and in_score_window(entry, split)
            and in_score_window(exit_at, split)
        )

    baseline = [trade for trade in baseline_trades if _eligible(trade)]
    candidate = [trade for trade in candidate_trades if _eligible(trade)]
    used_candidate: set[int] = set()
    matched: list[dict[str, object]] = []
    unmatched_baseline: list[dict[str, object]] = []
    for source in baseline:
        entry = parse_aware(source["entry_timestamp"])
        assert entry is not None
        best_index = None
        best_delta = None
        for index, other in enumerate(candidate):
            if index in used_candidate:
                continue
            other_entry = parse_aware(other["entry_timestamp"])
            if other_entry is None:
                continue
            if other.get("direction") != source.get("direction"):
                continue
            delta = abs((other_entry - entry).total_seconds())
            if delta > MATCH_WINDOW_SECONDS:
                continue
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_index = index
        if best_index is None:
            unmatched_baseline.append(dict(source))
            continue
        used_candidate.add(best_index)
        other = candidate[best_index]
        matched.append(
            {
                "baseline_entry": source.get("entry_timestamp"),
                "candidate_entry": other.get("entry_timestamp"),
                "entry_delay_seconds": best_delta,
                "baseline_exit": source.get("exit_timestamp"),
                "candidate_exit": other.get("exit_timestamp"),
                "baseline_net_pnl": source.get("net_realized_pnl"),
                "candidate_net_pnl": other.get("net_realized_pnl"),
                "baseline_reason": source.get("reason"),
                "candidate_reason": other.get("reason"),
            }
        )
    unmatched_candidate = [
        dict(trade) for index, trade in enumerate(candidate) if index not in used_candidate
    ]
    return {
        "matched": matched,
        "unmatched_baseline": unmatched_baseline,
        "unmatched_candidate": unmatched_candidate,
    }


def economic_classification(baseline_trades: int, candidate_trades: int, minimum: int = 30) -> dict[str, object]:
    inconclusive = baseline_trades < minimum or candidate_trades < minimum
    return {
        "economic_result": "inconclusive" if inconclusive else "not_ranked_in_this_stage",
        "reason": (
            "Either arm has fewer than 30 completed scored trades; "
            "H1 is not an economic ranking result."
            if inconclusive
            else "Sample gate passed; still not a profitability claim."
        ),
        "baseline_completed_scored_trades": baseline_trades,
        "candidate_completed_scored_trades": candidate_trades,
        "min_closed_trades_to_rank": minimum,
    }
