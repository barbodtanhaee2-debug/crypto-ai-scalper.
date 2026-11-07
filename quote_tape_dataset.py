"""Read-only loader and validator for captured public quote-tape sessions."""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Mapping

from strategy import Bar

SYMBOL = "BTCUSDT"
CANDLE_INTERVAL_SECONDS = 60


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ValidationIssue:
    source: str
    line_number: int
    code: str
    message: str


@dataclass(frozen=True)
class QuoteRecord:
    sequence: int
    source_file: str
    line_number: int
    received_at: datetime
    bid: float
    ask: float
    spread: float
    update_id: int | None
    bid_quantity: float | None
    ask_quantity: float | None
    bid_raw: object
    ask_raw: object
    bid_quantity_raw: object
    ask_quantity_raw: object
    exchange_event_timestamp: datetime | None
    symbol: str | None
    raw: Mapping[str, object]


@dataclass(frozen=True)
class CandleRecord:
    sequence: int
    source_file: str
    line_number: int
    received_at: datetime
    open_time: datetime
    close_time: datetime
    exchange_event_timestamp: datetime | None
    open: float
    high: float
    low: float
    close: float
    base_volume: float | None
    quote_volume: float | None
    symbol: str | None
    is_closed: bool
    raw: Mapping[str, object]

    def as_bar(self) -> Bar:
        return Bar(self.close_time, self.open, self.high, self.low, self.close)


@dataclass
class QuoteTapeDataset:
    session_dir: Path
    session: dict[str, object]
    quotes: tuple[QuoteRecord, ...]
    candles: tuple[CandleRecord, ...]
    issues: tuple[ValidationIssue, ...]
    host_clock_offset_seconds: float | None
    clock_offset_method: str

    @property
    def session_id(self) -> str:
        return str(self.session.get("session_id") or self.session_dir.name)


def quote_file_paths(session_dir: Path) -> tuple[Path, ...]:
    directory = Path(session_dir)
    files: list[Path] = []
    base = directory / "quotes.jsonl"
    if base.exists():
        files.append(base)
    parts: list[tuple[int, Path]] = []
    for path in directory.glob("quotes.part*.jsonl"):
        suffix = path.stem.split("part")[-1]
        try:
            parts.append((int(suffix), path))
        except ValueError:
            parts.append((10**9, path))
    parts.sort()
    files.extend(path for _index, path in parts)
    return tuple(files)


def _iter_jsonl(path: Path) -> Iterator[tuple[int, dict | None, str | None]]:
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                yield line_number, None, f"Malformed JSON: {error}."
                continue
            if not isinstance(payload, dict):
                yield line_number, None, "Record is not a JSON object."
                continue
            yield line_number, payload, None


