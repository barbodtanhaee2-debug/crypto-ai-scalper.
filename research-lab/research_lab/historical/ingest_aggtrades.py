"""Stream-parse verified aggTrades ZIPs without loading the full dataset into RAM."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

from research_lab.historical.constants import AGGTRADE_COLUMNS, PROCESSED, PROVENANCE
from research_lab.historical.timestamps import detect_unit, to_utc_datetime
from research_lab.isolation import assert_isolated_path


@dataclass
class AggTradeValidation:
    archive: str
    rows_read: int = 0
    rows_written: int = 0
    rejected: list[dict[str, object]] = field(default_factory=list)
    duplicate_ids: int = 0
    out_of_order: int = 0
    timestamp_unit_counts: dict[str, int] = field(default_factory=dict)
    first_timestamp_utc: str | None = None
    last_timestamp_utc: str | None = None
    first_agg_trade_id: int | None = None
    last_agg_trade_id: int | None = None


def _looks_like_header(row: list[str]) -> bool:
    if not row:
        return False
    first = row[0].strip().lower()
    return first in {"agg_trade_id", "a", "aggregate_trade_id"} or "agg_trade" in first


def iter_aggtrade_rows_from_zip(archive: Path) -> Iterator[list[str]]:
    with zipfile.ZipFile(archive) as bundle:
        names = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
        if not names:
            raise ValueError(f"No CSV member in {archive.name}.")
        with bundle.open(names[0]) as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8", newline="")
            reader = csv.reader(text)
            for row in reader:
                if not row or all(not cell.strip() for cell in row):
                    continue
                if _looks_like_header(row):
                    continue
                yield row


def _boolish(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"true", "1", "t", "yes"}:
        return True
    if lowered in {"false", "0", "f", "no"}:
        return False
    raise ValueError(f"Cannot parse boolean: {value!r}")


def parse_aggtrade_row(row: list[str]) -> dict[str, object]:
    if len(row) < 8:
        raise ValueError(f"AggTrade row has {len(row)} fields; expected 8.")
    agg_id = int(row[0])
    price = float(row[1])
    quantity = float(row[2])
    first_trade_id = int(row[3])
    last_trade_id = int(row[4])
    raw_ts = int(row[5])
    if price <= 0 or not (price == price):
        raise ValueError("price must be finite and positive.")
    if quantity < 0 or not (quantity == quantity):
        raise ValueError("quantity must be finite and nonnegative.")
    if first_trade_id > last_trade_id:
        raise ValueError("first_trade_id > last_trade_id.")
    unit = detect_unit(raw_ts)
    ts = to_utc_datetime(raw_ts, unit=unit)
    return {
        "agg_trade_id": agg_id,
        "price": price,
        "quantity": quantity,
        "first_trade_id": first_trade_id,
        "last_trade_id": last_trade_id,
        "timestamp_raw": raw_ts,
        "timestamp_unit": unit,
        "timestamp_utc": ts.isoformat(),
        "buyer_is_maker": _boolish(row[6]),
        "best_price_match": _boolish(row[7]),
        "provenance": PROVENANCE,
        "source_columns": list(AGGTRADE_COLUMNS),
    }


def ingest_aggtrade_archive(
    archive: Path,
    *,
    output_csv: Path | None = None,
    reject_cap: int = 200,
) -> AggTradeValidation:
    """Stream one archive to CSV. Tracks last-seen id/ts only (O(1) memory for ordering)."""

    archive = Path(archive)
    report = AggTradeValidation(archive=archive.name)
    destination = assert_isolated_path(
        output_csv
        or (PROCESSED / "aggTrades" / f"{archive.stem}.csv")
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    previous_id: int | None = None
    previous_ts: datetime | None = None
    # Rolling window for near-duplicate detection without loading all IDs.
    recent_ids: set[int] = set()
    recent_order: list[int] = []
    recent_cap = 50_000
    fieldnames = [
        "agg_trade_id",
        "timestamp_utc",
        "timestamp_raw",
        "timestamp_unit",
        "price",
        "quantity",
        "first_trade_id",
        "last_trade_id",
        "buyer_is_maker",
        "best_price_match",
        "provenance",
    ]
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in iter_aggtrade_rows_from_zip(archive):
            report.rows_read += 1
            try:
                parsed = parse_aggtrade_row(row)
            except (ValueError, TypeError, OverflowError) as error:
                if len(report.rejected) < reject_cap:
                    report.rejected.append({"row": row[:8], "error": str(error)})
                continue
            agg_id = int(parsed["agg_trade_id"])
            unit = str(parsed["timestamp_unit"])
            report.timestamp_unit_counts[unit] = report.timestamp_unit_counts.get(unit, 0) + 1
            if agg_id in recent_ids:
                report.duplicate_ids += 1
                if len(report.rejected) < reject_cap:
                    report.rejected.append({"row": row[:8], "error": "duplicate agg_trade_id"})
                continue
            ts = datetime.fromisoformat(str(parsed["timestamp_utc"]))
            if previous_ts is not None and ts < previous_ts:
                report.out_of_order += 1
                if len(report.rejected) < reject_cap:
                    report.rejected.append({"row": row[:8], "error": "out-of-order timestamp"})
                continue
            if previous_id is not None and agg_id < previous_id:
                report.out_of_order += 1
                if len(report.rejected) < reject_cap:
                    report.rejected.append({"row": row[:8], "error": "out-of-order agg_trade_id"})
                continue
            recent_ids.add(agg_id)
            recent_order.append(agg_id)
            if len(recent_order) > recent_cap:
                old = recent_order.pop(0)
                recent_ids.discard(old)
            previous_id = agg_id
            previous_ts = ts
            if report.first_agg_trade_id is None:
                report.first_agg_trade_id = agg_id
                report.first_timestamp_utc = str(parsed["timestamp_utc"])
            report.last_agg_trade_id = agg_id
            report.last_timestamp_utc = str(parsed["timestamp_utc"])
            writer.writerow(parsed)
            report.rows_written += 1
    meta_path = destination.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")
    return report
