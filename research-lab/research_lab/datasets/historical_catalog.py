"""Register Binance Vision historical datasets with sealed-holdout exclusion.

Does not modify quote-tape catalog entries or session metadata.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from strategy import Bar

from research_lab import LAB_ROOT, PROJECT_ROOT
from research_lab.historical.constants import (
    DATASET_ROOT,
    HISTORICAL_DATASET_ID,
    MANIFESTS,
    PROCESSED,
    PROVENANCE,
    RESEARCH_CUTOFF,
    SYMBOL,
)
from research_lab.isolation import assert_isolated_path


def _project_relative(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)

HISTORICAL_CATALOG_PATH = LAB_ROOT / "datasets" / "historical_catalog.json"
SYNTHETIC_QUOTE_REASON = (
    "synthetic_historical_candle_quote; not bookTicker; Stage 3.2.4 research only"
)


@dataclass(frozen=True)
class HistoricalCandle:
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    open_time_raw: int | None = None
    close_time_raw: int | None = None
    timestamp_unit: str | None = None
    provenance: str = PROVENANCE

    def as_bar(self) -> Bar:
        return Bar(
            timestamp=self.close_time,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
        )


@dataclass(frozen=True)
class RegisteredHistoricalDataset:
    dataset_id: str
    provenance: str
    symbol: str
    market: str
    source: str
    research_start: datetime
    research_end_exclusive: datetime
    manifest_path: Path
    manifest_sha256: str
    processed_klines_dir: Path
    candles: tuple[HistoricalCandle, ...]
    excluded_at_or_after_cutoff: int
    notes: tuple[str, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Historical timestamps must be timezone-aware UTC.")
        return value.astimezone(timezone.utc)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Naive timestamp refused: {value!r}")
    return parsed.astimezone(timezone.utc)


def assert_before_cutoff(
    moment: datetime,
    *,
    cutoff: datetime = RESEARCH_CUTOFF,
    label: str = "observation",
) -> datetime:
    aware = parse_utc(moment)
    if aware >= cutoff:
        raise ValueError(
            f"{label} {aware.isoformat()} crosses sealed research cutoff "
            f"{cutoff.isoformat()}."
        )
    return aware


def candle_from_processed_row(row: dict[str, str]) -> HistoricalCandle:
    open_time = parse_utc(row["open_time_utc"])
    close_time = parse_utc(row["close_time_utc"])
    return HistoricalCandle(
        open_time=open_time,
        close_time=close_time,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row.get("volume") or 0.0),
        quote_volume=float(row.get("quote_volume") or 0.0),
        trades=int(float(row.get("trades") or 0)),
        open_time_raw=int(row["open_time_raw"]) if row.get("open_time_raw") else None,
        close_time_raw=int(row["close_time_raw"]) if row.get("close_time_raw") else None,
        timestamp_unit=row.get("timestamp_unit") or None,
        provenance=row.get("provenance") or PROVENANCE,
    )


def iter_processed_kline_csvs(directory: Path) -> Iterator[Path]:
    root = assert_isolated_path(directory)
    yield from sorted(root.glob("*.csv"))


def iter_klines_from_csv(
    path: Path,
    *,
    cutoff: datetime = RESEARCH_CUTOFF,
) -> Iterator[HistoricalCandle]:
    """Yield candles strictly before cutoff (open and close). Equality is excluded."""

    path = assert_isolated_path(path)
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            candle = candle_from_processed_row(row)
            if candle.close_time >= cutoff or candle.open_time >= cutoff:
                continue
            assert_before_cutoff(candle.close_time, cutoff=cutoff, label="close_time")
            assert_before_cutoff(candle.open_time, cutoff=cutoff, label="open_time")
            yield candle


@dataclass(frozen=True)
class CutoffValidatedCandles:
    """Candles proven to come from a cutoff-enforcing loader (formal scoring only)."""

    candles: tuple[HistoricalCandle, ...]
    research_cutoff: datetime
    source: str
    excluded_at_or_after_cutoff: int = 0

    def __post_init__(self) -> None:
        if self.research_cutoff != RESEARCH_CUTOFF and self.source.startswith("formal"):
            # Allow explicit cutoff override only when source documents it.
            pass
        for candle in self.candles:
            assert_before_cutoff(
                candle.close_time, cutoff=self.research_cutoff, label="close_time"
            )
            assert_before_cutoff(
                candle.open_time, cutoff=self.research_cutoff, label="open_time"
            )
        if self.candles:
            latest = max(item.close_time for item in self.candles)
            if latest >= self.research_cutoff:
                raise ValueError("CutoffValidatedCandles leaked holdout observations.")


def load_formal_research_candles(
    sources: Sequence[Path] | Path,
    *,
    cutoff: datetime = RESEARCH_CUTOFF,
) -> CutoffValidatedCandles:
    """Formal scoring loader: only cutoff-filtered candles via load_klines."""

    candles, excluded = load_klines(sources, cutoff=cutoff)
    return CutoffValidatedCandles(
        candles=candles,
        research_cutoff=cutoff,
        source="load_klines",
        excluded_at_or_after_cutoff=excluded,
    )


def load_klines(
    sources: Sequence[Path] | Path,
    *,
    cutoff: datetime = RESEARCH_CUTOFF,
) -> tuple[tuple[HistoricalCandle, ...], int]:
    """Load candles strictly before cutoff. Returns (candles, excluded_count)."""

    paths = [sources] if isinstance(sources, Path) else list(sources)
    kept: list[HistoricalCandle] = []
    excluded = 0
    previous: datetime | None = None
    for path in paths:
        if path.is_dir():
            file_iter: Iterable[Path] = iter_processed_kline_csvs(path)
        else:
            file_iter = [path]
        for file_path in file_iter:
            with file_path.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    candle = candle_from_processed_row(row)
                    if candle.close_time >= cutoff:
                        excluded += 1
                        continue
                    assert_before_cutoff(candle.close_time, cutoff=cutoff, label="close_time")
                    assert_before_cutoff(candle.open_time, cutoff=cutoff, label="open_time")
                    if previous is not None and candle.open_time < previous:
                        raise ValueError(
                            f"Out-of-order candle open_time {candle.open_time.isoformat()} "
                            f"after {previous.isoformat()}."
                        )
                    previous = candle.open_time
                    kept.append(candle)
    if kept:
        assert_before_cutoff(max(item.close_time for item in kept), cutoff=cutoff)
    return tuple(kept), excluded


def filter_aggtrade_timestamp(
    timestamp_utc: datetime,
    *,
    cutoff: datetime = RESEARCH_CUTOFF,
) -> bool:
    """Return True if the aggregate trade may enter research (strictly before cutoff)."""
    return parse_utc(timestamp_utc) < cutoff


def default_manifest_path() -> Path:
    manifests = sorted(MANIFESTS.glob("dataset_manifest_*.json"))
    if not manifests:
        raise FileNotFoundError(f"No historical manifests under {MANIFESTS}")
    return manifests[-1]


def load_historical_catalog(path: Path | None = None) -> dict[str, object]:
    catalog_path = assert_isolated_path(path or HISTORICAL_CATALOG_PATH)
    if not catalog_path.exists():
        return {"datasets": []}
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("datasets"), list):
        raise ValueError("historical_catalog.json must contain a datasets list.")
    return payload


def write_historical_catalog_entry(entry: dict[str, object], path: Path | None = None) -> Path:
    catalog_path = assert_isolated_path(path or HISTORICAL_CATALOG_PATH)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    payload = load_historical_catalog(catalog_path) if catalog_path.exists() else {"datasets": []}
    datasets = list(payload.get("datasets") or [])
    datasets = [row for row in datasets if row.get("dataset_id") != entry["dataset_id"]]
    datasets.append(entry)
    payload = {
        "version": 1,
        "notes": [
            "Historical Vision registration only. Does not modify quote-tape catalog.json.",
            f"Research cutoff exclusive end: {RESEARCH_CUTOFF.isoformat()}",
        ],
        "datasets": datasets,
    }
    catalog_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return catalog_path


def ensure_default_historical_registration() -> dict[str, object]:
    """Register the Stage 3.2.3 Vision dataset pointer without loading all candles."""

    manifest = default_manifest_path()
    digest = sha256_file(manifest)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if payload.get("provenance") != PROVENANCE:
        raise ValueError(f"Manifest provenance must be {PROVENANCE}.")
    if payload.get("source") != "https://data.binance.vision/":
        raise ValueError("Manifest source must be Binance Vision public archive.")
    if payload.get("symbol") != SYMBOL:
        raise ValueError(f"Manifest symbol must be {SYMBOL}.")
    entry = {
        "dataset_id": HISTORICAL_DATASET_ID,
        "provenance": PROVENANCE,
        "symbol": SYMBOL,
        "market": "spot",
        "source": "https://data.binance.vision/",
        "research_start": "2024-11-04T00:00:00+00:00",
        "research_end_exclusive": RESEARCH_CUTOFF.isoformat(),
        "manifest_path": _project_relative(manifest),
        "manifest_sha256": digest,
        "processed_klines_dir": _project_relative(PROCESSED / "klines_1m"),
        "dataset_root": _project_relative(DATASET_ROOT),
        "immutable_raw": True,
        "notes": (
            "BINANCE_SPOT_HISTORICAL only. Sealed holdout excluded at bind/load. "
            "Not order-book depth."
        ),
    }
    write_historical_catalog_entry(entry)
    return entry


def bind_historical_dataset(
    dataset_id: str = HISTORICAL_DATASET_ID,
    *,
    catalog_path: Path | None = None,
    load_candles: bool = False,
    candle_sources: Sequence[Path] | Path | None = None,
    cutoff: datetime = RESEARCH_CUTOFF,
) -> RegisteredHistoricalDataset:
    """Verify registration and optionally load holdout-filtered candles."""

    if dataset_id == HISTORICAL_DATASET_ID and not (
        catalog_path or HISTORICAL_CATALOG_PATH
    ).exists():
        ensure_default_historical_registration()
    elif dataset_id == HISTORICAL_DATASET_ID:
        # Refresh pointer if missing entry.
        payload = load_historical_catalog(catalog_path)
        if not any(row.get("dataset_id") == dataset_id for row in payload.get("datasets", [])):
            ensure_default_historical_registration()

    payload = load_historical_catalog(catalog_path)
    match = next(
        (row for row in payload.get("datasets", []) if row.get("dataset_id") == dataset_id),
        None,
    )
    if match is None:
        raise KeyError(f"Unknown historical dataset_id: {dataset_id}")

    from research_lab.datasets.catalog import _project_path

    manifest_path = _project_path(str(match["manifest_path"]))
    assert_isolated_path(manifest_path)
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    actual = sha256_file(manifest_path)
    expected = str(match["manifest_sha256"]).lower()
    if actual != expected:
        raise ValueError(
            f"Historical manifest SHA-256 mismatch: expected {expected}, got {actual}."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("provenance") != PROVENANCE:
        raise ValueError("Bound manifest provenance mismatch.")
    if manifest.get("source") != "https://data.binance.vision/":
        raise ValueError("Bound manifest source mismatch.")

    processed_dir = _project_path(str(match["processed_klines_dir"]))
    assert_isolated_path(processed_dir)

    candles: tuple[HistoricalCandle, ...] = ()
    excluded = 0
    if load_candles:
        formal = load_formal_research_candles(
            candle_sources if candle_sources is not None else processed_dir,
            cutoff=cutoff,
        )
        candles = formal.candles
        excluded = formal.excluded_at_or_after_cutoff

    return RegisteredHistoricalDataset(
        dataset_id=str(match["dataset_id"]),
        provenance=PROVENANCE,
        symbol=str(match["symbol"]),
        market=str(match.get("market") or "spot"),
        source=str(match["source"]),
        research_start=parse_utc(str(match["research_start"])),
        research_end_exclusive=parse_utc(str(match["research_end_exclusive"])),
        manifest_path=manifest_path,
        manifest_sha256=actual,
        processed_klines_dir=processed_dir,
        candles=candles,
        excluded_at_or_after_cutoff=excluded,
        notes=(str(match.get("notes") or ""),),
    )


def candles_from_bars(
    bars: Sequence[Bar],
    *,
    provenance: str = PROVENANCE,
    cutoff: datetime = RESEARCH_CUTOFF,
    allow_at_or_after_cutoff: bool = False,
) -> tuple[HistoricalCandle, ...]:
    """Build historical candles from Bars (fixture helper). Bar.timestamp = close_time.

    By default refuses bars at/after the sealed research cutoff so fixtures cannot
    silently include holdout times. Set allow_at_or_after_cutoff=True only for
    explicit holdout-rejection tests.
    """

    from datetime import timedelta

    out: list[HistoricalCandle] = []
    for bar in bars:
        close_time = parse_utc(bar.timestamp)
        # Binance 1m inclusive close is typically open + 59.999s.
        open_time = close_time - timedelta(milliseconds=59_999)
        if not allow_at_or_after_cutoff:
            assert_before_cutoff(close_time, cutoff=cutoff, label="close_time")
            assert_before_cutoff(open_time, cutoff=cutoff, label="open_time")
        out.append(
            HistoricalCandle(
                open_time=open_time,
                close_time=close_time,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=0.0,
                quote_volume=0.0,
                trades=0,
                provenance=provenance,
            )
        )
    return tuple(out)