def _finite_positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def load_quote_tape_session(session_dir: str | Path) -> QuoteTapeDataset:
    """Load a captured session without modifying source files."""
    directory = Path(session_dir).expanduser().resolve()
    session_path = directory / "session.json"
    issues: list[ValidationIssue] = []
    if not session_path.exists():
        raise FileNotFoundError(f"session.json is missing in {directory}.")
    session = json.loads(session_path.read_text(encoding="utf-8"))
    if not isinstance(session, dict):
        raise ValueError("session.json must contain a JSON object.")

    quotes: list[QuoteRecord] = []
    previous_update_id: int | None = None
    seen_update_ids: set[int] = set()
    for path in quote_file_paths(directory):
        for line_number, payload, error in _iter_jsonl(path):
            if error is not None:
                issues.append(
                    ValidationIssue(path.name, line_number, "malformed_quote", error)
                )
                continue
            assert payload is not None
            received_at = _parse_datetime(payload.get("received_at_utc"))
            bid = _finite_positive(payload.get("bid"))
            ask = _finite_positive(payload.get("ask"))
            update_id = payload.get("update_id")
            symbol = payload.get("symbol")
            if received_at is None:
                issues.append(
                    ValidationIssue(
                        path.name, line_number, "invalid_timestamp",
                        "Quote received_at_utc is missing or invalid.",
                    )
                )
                continue
            if symbol not in (None, SYMBOL) and symbol != SYMBOL:
                issues.append(
                    ValidationIssue(
                        path.name, line_number, "symbol_mismatch",
                        f"Quote symbol {symbol!r} is not {SYMBOL}.",
                    )
                )
            if bid is None or ask is None:
                issues.append(
                    ValidationIssue(
                        path.name, line_number, "invalid_bid_ask",
                        "Quote bid/ask is missing, non-positive, or non-numeric.",
                    )
                )
                continue
            if ask <= bid:
                issues.append(
                    ValidationIssue(
                        path.name, line_number, "crossed_market",
                        "Quote ask is not greater than bid.",
                    )
                )
                continue
            if isinstance(update_id, bool) or (
                update_id is not None and not isinstance(update_id, int)
            ):
                issues.append(
                    ValidationIssue(
                        path.name, line_number, "invalid_update_id",
                        "Quote update_id is not an integer.",
                    )
                )
                update_id = None
            if isinstance(update_id, int):
                if update_id in seen_update_ids:
                    issues.append(
                        ValidationIssue(
                            path.name, line_number, "duplicate_update_id",
                            f"Duplicate update_id {update_id}.",
                        )
                    )
                elif previous_update_id is not None and update_id < previous_update_id:
                    issues.append(
                        ValidationIssue(
                            path.name, line_number, "out_of_order_update_id",
                            f"Update id {update_id} precedes {previous_update_id}.",
                        )
                    )
                seen_update_ids.add(update_id)
                previous_update_id = update_id
            event_time = _parse_datetime(payload.get("exchange_event_timestamp"))
            quotes.append(
                QuoteRecord(
                    sequence=len(quotes),
                    source_file=path.name,
                    line_number=line_number,
                    received_at=received_at,
                    bid=bid,
                    ask=ask,
                    spread=ask - bid,
                    update_id=update_id if isinstance(update_id, int) else None,
                    bid_quantity=_finite_positive(payload.get("bid_quantity")),
                    ask_quantity=_finite_positive(payload.get("ask_quantity")),
                    bid_raw=payload.get("bid_raw"),
                    ask_raw=payload.get("ask_raw"),
                    bid_quantity_raw=payload.get("bid_quantity_raw"),
                    ask_quantity_raw=payload.get("ask_quantity_raw"),
                    exchange_event_timestamp=event_time,
                    symbol=symbol if isinstance(symbol, str) else None,
                    raw=payload,
                )
            )

    candles: list[CandleRecord] = []
    candle_path = directory / "candles.jsonl"
    previous_open: datetime | None = None
    if candle_path.exists():
        for line_number, payload, error in _iter_jsonl(candle_path):
            if error is not None:
                issues.append(
                    ValidationIssue("candles.jsonl", line_number, "malformed_candle", error)
                )
                continue
            assert payload is not None
            received_at = _parse_datetime(payload.get("received_at_utc"))
            open_time = _parse_datetime(payload.get("open_time_utc"))
            close_time = _parse_datetime(payload.get("close_time_utc"))
            symbol = payload.get("symbol")
            if received_at is None or open_time is None or close_time is None:
                issues.append(
                    ValidationIssue(
                        "candles.jsonl", line_number, "invalid_timestamp",
                        "Candle timestamps are missing or invalid.",
                    )
                )
                continue
            if payload.get("is_closed") is not True:
                issues.append(
                    ValidationIssue(
                        "candles.jsonl", line_number, "incomplete_candle",
                        "Stored candle is not confirmed complete.",
                    )
                )
                continue
            if symbol != SYMBOL:
                issues.append(
                    ValidationIssue(
                        "candles.jsonl", line_number, "symbol_mismatch",
                        f"Candle symbol {symbol!r} is not {SYMBOL}.",
                    )
                )
            ohlc = (
                _finite_positive(payload.get("open")),
                _finite_positive(payload.get("high")),
                _finite_positive(payload.get("low")),
                _finite_positive(payload.get("close")),
            )
            if any(value is None for value in ohlc):
                issues.append(
                    ValidationIssue(
                        "candles.jsonl", line_number, "invalid_ohlc",
                        "Candle OHLC is missing or invalid.",
                    )
                )
                continue
            open_price, high, low, close = ohlc
            if low > min(open_price, close) or high < max(open_price, close) or high < low:
                issues.append(
                    ValidationIssue(
                        "candles.jsonl", line_number, "inconsistent_ohlc",
                        "Candle OHLC values are inconsistent.",
                    )
                )
                continue
            if previous_open is not None:
                elapsed = (open_time - previous_open).total_seconds()
                if elapsed == 0:
                    issues.append(
                        ValidationIssue(
                            "candles.jsonl", line_number, "duplicate_candle",
                            "Duplicate candle open time.",
                        )
                    )
                elif elapsed != CANDLE_INTERVAL_SECONDS:
                    issues.append(
                        ValidationIssue(
                            "candles.jsonl", line_number, "missing_interval",
                            f"Candle open gap is {elapsed} seconds; expected "
                            f"{CANDLE_INTERVAL_SECONDS}.",
                        )
                    )
            previous_open = open_time
            candles.append(
                CandleRecord(
                    sequence=len(candles),
                    source_file="candles.jsonl",
                    line_number=line_number,
                    received_at=received_at,
                    open_time=open_time,
                    close_time=close_time,
                    exchange_event_timestamp=_parse_datetime(
                        payload.get("exchange_event_timestamp")
                    ),
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    base_volume=_finite_positive(payload.get("base_volume")),
                    quote_volume=_finite_positive(payload.get("quote_volume")),
                    symbol=symbol if isinstance(symbol, str) else None,
                    is_closed=True,
                    raw=payload,
                )
            )
    else:
        issues.append(
            ValidationIssue("candles.jsonl", 0, "missing_file", "candles.jsonl is missing.")
        )

    offsets = [
        (candle.exchange_event_timestamp - candle.received_at).total_seconds()
        for candle in candles
        if candle.exchange_event_timestamp is not None
    ]
    host_offset = statistics.median(offsets) if offsets else None
    method = (
        "median(kline_E - candle_received_at_utc); quote bookTicker E was not supplied"
        if offsets else
        "unavailable; captured klines did not include exchange event time E"
    )

    started = _parse_datetime(session.get("started_at_utc"))
    ended = _parse_datetime(session.get("ended_at_utc"))
    if quotes:
        first_received = quotes[0].received_at
        last_received = quotes[-1].received_at
        if started is not None and first_received + timedelta(seconds=2) < started:
            issues.append(
                ValidationIssue(
                    "session.json",
                    0,
                    "session_boundary",
                    "First quote arrives more than two seconds before started_at_utc.",
                )
            )
        if ended is not None and last_received > ended + timedelta(seconds=2):
            issues.append(
                ValidationIssue(
                    "session.json",
                    0,
                    "session_boundary",
                    "Last quote arrives more than two seconds after ended_at_utc.",
                )
            )
        if last_received < first_received:
            issues.append(
                ValidationIssue(
                    "quotes.jsonl",
                    0,
                    "receive_order",
                    "Last quote received_at is before the first quote.",
                )
            )

    return QuoteTapeDataset(
        session_dir=directory,
        session=session,
        quotes=tuple(quotes),
        candles=tuple(candles),
        issues=tuple(issues),
        host_clock_offset_seconds=host_offset,
        clock_offset_method=method,
    )


