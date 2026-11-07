"""Orchestrate plan → disk gate → download → ingest → manifest → report."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from research_lab.historical.constants import (
    AGGTRADES_END_EXCLUSIVE,
    AGGTRADES_START,
    DISK_RESERVE_BYTES,
    KLINES_END_EXCLUSIVE,
    KLINES_START,
    LOGS,
    MANIFESTS,
    MAX_WORKERS,
    PROVENANCE,
    REPORTS,
    ensure_layout,
)
from research_lab.historical.disk import disk_safety_report, format_bytes, free_bytes
from research_lab.historical.downloader import (
    build_download_plan,
    download_many,
    write_download_log,
)
from research_lab.historical.ingest_aggtrades import ingest_aggtrade_archive
from research_lab.historical.ingest_klines import ingest_kline_archive
from research_lab.historical.manifest import (
    DatasetManifest,
    entry_from_download,
    write_manifest,
)
from research_lab.historical.planning import plan_archives
from research_lab.isolation import assert_isolated_path

logger = logging.getLogger(__name__)


@dataclass
class AcquisitionSummary:
    planned_archives: int = 0
    download_results: list[dict[str, object]] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    kline_ingest: list[dict[str, object]] = field(default_factory=list)
    aggtrade_ingest: list[dict[str, object]] = field(default_factory=list)
    disk: dict[str, object] = field(default_factory=dict)
    plan_safe: bool = False
    plan_reasons: list[str] = field(default_factory=list)
    estimated_bytes: int = 0
    manifest_path: str | None = None
    report_path: str | None = None
    aborted: bool = False
    abort_reason: str | None = None


def build_full_archive_list():
    klines = plan_archives("klines_1m", KLINES_START, KLINES_END_EXCLUSIVE)
    aggs = plan_archives("aggTrades", AGGTRADES_START, AGGTRADES_END_EXCLUSIVE)
    return klines + aggs


def probe_storage(*, workers_probe: bool = True) -> dict[str, object]:
    ensure_layout()
    archives = build_full_archive_list()
    free = free_bytes()
    plan = build_download_plan(
        archives,
        free_bytes=free,
        reserve_bytes=DISK_RESERVE_BYTES,
    )
    safety = disk_safety_report(
        estimated_download_bytes=plan.estimated_bytes,
        free=free,
        reserve_bytes=DISK_RESERVE_BYTES,
    )
    return {
        "archive_count": len(archives),
        "klines_count": sum(1 for item in archives if item.kind == "klines_1m"),
        "aggtrades_count": sum(1 for item in archives if item.kind == "aggTrades"),
        "estimated_bytes": plan.estimated_bytes,
        "estimated_human": format_bytes(plan.estimated_bytes),
        "known_sizes": plan.known_sizes,
        "unknown_sizes": plan.unknown_sizes,
        "safe": plan.safe,
        "reasons": plan.reasons,
        "disk": safety,
        "target_directory": str(Path(safety["path"])),
        "workers": MAX_WORKERS if workers_probe else 1,
        "windows": {
            "klines": {
                "start": KLINES_START.isoformat(),
                "end_exclusive": KLINES_END_EXCLUSIVE.isoformat(),
            },
            "aggTrades": {
                "start": AGGTRADES_START.isoformat(),
                "end_exclusive": AGGTRADES_END_EXCLUSIVE.isoformat(),
            },
        },
        "provenance": PROVENANCE,
    }


def run_acquisition(
    *,
    download: bool = True,
    ingest: bool = True,
    force: bool = False,
    workers: int = MAX_WORKERS,
) -> AcquisitionSummary:
    ensure_layout()
    summary = AcquisitionSummary()
    archives = build_full_archive_list()
    summary.planned_archives = len(archives)
    free = free_bytes()
    plan = build_download_plan(
        archives,
        free_bytes=free,
        reserve_bytes=DISK_RESERVE_BYTES,
    )
    summary.estimated_bytes = plan.estimated_bytes
    summary.plan_safe = plan.safe
    summary.plan_reasons = list(plan.reasons)
    summary.disk = disk_safety_report(
        estimated_download_bytes=plan.estimated_bytes,
        free=free,
        reserve_bytes=DISK_RESERVE_BYTES,
    )

    if download and not plan.safe:
        summary.aborted = True
        summary.abort_reason = "; ".join(plan.reasons) or "Disk safety check failed."
        report_path = _write_report(summary)
        summary.report_path = str(report_path)
        return summary

    results = []
    if download:
        results = download_many(archives, workers=workers, force=force)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        write_download_log(LOGS / f"download_{stamp}.json", results)
    else:
        from research_lab.historical.downloader import DownloadResult, raw_dir_for

        for spec in archives:
            archive_path = raw_dir_for(spec.kind) / spec.filename
            checksum_path = archive_path.with_name(spec.checksum_filename)
            if archive_path.exists() and checksum_path.exists():
                results.append(
                    DownloadResult(
                        spec=spec,
                        status="already_verified",
                        archive_path=str(archive_path),
                        checksum_path=str(checksum_path),
                        content_length=archive_path.stat().st_size,
                    )
                )
            else:
                results.append(
                    DownloadResult(
                        spec=spec,
                        status="unavailable",
                        error="Local archive/checksum missing; download=False.",
                    )
                )

    for result in results:
        payload = {
            **asdict(result),
            "spec": asdict(result.spec),
        }
        summary.download_results.append(payload)
        if result.status == "unavailable":
            summary.unavailable.append(result.spec.filename)
        elif result.status == "failed":
            summary.failed.append(result.spec.filename)

    manifest = DatasetManifest(
        klines_window={
            "start": KLINES_START.isoformat(),
            "end_exclusive": KLINES_END_EXCLUSIVE.isoformat(),
            "interval": "1m",
        },
        aggtrades_window={
            "start": AGGTRADES_START.isoformat(),
            "end_exclusive": AGGTRADES_END_EXCLUSIVE.isoformat(),
        },
    )
    if summary.unavailable:
        manifest.notes.append(
            "Unavailable archives (explicit): " + ", ".join(summary.unavailable)
        )
    if summary.failed:
        manifest.notes.append("Failed archives: " + ", ".join(summary.failed))

    for result in results:
        record_count = None
        if (
            ingest
            and result.status in {"downloaded", "already_verified"}
            and result.archive_path
        ):
            archive = Path(result.archive_path)
            try:
                if result.spec.kind == "klines_1m":
                    report = ingest_kline_archive(archive)
                    summary.kline_ingest.append(asdict(report))
                    record_count = report.rows_written
                else:
                    report = ingest_aggtrade_archive(archive)
                    summary.aggtrade_ingest.append(asdict(report))
                    record_count = report.rows_written
            except Exception as error:  # noqa: BLE001 — surface per-archive failures
                logger.exception("Ingest failed for %s", archive.name)
                summary.failed.append(f"ingest:{archive.name}:{error}")
                manifest.notes.append(f"Ingest failed for {archive.name}: {error}")
        manifest.archives.append(
            entry_from_download(
                kind=result.spec.kind,
                granularity=result.spec.granularity,
                period=result.spec.period,
                filename=result.spec.filename,
                relative_path=result.spec.relative_path,
                status=result.status,
                archive_path=result.archive_path,
                checksum_path=result.checksum_path,
                sha256=result.sha256,
                content_length=result.content_length,
                error=result.error,
                record_count=record_count,
            )
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_path = write_manifest(manifest, MANIFESTS / f"dataset_manifest_{stamp}.json")
    summary.manifest_path = str(manifest_path)
    report_path = _write_report(summary)
    summary.report_path = str(report_path)
    return summary


def _write_report(summary: AcquisitionSummary) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = assert_isolated_path(REPORTS / f"acquisition_report_{stamp}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(summary), indent=2) + "\n", encoding="utf-8")
    # Also keep a human markdown snapshot for Stage 3.2.3.
    md = assert_isolated_path(REPORTS / "STAGE3.2.3_ACQUISITION_REPORT.md")
    lines = [
        "# Stage 3.2.3 — Historical acquisition report",
        "",
        f"- Written: `{datetime.now(timezone.utc).isoformat()}`",
        f"- Provenance: `{PROVENANCE}`",
        f"- Planned archives: {summary.planned_archives}",
        f"- Estimated download: {format_bytes(summary.estimated_bytes)}",
        f"- Plan safe: {summary.plan_safe}",
        f"- Aborted: {summary.aborted}",
    ]
    if summary.abort_reason:
        lines.append(f"- Abort reason: {summary.abort_reason}")
    lines.extend(
        [
            f"- Unavailable: {len(summary.unavailable)}",
            f"- Failed: {len(summary.failed)}",
            f"- Manifest: `{summary.manifest_path}`",
            "",
            "## Disk",
            "",
            "```json",
            json.dumps(summary.disk, indent=2),
            "```",
            "",
            "## Unavailable archives",
            "",
        ]
    )
    if summary.unavailable:
        lines.extend(f"- `{name}`" for name in summary.unavailable)
    else:
        lines.append("- (none)")
    lines.extend(["", "## Failed archives", ""])
    if summary.failed:
        lines.extend(f"- `{name}`" for name in summary.failed)
    else:
        lines.append("- (none)")
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "- Current incomplete UTC day is not fabricated.",
            "- Historical Spot archives are not order-book snapshots.",
            "- Live quote-tape sessions remain separate (`BINANCE_SPOT_LIVE_CAPTURE`).",
            "",
        ]
    )
    md.write_text("\n".join(lines), encoding="utf-8")
    return path
