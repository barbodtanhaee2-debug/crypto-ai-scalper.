"""Immutable dataset catalog: pointers to validated quote-tape sessions."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from quote_tape_dataset import QuoteTapeDataset, load_quote_tape_session
from quote_tape_replay import load_reconnect_gaps
from research_lab import LAB_ROOT, PROJECT_ROOT
from research_lab.isolation import assert_isolated_path

CATALOG_PATH = LAB_ROOT / "datasets" / "catalog.json"


@dataclass(frozen=True)
class RegisteredDataset:
    dataset_id: str
    session_id: str
    session_dir: Path
    manifest_path: Path
    validation_report: Path
    session_json_sha256: str
    immutable: bool
    tape: QuoteTapeDataset
    reconnect_gaps: tuple[dict[str, object], ...]
    missing_candle_intervals: tuple[str, ...]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _project_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def load_catalog(path: str | Path | None = None) -> list[dict[str, object]]:
    catalog_path = Path(path) if path is not None else CATALOG_PATH
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    datasets = payload.get("datasets")
    if not isinstance(datasets, list):
        raise ValueError("catalog.json must contain a datasets list.")
    return datasets


def _manifest_contains_session_hash(manifest_path: Path, expected: str) -> bool:
    with manifest_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            relative = (row.get("relative_path") or row.get("path") or "").replace("\\", "/")
            digest = (row.get("sha256") or row.get("hash") or "").strip().lower()
            if relative.endswith("session.json") and digest == expected.lower():
                return True
    return False


def _missing_minute_intervals(dataset: QuoteTapeDataset) -> tuple[str, ...]:
    times = [candle.close_time for candle in dataset.candles]
    if len(times) < 2:
        return ()
    missing: list[str] = []
    ordered = sorted(times)
    for previous, current in zip(ordered, ordered[1:]):
        delta = (current - previous).total_seconds()
        if delta > 90:
            missing.append(f"{previous.isoformat()} -> {current.isoformat()} ({delta:.3f}s)")
    return tuple(missing)


def bind_dataset(dataset_id: str, *, catalog_path: str | Path | None = None) -> RegisteredDataset:
    """Load a catalogued session read-only and verify identity against the SHA-256 manifest."""
    entries = load_catalog(catalog_path)
    match = next((row for row in entries if row.get("dataset_id") == dataset_id), None)
    if match is None:
        raise KeyError(f"Unknown dataset_id: {dataset_id}")
    session_dir = _project_path(str(match["session_dir"]))
    manifest_path = _project_path(str(match["manifest_path"]))
    validation_report = _project_path(str(match["validation_report"]))
    assert_isolated_path(session_dir, allow_capture_read=True)
    if not session_dir.is_dir():
        raise FileNotFoundError(f"Session directory missing: {session_dir}")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"SHA-256 manifest missing: {manifest_path}")
    if not validation_report.is_file():
        raise FileNotFoundError(f"Validation report missing: {validation_report}")
    expected = str(match["session_json_sha256"]).lower()
    actual = _sha256_file(session_dir / "session.json")
    if actual != expected:
        raise ValueError(
            "session.json hash does not match the catalogued Stage 2.3 digest; "
            "refusing to bind a mutated capture."
        )
    if not _manifest_contains_session_hash(manifest_path, expected):
        raise ValueError("Manifest does not contain the catalogued session.json SHA-256.")
    tape = load_quote_tape_session(session_dir)
    status = tape.session.get("status")
    ended = tape.session.get("ended_at_utc")
    if status != "stopped":
        raise ValueError(f"Session status must be stopped, got {status!r}.")
    if not ended:
        raise ValueError("ended_at_utc is missing; capture is not complete.")
    return RegisteredDataset(
        dataset_id=str(match["dataset_id"]),
        session_id=str(match["session_id"]),
        session_dir=session_dir,
        manifest_path=manifest_path,
        validation_report=validation_report,
        session_json_sha256=expected,
        immutable=bool(match.get("immutable", True)),
        tape=tape,
        reconnect_gaps=tuple(load_reconnect_gaps(session_dir)),
        missing_candle_intervals=_missing_minute_intervals(tape),
    )


def slice_dataset(
    registered: RegisteredDataset,
    *,
    start: datetime | None,
    end: datetime | None,
) -> QuoteTapeDataset:
    """In-memory chronological view. Does not rewrite capture files."""
    tape = registered.tape

    def in_window(received: datetime) -> bool:
        if start is not None and received < start:
            return False
        if end is not None and received >= end:
            return False
        return True

    quotes = tuple(quote for quote in tape.quotes if in_window(quote.received_at))
    candles = tuple(candle for candle in tape.candles if in_window(candle.received_at))
    return replace(tape, quotes=quotes, candles=candles)
