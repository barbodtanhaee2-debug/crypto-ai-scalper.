#!/usr/bin/env python3
"""CLI for Stage 3.2.3 Binance Vision historical acquisition (research-lab only)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parent
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from research_lab.historical.acquisition import probe_storage, run_acquisition
from research_lab.historical.constants import DATASET_ROOT, ensure_layout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Acquire BTCUSDT Spot historical archives from data.binance.vision."
    )
    parser.add_argument(
        "command",
        choices=("probe", "download", "ingest-only", "full"),
        help="probe=disk/size only; download=fetch+verify; ingest-only=process local; full=download+ingest",
    )
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    ensure_layout()
    if args.command == "probe":
        report = probe_storage()
        print(json.dumps(report, indent=2))
        print(f"\nTarget directory: {DATASET_ROOT}")
        return 0 if report["safe"] else 2

    workers = args.workers
    if workers is None:
        from research_lab.historical.constants import MAX_WORKERS

        workers = MAX_WORKERS

    if args.command == "download":
        summary = run_acquisition(
            download=True, ingest=False, force=args.force, workers=workers
        )
    elif args.command == "ingest-only":
        summary = run_acquisition(
            download=False, ingest=True, force=False, workers=workers
        )
    else:
        summary = run_acquisition(
            download=True, ingest=True, force=args.force, workers=workers
        )

    print(json.dumps({
        "aborted": summary.aborted,
        "abort_reason": summary.abort_reason,
        "plan_safe": summary.plan_safe,
        "estimated_bytes": summary.estimated_bytes,
        "planned_archives": summary.planned_archives,
        "unavailable": summary.unavailable,
        "failed": summary.failed,
        "manifest_path": summary.manifest_path,
        "report_path": summary.report_path,
        "disk": summary.disk,
    }, indent=2))
    if summary.aborted or summary.failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
