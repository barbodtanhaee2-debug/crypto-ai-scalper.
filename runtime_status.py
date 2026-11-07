"""Atomic local runtime-status persistence for read-only monitoring."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Mapping


def write_runtime_status(path: str | Path, status: Mapping[str, object]) -> None:
    """Replace the status record atomically within its destination directory."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(dict(status), temporary, separators=(",", ":"), allow_nan=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
