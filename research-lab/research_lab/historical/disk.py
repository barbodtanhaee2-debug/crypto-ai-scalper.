"""Disk-space inspection for safe historical downloads."""

from __future__ import annotations

import shutil
from pathlib import Path

from research_lab.historical.constants import DATASET_ROOT, DISK_RESERVE_BYTES
from research_lab.isolation import assert_isolated_path


def free_bytes(path: Path | None = None) -> int:
    target = assert_isolated_path(path or DATASET_ROOT)
    target.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target)
    return int(usage.free)


def format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{value} B"


def disk_safety_report(
    *,
    estimated_download_bytes: int,
    free: int | None = None,
    reserve_bytes: int = DISK_RESERVE_BYTES,
) -> dict[str, object]:
    available = free if free is not None else free_bytes()
    required = estimated_download_bytes + reserve_bytes
    return {
        "path": str(DATASET_ROOT),
        "free_bytes": available,
        "free_human": format_bytes(available),
        "estimated_download_bytes": estimated_download_bytes,
        "estimated_download_human": format_bytes(estimated_download_bytes),
        "reserve_bytes": reserve_bytes,
        "reserve_human": format_bytes(reserve_bytes),
        "required_bytes": required,
        "required_human": format_bytes(required),
        "safe": available >= required and available >= reserve_bytes,
    }
