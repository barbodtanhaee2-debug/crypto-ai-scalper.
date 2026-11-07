"""Dataset manifests: source, dates, sizes, hashes, record counts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from research_lab.historical.checksums import sha256_file
from research_lab.historical.constants import MANIFESTS, PROVENANCE, SYMBOL
from research_lab.isolation import assert_isolated_path


@dataclass
class ArchiveManifestEntry:
    kind: str
    granularity: str
    period: str
    filename: str
    relative_path: str
    status: str
    archive_path: str | None = None
    checksum_path: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    record_count: int | None = None
    error: str | None = None


@dataclass
class DatasetManifest:
    provenance: str = PROVENANCE
    symbol: str = SYMBOL
    market: str = "spot"
    source: str = "https://data.binance.vision/"
    created_at_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    klines_window: dict[str, str] = field(default_factory=dict)
    aggtrades_window: dict[str, str] = field(default_factory=dict)
    archives: list[ArchiveManifestEntry] = field(default_factory=list)
    totals: dict[str, object] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "provenance": self.provenance,
            "symbol": self.symbol,
            "market": self.market,
            "source": self.source,
            "created_at_utc": self.created_at_utc,
            "klines_window": self.klines_window,
            "aggtrades_window": self.aggtrades_window,
            "archives": [asdict(item) for item in self.archives],
            "totals": self.totals,
            "notes": self.notes,
        }


def entry_from_download(
    *,
    kind: str,
    granularity: str,
    period: str,
    filename: str,
    relative_path: str,
    status: str,
    archive_path: str | None,
    checksum_path: str | None,
    sha256: str | None,
    content_length: int | None,
    error: str | None,
    record_count: int | None = None,
) -> ArchiveManifestEntry:
    size = content_length
    digest = sha256
    if archive_path and Path(archive_path).exists():
        size = Path(archive_path).stat().st_size
        if digest is None and status in {"downloaded", "already_verified"}:
            digest = sha256_file(Path(archive_path))
    return ArchiveManifestEntry(
        kind=kind,
        granularity=granularity,
        period=period,
        filename=filename,
        relative_path=relative_path,
        status=status,
        archive_path=archive_path,
        checksum_path=checksum_path,
        sha256=digest,
        size_bytes=size,
        record_count=record_count,
        error=error,
    )


def summarize(manifest: DatasetManifest) -> dict[str, object]:
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    total_bytes = 0
    total_records = 0
    for entry in manifest.archives:
        by_status[entry.status] = by_status.get(entry.status, 0) + 1
        by_kind[entry.kind] = by_kind.get(entry.kind, 0) + 1
        if entry.size_bytes:
            total_bytes += entry.size_bytes
        if entry.record_count:
            total_records += entry.record_count
    return {
        "archive_count": len(manifest.archives),
        "by_status": by_status,
        "by_kind": by_kind,
        "total_verified_bytes": total_bytes,
        "total_records": total_records,
    }


def write_manifest(manifest: DatasetManifest, path: Path | None = None) -> Path:
    manifest.totals = summarize(manifest)
    destination = assert_isolated_path(
        path
        or (
            MANIFESTS
            / f"dataset_manifest_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        )
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest.to_dict(), indent=2) + "\n", encoding="utf-8")
    return destination
