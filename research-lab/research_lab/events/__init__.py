"""Bounded research event capture (Stage 3.2.8). Lab-only; no production hooks."""

from __future__ import annotations

from research_lab.events.schema import (
    EVENT_TYPES,
    SCHEMA_VERSION,
    ResearchEvent,
    make_event_id,
)
from research_lab.events.validator import EventValidationReport, validate_event_stream
from research_lab.events.writer import ResearchEventWriter, fingerprint_event_file

__all__ = [
    "EVENT_TYPES",
    "SCHEMA_VERSION",
    "ResearchEvent",
    "ResearchEventWriter",
    "EventValidationReport",
    "fingerprint_event_file",
    "make_event_id",
    "validate_event_stream",
]
