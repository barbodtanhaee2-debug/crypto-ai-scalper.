"""Offline integrity validation for research event JSONL streams."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_lab.events.schema import (
    EVENT_TYPES,
    LIFECYCLE_REQUIRED_TYPES,
    make_event_id,
    validate_event_dict,
)


@dataclass
class EventValidationReport:
    path: str
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    event_count: int = 0
    event_counts: dict[str, int] = field(default_factory=dict)
    has_run_completed: bool = False
    has_run_failed: bool = False
    persistence_complete_flag: bool | None = None
    unavailable_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "event_count": self.event_count,
            "event_counts": dict(self.event_counts),
            "has_run_completed": self.has_run_completed,
            "has_run_failed": self.has_run_failed,
            "persistence_complete_flag": self.persistence_complete_flag,
            "unavailable_evidence": list(self.unavailable_evidence),
        }


def validate_event_stream(path: Path) -> EventValidationReport:
    """Validate schema, sequences, IDs, and lifecycle consistency.

    Distinguishes:
    - legitimate absence (no pending ever created) — no error
    - unavailable fields (explicit nulls on non-lifecycle events) — recorded as
      ``unavailable_evidence``, not errors
    - missing/corrupted records (required lifecycle_id absent, bad event_id,
      illegal transitions) — errors
    """

    report = EventValidationReport(path=str(path), ok=True)
    if not path.exists():
        report.ok = False
        report.errors.append("event file does not exist")
        return report

    seen_ids: set[str] = set()
    expected_sequence = 1
    # lifecycle_id -> "pending_entry" | "cancelled" | "filled" | "pending_exit" | "closed"
    lifecycle_state: dict[str, str] = {}
    open_pending: str | None = None
    open_exit: str | None = None
    open_position = False
    run_id: str | None = None
    arm: str | None = None

    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                report.errors.append(f"line {line_number}: empty line")
                continue
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as error:
                report.errors.append(f"line {line_number}: invalid JSON ({error})")
                continue
            if not isinstance(raw, dict):
                report.errors.append(f"line {line_number}: event must be an object")
                continue

            schema_errors = validate_event_dict(raw)
            for problem in schema_errors:
                report.errors.append(f"line {line_number}: {problem}")

            event_type = str(raw.get("event_type"))
            report.event_counts[event_type] = report.event_counts.get(event_type, 0) + 1
            report.event_count += 1

            sequence = raw.get("sequence")
            if sequence != expected_sequence:
                report.errors.append(
                    f"line {line_number}: sequence gap/mismatch "
                    f"expected={expected_sequence} got={sequence}"
                )
            expected_sequence += 1

            event_id = str(raw.get("event_id"))
            if event_id in seen_ids:
                report.errors.append(f"line {line_number}: duplicate event_id={event_id}")
            seen_ids.add(event_id)

            if run_id is None:
                run_id = str(raw.get("run_id"))
                arm = str(raw.get("arm"))
            else:
                if raw.get("run_id") != run_id:
                    report.errors.append(f"line {line_number}: inconsistent run_id")
                if raw.get("arm") != arm:
                    report.errors.append(f"line {line_number}: inconsistent arm")

            # Recompute event_id from documented inputs; reject mismatches.
            if (
                run_id is not None
                and arm is not None
                and isinstance(sequence, int)
                and event_type in EVENT_TYPES
            ):
                expected_id = make_event_id(
                    run_id=run_id,
                    arm=arm,
                    sequence=int(sequence),
                    event_type=event_type,
                )
                if event_id != expected_id:
                    report.errors.append(
                        f"line {line_number}: event_id mismatch "
                        f"expected={expected_id} got={event_id}"
                    )

            payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
            lifecycle_id = payload.get("lifecycle_id")

            if event_type in LIFECYCLE_REQUIRED_TYPES:
                if lifecycle_id is None or lifecycle_id == "":
                    report.errors.append(
                        f"line {line_number}: {event_type} missing required lifecycle_id "
                        "(corrupt/missing evidence, not unavailable)"
                    )
            elif "lifecycle_id" in payload and payload.get("lifecycle_id") is None:
                report.unavailable_evidence.append(
                    f"line {line_number}: {event_type} has explicit null lifecycle_id"
                )

            lid = str(lifecycle_id) if lifecycle_id else None

            if event_type == "entry_signal":
                if lid and lifecycle_state.get(lid) in {
                    "cancelled",
                    "filled",
                    "closed",
                    "pending_exit",
                }:
                    report.errors.append(
                        f"line {line_number}: entry_signal on spent "
                        f"lifecycle_id={lid} state={lifecycle_state[lid]}"
                    )
                elif lid and lid not in lifecycle_state:
                    lifecycle_state[lid] = "signaled"
            elif event_type == "entry_pending":
                if open_pending is not None:
                    report.errors.append(
                        f"line {line_number}: duplicate pending entry "
                        f"(open={open_pending}, new={lid})"
                    )
                prior = lifecycle_state.get(lid) if lid else None
                if prior not in {None, "signaled", "pending_entry"}:
                    report.errors.append(
                        f"line {line_number}: duplicate lifecycle_id={lid} "
                        f"prior_state={prior}"
                    )
                open_pending = lid or f"seq:{sequence}"
                if lid:
                    lifecycle_state[lid] = "pending_entry"
            elif event_type == "entry_compatibility_check":
                prior = lifecycle_state.get(lid) if lid else None
                if prior not in {None, "signaled", "pending_entry"}:
                    report.errors.append(
                        f"line {line_number}: compatibility check on "
                        f"lifecycle_id={lid} state={prior}"
                    )
            elif event_type in {
                "entry_cancelled_incompatible",
                "entry_cancelled_missing_candle",
                "entry_rejected",
            }:
                if open_pending is None:
                    report.errors.append(
                        f"line {line_number}: {event_type} without open pending"
                    )
                prior = lifecycle_state.get(lid) if lid else None
                if prior == "cancelled":
                    report.errors.append(
                        f"line {line_number}: duplicate cancel transition "
                        f"lifecycle_id={lid}"
                    )
                if prior == "filled":
                    report.errors.append(
                        f"line {line_number}: cancel/reject after fill "
                        f"lifecycle_id={lid}"
                    )
                if lid:
                    lifecycle_state[lid] = "cancelled"
                open_pending = None
            elif event_type == "risk_limit_rejected":
                # May terminate either an entry pending or an exit pending.
                if open_pending is None and open_exit is None:
                    report.errors.append(
                        f"line {line_number}: risk_limit_rejected without open "
                        "entry/exit pending"
                    )
                prior = lifecycle_state.get(lid) if lid else None
                if prior == "cancelled":
                    report.errors.append(
                        f"line {line_number}: duplicate risk reject "
                        f"lifecycle_id={lid}"
                    )
                if lid:
                    lifecycle_state[lid] = "cancelled"
                if open_pending is not None and (
                    lid is None or open_pending == lid or lid == open_pending
                ):
                    open_pending = None
                elif open_exit is not None:
                    open_exit = None
                else:
                    open_pending = None
            elif event_type == "entry_filled":
                if open_pending is None:
                    report.errors.append(
                        f"line {line_number}: entry_filled without open pending"
                    )
                prior = lifecycle_state.get(lid) if lid else None
                if prior == "cancelled":
                    report.errors.append(
                        f"line {line_number}: fill-after-cancel lifecycle_id={lid}"
                    )
                if prior == "filled":
                    report.errors.append(
                        f"line {line_number}: duplicate fill lifecycle_id={lid}"
                    )
                if lid:
                    lifecycle_state[lid] = "filled"
                open_pending = None
                if open_position:
                    report.errors.append(
                        f"line {line_number}: entry_filled while position already open"
                    )
                open_position = True
            elif event_type == "position_exit_signal":
                if open_exit is not None:
                    report.errors.append(
                        f"line {line_number}: duplicate exit pending "
                        f"(open={open_exit}, new={lid})"
                    )
                if not open_position:
                    report.warnings.append(
                        f"line {line_number}: position_exit_signal without open position"
                    )
                open_exit = lid or f"seq:{sequence}"
                if lid:
                    lifecycle_state[lid] = "pending_exit"
            elif event_type in {"exit_rejected", "exit_cancelled_missing_candle"}:
                if open_exit is None:
                    report.errors.append(
                        f"line {line_number}: {event_type} without open exit pending"
                    )
                prior = lifecycle_state.get(lid) if lid else None
                if prior == "cancelled":
                    report.errors.append(
                        f"line {line_number}: duplicate exit cancel lifecycle_id={lid}"
                    )
                if lid:
                    lifecycle_state[lid] = "cancelled"
                open_exit = None
            elif event_type == "position_closed":
                if not open_position:
                    report.errors.append(
                        f"line {line_number}: position_closed before position opened "
                        "(close-before-open)"
                    )
                prior = lifecycle_state.get(lid) if lid else None
                if prior == "closed":
                    report.errors.append(
                        f"line {line_number}: duplicate close lifecycle_id={lid}"
                    )
                if lid:
                    lifecycle_state[lid] = "closed"
                open_position = False
                open_exit = None
            elif event_type == "run_completed":
                report.has_run_completed = True
                report.persistence_complete_flag = payload.get("persistence_complete")
                if payload.get("persistence_complete") is not True:
                    report.errors.append(
                        f"line {line_number}: run_completed without persistence_complete=true"
                    )
            elif event_type == "run_failed":
                report.has_run_failed = True
                report.persistence_complete_flag = payload.get("persistence_complete")

            if event_type not in EVENT_TYPES and not schema_errors:
                report.errors.append(f"line {line_number}: unknown event_type")

    if report.has_run_completed and report.has_run_failed:
        report.errors.append("stream contains both run_completed and run_failed")
    if not report.has_run_completed and not report.has_run_failed:
        # Stage 3.2.8.4: incomplete streams are not accepted as durable-complete.
        report.errors.append(
            "no terminal run_completed/run_failed event (incomplete stream)"
        )
    if report.has_run_completed and report.persistence_complete_flag is not True:
        if not any("persistence_complete" in e for e in report.errors):
            report.errors.append(
                "run_completed present without confirmed persistence_complete=true"
            )
    if open_pending is not None and report.has_run_completed:
        report.warnings.append("run_completed with unresolved pending entry lifecycle")
    if open_exit is not None and report.has_run_completed:
        report.warnings.append("run_completed with unresolved pending exit lifecycle")

    report.ok = not report.errors
    return report
