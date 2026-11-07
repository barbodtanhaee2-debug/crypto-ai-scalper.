"""Refuse production journals, original captures, and the v4 tree."""

from __future__ import annotations

from pathlib import Path

from research_lab import PROJECT_ROOT

FORBIDDEN_TREES = (Path(r"F:\crypto-ai-trade-v4"),)
LIVE_JOURNAL_NAMES = frozenset({"paper_broker.jsonl", "runtime_status.json"})
CAPTURE_ROOT = PROJECT_ROOT / "quote-tape" / "sessions"


def assert_isolated_path(path: str | Path, *, allow_capture_read: bool = False) -> Path:
    resolved = Path(path).expanduser().resolve()
    for tree in FORBIDDEN_TREES:
        try:
            resolved.relative_to(tree.resolve())
        except ValueError:
            continue
        raise ValueError(f"Path is outside the scalper project boundary: {resolved}")
    if resolved.name in LIVE_JOURNAL_NAMES:
        raise ValueError("Research runs must not use the live paper journal or status file.")
    if not allow_capture_read:
        try:
            resolved.relative_to(CAPTURE_ROOT.resolve())
        except ValueError:
            pass
        else:
            raise ValueError(
                "Research must not write original capture files "
                f"under {CAPTURE_ROOT}."
            )
    return resolved


def assert_not_capture_write(path: str | Path) -> Path:
    return assert_isolated_path(path, allow_capture_read=False)
