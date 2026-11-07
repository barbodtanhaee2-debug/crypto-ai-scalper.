"""Keep session-start history; drop the chronological test holdout from replay."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from quote_tape_dataset import QuoteTapeDataset

from research_lab.backtesting.splits import ChronologicalSplit


def parse_aware(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def dataset_excluding_holdout(
    dataset: QuoteTapeDataset, split: ChronologicalSplit
) -> QuoteTapeDataset:
    """Candles from session start until test_start (exclusive). Warmup history kept."""
    holdout = split.test_start
    candles = tuple(
        candle for candle in dataset.candles if candle.close_time < holdout
    )
    if not candles:
        return replace(dataset, quotes=(), candles=())
    cutoff = max(candle.received_at for candle in candles)
    quotes = tuple(quote for quote in dataset.quotes if quote.received_at <= cutoff)
    return replace(dataset, quotes=quotes, candles=candles)


def in_score_window(timestamp: datetime, split: ChronologicalSplit) -> bool:
    return timestamp < split.test_start


def trade_touches_holdout(trade: dict[str, object], split: ChronologicalSplit) -> bool:
    entry = parse_aware(trade.get("entry_timestamp"))
    exit_at = parse_aware(trade.get("exit_timestamp"))
    if entry is not None and not in_score_window(entry, split):
        return True
    if exit_at is not None and not in_score_window(exit_at, split):
        return True
    return False


def scored_trades(
    trades: list[dict[str, object]], split: ChronologicalSplit
) -> list[dict[str, object]]:
    """Drop holdout trades without using their P&L. Entry and exit must both be before test_start."""
    kept: list[dict[str, object]] = []
    for trade in trades:
        if trade_touches_holdout(trade, split):
            continue
        kept.append(trade)
    return kept


def assert_no_holdout_metrics(
    trades: list[dict[str, object]], split: ChronologicalSplit
) -> None:
    leaked = [trade for trade in trades if trade_touches_holdout(trade, split)]
    if leaked:
        raise ValueError(
            f"Holdout period contains {len(leaked)} trade(s); "
            "Stage 3.2.1 must not score the final 25% window."
        )
