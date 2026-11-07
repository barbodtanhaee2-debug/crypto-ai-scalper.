"""Chronological train / validation / test splits. No shuffling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from quote_tape_dataset import QuoteTapeDataset


@dataclass(frozen=True)
class ChronologicalSplit:
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    test_start: datetime
    test_end: datetime
    train_candles: int
    validation_candles: int
    test_candles: int


def chronological_split(
    dataset: QuoteTapeDataset,
    *,
    train_fraction: float = 0.5,
    validation_fraction: float = 0.25,
) -> ChronologicalSplit:
    if train_fraction <= 0 or validation_fraction < 0:
        raise ValueError("Split fractions must be positive train and non-negative validation.")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("Train plus validation must leave a non-empty test fraction.")
    candles = tuple(dataset.candles)
    if len(candles) < 6:
        raise ValueError("Need at least six candles to form chronological splits.")
    times = [candle.close_time.astimezone(timezone.utc) for candle in candles]
    count = len(times)
    train_end_index = max(1, int(count * train_fraction))
    validation_end_index = max(
        train_end_index + 1,
        int(count * (train_fraction + validation_fraction)),
    )
    if validation_end_index >= count:
        validation_end_index = count - 1
    train_end = times[train_end_index]
    validation_end = times[validation_end_index]
    test_end = times[-1] + (times[-1] - times[-2])
    return ChronologicalSplit(
        train_start=times[0],
        train_end=train_end,
        validation_start=train_end,
        validation_end=validation_end,
        test_start=validation_end,
        test_end=test_end,
        train_candles=train_end_index,
        validation_candles=validation_end_index - train_end_index,
        test_candles=count - validation_end_index,
    )
