"""Semantic versioning and compatibility policy for adaptive contracts.

Policy (Stage 3.3.2.2 / remediated 3.3.2.4)
------------------------------------------
- **Major:** breaking structural or semantic changes. Unsupported major → fail closed.
- **Minor:** backward-compatible additions (optional fields with defaults, new
  recognized statuses that do not reinterpret existing ones).
- **Patch:** corrections that do not change contract meaning.

A reader at version ``X.Y.Z`` may accept records with the same major ``X`` and
``minor <= Y``. Higher minor from a writer is accepted only when the reader
declares ``allow_newer_minor=True`` (default False for fail-closed research
tools). Unknown major always fails.

SemVer components must be non-negative integers without leading zeros
(except the value ``0`` itself).

Deprecated fields: retained in ``deprecated_fields`` maps when present; readers
must not require them. Removal of a deprecated field is a major change after
the documented transition window.
"""

from __future__ import annotations

from dataclasses import dataclass


SCHEMA_VERSION = "1.0.0"
CANDIDATE_CONTRACT_VERSION = "1.0.0"
RECOMMENDATION_CONTRACT_VERSION = "1.0.0"
DECISION_RECORD_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def _parse_component(part: str, *, label: str) -> int:
    if not part.isdigit():
        raise ValueError(f"invalid semver {label}: non-numeric component {part!r}")
    if len(part) > 1 and part.startswith("0"):
        raise ValueError(
            f"invalid semver {label}: leading zeros not allowed in component {part!r}"
        )
    return int(part)


def parse_semver(value: str) -> SemVer:
    if not isinstance(value, str):
        raise ValueError(f"invalid semver {value!r}; expected string MAJOR.MINOR.PATCH")
    text = value.strip()
    parts = text.split(".")
    if len(parts) != 3:
        raise ValueError(f"invalid semver {value!r}; expected MAJOR.MINOR.PATCH")
    major = _parse_component(parts[0], label=value)
    minor = _parse_component(parts[1], label=value)
    patch = _parse_component(parts[2], label=value)
    if major < 0 or minor < 0 or patch < 0:
        raise ValueError(f"invalid semver {value!r}; components must be non-negative")
    return SemVer(major, minor, patch)


def is_compatible(
    record_version: str,
    *,
    reader_version: str,
    allow_newer_minor: bool = False,
) -> bool:
    """Return True when ``record_version`` may be read by ``reader_version``."""

    try:
        record = parse_semver(record_version)
        reader = parse_semver(reader_version)
    except ValueError:
        return False
    if record.major != reader.major:
        return False
    if record.minor < reader.minor:
        return True
    if record.minor == reader.minor:
        return True
    return bool(allow_newer_minor)


def classify_version(
    record_version: str,
    *,
    reader_version: str,
    allow_newer_minor: bool = False,
) -> str:
    """Return ``ok``, ``malformed``, or ``unsupported`` for error reporting."""

    try:
        parse_semver(record_version)
        parse_semver(reader_version)
    except ValueError:
        return "malformed"
    if is_compatible(
        record_version,
        reader_version=reader_version,
        allow_newer_minor=allow_newer_minor,
    ):
        return "ok"
    return "unsupported"


def assert_compatible(
    record_version: str,
    *,
    reader_version: str,
    allow_newer_minor: bool = False,
    label: str = "schema_version",
) -> None:
    kind = classify_version(
        record_version,
        reader_version=reader_version,
        allow_newer_minor=allow_newer_minor,
    )
    if kind == "malformed":
        raise ValueError(f"malformed {label}={record_version!r}")
    if kind == "unsupported":
        raise ValueError(
            f"unsupported {label}={record_version!r} for reader "
            f"{reader_version!r} (fail closed)"
        )
