"""Bounded buffered JSONL writer for research events (Stage 3.2.8 / 3.2.8.4).

Partial-write guarantees
------------------------
Each flush concatenates the in-memory buffer into one payload and records the
file offset before writing. On ``OSError``:

1. Attempt to ``seek``+``truncate`` back to the pre-write offset.
2. If truncation succeeds, the on-disk file is unchanged relative to that flush
   attempt; the buffer is retained and a later flush may retry safely (no
   duplicates).
3. If truncation fails, or the writer cannot establish a reliable offset,
   ``persistence_uncertain`` is set, further writes are refused, the buffer is
   retained in memory (not discarded), and the run must not be marked complete.

Close / retry policy (Stage 3.2.8.4)
------------------------------------
- Recoverable flush failure during ``close()`` (truncate succeeded): the file
  handle stays open, ``_closed`` stays False, buffer is retained, and the
  caller may retry ``flush()`` / ``close()``. Recovery after this failure is
  **supported**.
- Unrecoverable flush failure or pre-existing ``persistence_uncertain``: the
  writer **seals** (handle closed, ``_closed=True``), buffer retained for
  inspection only, further writes prohibited. Recovery after seal is
  **intentionally prohibited**.
- Repeated ``close()`` after seal is a no-op. Empty ``flush()`` is a no-op.
- ``persistence_complete`` is never True while the buffer is non-empty, while
  persistence is uncertain, or while unrecovered flush errors remain.

``run_completed`` vs durable completion
---------------------------------------
Emitting ``run_completed`` with ``persistence_complete: true`` in the payload
is only a claim that becomes meaningful once that line (and prior events) have
been successfully flushed. Callers must flush the terminal event before
``close(mark_complete=True)``. The writer's ``persistence_complete`` property
is the authoritative seal for the run summary.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, TextIO

from research_lab.events.schema import (
    SCHEMA_VERSION,
    SYMBOL_DEFAULT,
    ResearchEvent,
    make_event_id,
    utc_iso,
)
from research_lab.historical.constants import PROVENANCE
from research_lab.isolation import assert_isolated_path


DEFAULT_BUFFER_LIMIT = 512

# Optional test hook: (payload: str, handle) -> None. May write a prefix then raise.
FaultInjector = Callable[[str, TextIO], None]
# Optional test hook: called with stage name ("tell"|"seek"|"truncate") before IO.
IoStageFault = Callable[[str], None]


@dataclass
class ResearchEventWriter:
    """Append-only JSONL event stream with bounded memory and durable flushes."""

    path: Path
    run_id: str
    arm: str
    buffer_limit: int = DEFAULT_BUFFER_LIMIT
    symbol: str = SYMBOL_DEFAULT
    provenance: str = PROVENANCE
    schema_version: str = SCHEMA_VERSION
    # Test-only fault injection; must remain None in production research runs.
    fault_injector: FaultInjector | None = None
    io_stage_fault: IoStageFault | None = None

    _buffer: list[str] = field(default_factory=list, init=False, repr=False)
    _sequence: int = field(default=0, init=False, repr=False)
    _handle: TextIO | None = field(default=None, init=False, repr=False)
    _fingerprint: Any = field(default=None, init=False, repr=False)
    _event_counts: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _flush_errors: list[dict[str, object]] = field(
        default_factory=list, init=False, repr=False
    )
    _closed: bool = field(default=False, init=False, repr=False)
    _closed_reason: str | None = field(default=None, init=False, repr=False)
    _persistence_complete: bool = field(default=False, init=False, repr=False)
    _persistence_uncertain: bool = field(default=False, init=False, repr=False)
    _last_emitted_sequence: int = field(default=0, init=False, repr=False)
    _pending_lifecycle_id: str | None = field(default=None, init=False, repr=False)
    _bytes_flushed: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.buffer_limit < 1:
            raise ValueError("buffer_limit must be >= 1")
        self.path = assert_isolated_path(self.path)
        runs_ok = "experiments" in {p.lower() for p in self.path.parts} and "runs" in {
            p.lower() for p in self.path.parts
        }
        if not runs_ok:
            raise ValueError(
                "Research event files must live under research-lab/experiments/runs/."
            )
        if self.path.exists():
            raise ValueError(
                f"Refusing to overwrite existing research event file: {self.path}"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8", newline="\n")
        self._fingerprint = hashlib.sha256()

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def closed_reason(self) -> str | None:
        return self._closed_reason

    @property
    def persistence_uncertain(self) -> bool:
        return self._persistence_uncertain

    @property
    def retry_supported(self) -> bool:
        """True when flush/close may still be retried (handle open, not uncertain)."""

        return (
            not self._closed
            and not self._persistence_uncertain
            and self._handle is not None
        )

    @property
    def _blocking_flush_errors(self) -> list[dict[str, object]]:
        """Errors that prevent claiming durable completion.

        Truncate-recovered failures are retained for diagnostics but do not
        block completion after a later successful flush of the same buffer.
        """

        return [
            error
            for error in self._flush_errors
            if not error.get("truncate_recovered")
        ]

    @property
    def persistence_complete(self) -> bool:
        return (
            self._persistence_complete
            and not self._blocking_flush_errors
            and not self._buffer
            and not self._persistence_uncertain
        )

    @property
    def flush_errors(self) -> list[dict[str, object]]:
        return list(self._flush_errors)

    @property
    def event_counts(self) -> dict[str, int]:
        return dict(self._event_counts)

    @property
    def buffer_len(self) -> int:
        return len(self._buffer)

    @property
    def pending_lifecycle_id(self) -> str | None:
        return self._pending_lifecycle_id

    def set_pending_lifecycle_id(self, lifecycle_id: str | None) -> None:
        self._pending_lifecycle_id = lifecycle_id

    def _invoke_stage_fault(self, stage: str) -> None:
        if self.io_stage_fault is not None:
            self.io_stage_fault(stage)

    def emit(
        self,
        event_type: str,
        *,
        event_time: datetime | None,
        candle_open_time: datetime | None = None,
        signal_time: datetime | None = None,
        signal_price: float | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> ResearchEvent:
        if self._closed:
            raise RuntimeError("ResearchEventWriter is closed.")
        if self._persistence_uncertain:
            raise RuntimeError(
                "Research event persistence is uncertain after a partial write; "
                "refusing further emits."
            )
        self._sequence += 1
        event = ResearchEvent(
            schema_version=self.schema_version,
            run_id=self.run_id,
            arm=self.arm,
            event_id=make_event_id(
                run_id=self.run_id,
                arm=self.arm,
                sequence=self._sequence,
                event_type=event_type,
            ),
            sequence=self._sequence,
            event_type=event_type,
            event_time=utc_iso(event_time),
            candle_open_time=utc_iso(candle_open_time),
            signal_time=utc_iso(signal_time),
            signal_price=None if signal_price is None else float(signal_price),
            symbol=self.symbol,
            provenance=self.provenance,
            payload=dict(payload or {}),
        )
        line = event.to_json_line() + "\n"
        self._buffer.append(line)
        self._event_counts[event_type] = self._event_counts.get(event_type, 0) + 1
        self._last_emitted_sequence = self._sequence
        if len(self._buffer) >= self.buffer_limit:
            self.flush()
        return event

    def flush(self) -> None:
        """Persist the buffer with truncate-on-failure safety.

        Empty-buffer flush is a no-op. After ``persistence_uncertain`` or seal,
        flush raises without writing.
        """

        if not self._buffer:
            return
        if self._closed:
            raise RuntimeError("ResearchEventWriter is closed; flush refused.")
        if self._persistence_uncertain:
            raise RuntimeError(
                "Research event persistence is uncertain; refusing flush retry."
            )
        if self._handle is None:
            raise RuntimeError("Event writer has no open handle.")

        start_seq = self._last_emitted_sequence - len(self._buffer) + 1
        end_seq = self._last_emitted_sequence
        payload = "".join(self._buffer)
        try:
            self._invoke_stage_fault("tell")
            start_pos = self._handle.tell()
        except OSError as error:
            self._mark_uncertain(
                error=error,
                start_seq=start_seq,
                end_seq=end_seq,
                detail="tell_failed_before_write",
            )
            raise

        try:
            if self.fault_injector is not None:
                self.fault_injector(payload, self._handle)
            else:
                self._handle.write(payload)
            self._handle.flush()
        except OSError as error:
            recovered = self._try_truncate(start_pos)
            self._flush_errors.append(
                {
                    "error": str(error),
                    "sequence_range": [start_seq, end_seq],
                    "buffer_lines": len(self._buffer),
                    "truncate_recovered": recovered,
                    "start_pos": start_pos,
                }
            )
            self._persistence_complete = False
            if not recovered:
                self._persistence_uncertain = True
            # Buffer always retained (never silently discarded).
            raise

        # Success: advance fingerprint, clear buffer, track bytes.
        for line in self._buffer:
            self._fingerprint.update(line.encode("utf-8"))
        self._bytes_flushed += len(payload.encode("utf-8"))
        self._buffer.clear()

    def _try_truncate(self, start_pos: int) -> bool:
        if self._handle is None:
            return False
        try:
            self._invoke_stage_fault("seek")
            self._handle.seek(start_pos)
            self._invoke_stage_fault("truncate")
            # Prefer position-based truncate after seek; pass size for clarity on
            # platforms where tell() returns a byte offset (local UTF-8 JSONL).
            self._handle.truncate(start_pos)
            self._handle.flush()
            return True
        except OSError as error:
            self._flush_errors.append(
                {
                    "error": str(error),
                    "detail": "truncate_after_partial_write_failed",
                    "start_pos": start_pos,
                    "truncate_recovered": False,
                }
            )
            return False

    def _mark_uncertain(
        self,
        *,
        error: BaseException,
        start_seq: int,
        end_seq: int,
        detail: str,
    ) -> None:
        self._persistence_uncertain = True
        self._persistence_complete = False
        self._flush_errors.append(
            {
                "error": str(error),
                "sequence_range": [start_seq, end_seq],
                "buffer_lines": len(self._buffer),
                "detail": detail,
                "truncate_recovered": False,
            }
        )

    def _seal(self, *, reason: str) -> None:
        """Close the handle and prohibit further writes. Buffer is not cleared."""

        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None
        self._closed = True
        self._closed_reason = reason

    def abandon(self) -> None:
        """Seal without flushing. Buffer retained for inspection; never complete.

        Used when a run must fail closed and will not retry persistence.
        Idempotent. Further emit/flush are refused.
        """

        if self._closed:
            return
        self._persistence_complete = False
        self._seal(reason="abandoned")

    def close(self, *, mark_complete: bool = False) -> None:
        """Flush remaining buffer then seal, or leave open after recoverable failure.

        See module docstring for retry policy.
        """

        if self._closed:
            return

        if self._persistence_uncertain:
            self._persistence_complete = False
            self._seal(reason="persistence_uncertain")
            return

        try:
            self.flush()
        except OSError:
            if self._persistence_uncertain:
                # Unrecoverable: seal, keep buffer for inspection, no write retry.
                self._persistence_complete = False
                self._seal(reason="unrecoverable_flush")
                raise
            # Recoverable: keep handle open so flush/close can be retried.
            raise

        if (
            mark_complete
            and not self._blocking_flush_errors
            and not self._buffer
            and not self._persistence_uncertain
        ):
            self._persistence_complete = True
        elif mark_complete:
            self._persistence_complete = False

        self._seal(reason="normal")

    def incremental_fingerprint_hex(self) -> str:
        """SHA-256 of flushed bytes only (excludes unflushed buffer)."""

        return self._fingerprint.hexdigest()

    def status_payload(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "run_id": self.run_id,
            "arm": self.arm,
            "events_emitted": self._sequence,
            "event_counts": self.event_counts,
            "buffer_remaining": len(self._buffer),
            "flush_errors": self.flush_errors,
            "persistence_complete": self.persistence_complete,
            "persistence_uncertain": self._persistence_uncertain,
            "closed": self._closed,
            "closed_reason": self._closed_reason,
            "retry_supported": self.retry_supported,
            "incremental_fingerprint": self.incremental_fingerprint_hex(),
            "bytes_flushed": self._bytes_flushed,
        }


def fingerprint_event_file(path: Path) -> str:
    """Deterministic fingerprint of a flushed event JSONL (entire file bytes)."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_events_excluding_wallclock(path: Path) -> str:
    """Fingerprint event file bytes (no wall-clock fields are written)."""

    return fingerprint_event_file(path)
