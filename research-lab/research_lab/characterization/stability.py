"""Regime confirmation with persistence and hysteresis (no retrospective edits).

A single noisy raw label does not immediately change the confirmed regime.
Confirmed state only advances forward in time; past outputs are never rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass

from research_lab.characterization.config import StabilityConfig
from research_lab.characterization.models import PrimaryRegime


@dataclass
class RegimeStabilityTracker:
    """Stateful confirmer for sequential candle closes."""

    config: StabilityConfig
    confirmed: PrimaryRegime = PrimaryRegime.UNKNOWN
    candidate: PrimaryRegime | None = None
    candidate_count: int = 0
    persistence_count: int = 0

    def reset(self, *, reason: str = "reset") -> None:
        del reason  # documented for call sites / logging only
        self.confirmed = PrimaryRegime.UNKNOWN
        self.candidate = None
        self.candidate_count = 0
        self.persistence_count = 0

    def on_gap(self, gap_intervals: int) -> None:
        if gap_intervals >= self.config.gap_reset_intervals:
            self.reset(reason="data_gap")

    def update(self, raw: PrimaryRegime) -> tuple[PrimaryRegime, int]:
        """Incorporate one raw label; returns (confirmed, persistence_count)."""

        cfg = self.config
        if raw == PrimaryRegime.UNKNOWN:
            # Unknown does not extend a candidate; does not flip confirmed alone.
            self.candidate = None
            self.candidate_count = 0
            if self.confirmed != PrimaryRegime.UNKNOWN:
                self.persistence_count += 1
            return self.confirmed, self.persistence_count

        if self.confirmed == PrimaryRegime.UNKNOWN:
            required = cfg.min_persistence_bars
        elif raw == self.confirmed:
            self.candidate = None
            self.candidate_count = 0
            self.persistence_count += 1
            return self.confirmed, self.persistence_count
        else:
            # Switching away from an established regime requires hysteresis.
            required = cfg.min_persistence_bars + cfg.hysteresis_bars

        if self.candidate == raw:
            self.candidate_count += 1
        else:
            self.candidate = raw
            self.candidate_count = 1

        if self.candidate_count >= required:
            self.confirmed = raw
            self.candidate = None
            self.candidate_count = 0
            self.persistence_count = 1
        return self.confirmed, self.persistence_count
