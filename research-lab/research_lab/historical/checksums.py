"""SHA-256 verification against Binance Vision .CHECKSUM sidecars."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

HEX64 = re.compile(r"\b([a-fA-F0-9]{64})\b")


def parse_checksum_text(text: str, *, expected_filename: str | None = None) -> str:
    """Extract the SHA-256 digest from a CHECKSUM file body."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("CHECKSUM file is empty.")
    for line in lines:
        match = HEX64.search(line)
        if match is None:
            continue
        digest = match.group(1).lower()
        if expected_filename is None:
            return digest
        # Prefer a line that mentions the archive name when present.
        if expected_filename in line or Path(expected_filename).name in line:
            return digest
    # Fall back to first hex digest found.
    match = HEX64.search(lines[0])
    if match is None:
        raise ValueError("CHECKSUM file does not contain a SHA-256 digest.")
    return match.group(1).lower()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(path: Path, expected_sha256: str) -> str:
    actual = sha256_file(path)
    if actual.lower() != expected_sha256.lower():
        raise ValueError(
            f"SHA-256 mismatch for {path.name}: expected {expected_sha256}, got {actual}."
        )
    return actual
