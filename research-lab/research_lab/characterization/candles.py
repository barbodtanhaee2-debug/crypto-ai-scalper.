"""Minimal OHLC candle type for characterization (lab-only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence


@dataclass(frozen=True)
class CharacterizationCandle:
    """One completed OHLC bar available at ``timestamp`` (candle close time)."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


CandleSeries = Sequence[CharacterizationCandle]