def dataset_summary(dataset: QuoteTapeDataset) -> dict[str, object]:
    issue_counts: dict[str, int] = {}
    for issue in dataset.issues:
        issue_counts[issue.code] = issue_counts.get(issue.code, 0) + 1
    quotes_with_e = sum(
        1 for quote in dataset.quotes if quote.exchange_event_timestamp is not None
    )
    candles_with_e = sum(
        1 for candle in dataset.candles if candle.exchange_event_timestamp is not None
    )
    return {
        "session_id": dataset.session_id,
        "session_dir": str(dataset.session_dir),
        "quotes": len(dataset.quotes),
        "candles": len(dataset.candles),
        "quotes_with_exchange_event_timestamp": quotes_with_e,
        "candles_with_exchange_event_timestamp": candles_with_e,
        "host_clock_offset_seconds": dataset.host_clock_offset_seconds,
        "clock_offset_method": dataset.clock_offset_method,
        "issue_count": len(dataset.issues),
        "issues_by_code": issue_counts,
        "issues": [
            {
                "source": issue.source,
                "line_number": issue.line_number,
                "code": issue.code,
                "message": issue.message,
            }
            for issue in dataset.issues[:50]
        ],
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--output", help="Write JSON summary without modifying the session.")
    args = parser.parse_args()
    dataset = load_quote_tape_session(args.session)
    summary = dataset_summary(dataset)
    text = json.dumps(summary, indent=2) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()

