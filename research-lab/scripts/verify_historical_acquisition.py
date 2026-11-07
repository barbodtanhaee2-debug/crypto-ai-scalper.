"""Independent post-download verification (checksums, schemas, counts)."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

from research_lab.historical.checksums import parse_checksum_text, verify_archive
from research_lab.historical.constants import PROCESSED, RAW_AGGTRADES, RAW_KLINES, REPORTS


def count_rows(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            next(reader, None)
            for _ in reader:
                total += 1
    return total


def schema_sample(archive: Path) -> dict[str, object]:
    with zipfile.ZipFile(archive) as bundle:
        name = next(n for n in bundle.namelist() if n.lower().endswith(".csv"))
        with bundle.open(name) as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8", newline="")
            first = text.readline().strip()
            second = text.readline().strip()
    return {
        "archive": archive.name,
        "member": name,
        "first_line_fields": len(first.split(",")),
        "first_line_preview": first[:160],
        "second_line_preview": second[:160],
        "has_header_guess": first.lower().startswith("open_time")
        or first.lower().startswith("agg_trade"),
    }


def main() -> int:
    manifests = sorted(
        Path(r"F:\crypto-ai-scalper\research-lab\data\binance_spot\BTCUSDT\manifests").glob(
            "dataset_manifest_*.json"
        )
    )
    if not manifests:
        raise SystemExit("No manifest found.")
    manifest = json.loads(manifests[-1].read_text(encoding="utf-8"))

    verified = 0
    mismatches: list[str] = []
    missing_files: list[str] = []
    status_counts: dict[str, int] = {}
    record_totals: dict[str, int] = {"klines_1m": 0, "aggTrades": 0}

    for entry in manifest["archives"]:
        status_counts[entry["status"]] = status_counts.get(entry["status"], 0) + 1
        if entry["status"] not in {"downloaded", "already_verified"}:
            continue
        archive = Path(entry["archive_path"])
        checksum = Path(entry["checksum_path"])
        if not archive.exists() or not checksum.exists():
            missing_files.append(entry["filename"])
            continue
        expected = parse_checksum_text(
            checksum.read_text(encoding="utf-8"),
            expected_filename=entry["filename"],
        )
        try:
            actual = verify_archive(archive, expected)
        except ValueError as error:
            mismatches.append(str(error))
            continue
        if entry.get("sha256") and entry["sha256"].lower() != actual.lower():
            mismatches.append(f"manifest digest mismatch {entry['filename']}")
            continue
        verified += 1
        if entry.get("record_count"):
            kind = entry["kind"]
            record_totals[kind] = record_totals.get(kind, 0) + int(entry["record_count"])

    proc_k = sorted((PROCESSED / "klines_1m").glob("*.csv"))
    proc_a = sorted((PROCESSED / "aggTrades").glob("*.csv"))
    out = {
        "manifest": str(manifests[-1]),
        "manifest_archives": len(manifest["archives"]),
        "status_counts": status_counts,
        "independently_verified_sha256": verified,
        "checksum_mismatches": mismatches,
        "missing_files": missing_files,
        "manifest_record_totals": record_totals,
        "processed_csv_files": {"klines_1m": len(proc_k), "aggTrades": len(proc_a)},
        "processed_row_counts_independent": {
            "klines_1m": count_rows(proc_k),
            "aggTrades": count_rows(proc_a),
        },
        "schema_samples": {
            "klines": schema_sample(next(RAW_KLINES.glob("*.zip"))),
            "aggTrades": schema_sample(next(RAW_AGGTRADES.glob("*.zip"))),
        },
        "manifest_totals": manifest.get("totals"),
    }
    destination = REPORTS / "INDEPENDENT_VERIFICATION.json"
    destination.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))
    if mismatches or missing_files or verified != len(manifest["archives"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
