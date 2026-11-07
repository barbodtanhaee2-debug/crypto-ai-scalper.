"""Explain Stage 2.4 stale candle decisions without changing production replay."""

from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

from quote_tape_dataset import QuoteRecord, QuoteTapeDataset
from quote_tape_replay import (
    QUOTE_STALE_SECONDS,
    candle_decision_time,
    candle_order_time,
    load_reconnect_gaps,
    quote_order_time,
    recorded_quote_health,
)

from research_lab.backtesting.clocks import HOST_RECEIVE, KLINE_EVENT, research_decision_time
from research_lab.isolation import assert_isolated_path


def _in_gap(timestamp: datetime, gaps: list[dict[str, object]]) -> bool:
    for gap in gaps:
        start = gap.get("start")
        end = gap.get("end")
        if isinstance(start, datetime) and isinstance(end, datetime):
            if start <= timestamp <= end:
                return True
    return False


def analyze_stale_decisions(
    dataset: QuoteTapeDataset,
    *,
    stale_after_seconds: float = QUOTE_STALE_SECONDS,
) -> dict[str, object]:
    gaps = load_reconnect_gaps(dataset.session_dir)
    timeline: list[tuple[datetime, int, int, str, object]] = []
    seen: set[int] = set()
    for quote in dataset.quotes:
        if quote.update_id is not None and quote.update_id in seen:
            continue
        if quote.update_id is not None:
            seen.add(quote.update_id)
        timeline.append((quote_order_time(quote), 0, quote.sequence, "quote", quote))
    for candle in dataset.candles:
        timeline.append((candle_order_time(candle), 1, candle.sequence, "candle", candle))
    timeline.sort()

    last_quote: QuoteRecord | None = None
    last_quote_time: datetime | None = None
    rows: list[dict[str, object]] = []
    for event_time, _kind, _seq, name, payload in timeline:
        if name == "quote":
            last_quote = payload
            last_quote_time = event_time
            continue
        candle = payload
        kline_time = candle_decision_time(candle)
        host_time = candle.received_at
        if last_quote is None or last_quote_time is None:
            rows.append(
                {
                    "open_time": candle.open_time.isoformat(),
                    "classification": "missing_quote",
                    "kline_e": kline_time.isoformat(),
                    "host_receive": host_time.isoformat(),
                }
            )
            continue
        age_kline = (kline_time - last_quote_time).total_seconds()
        age_host = (host_time - last_quote_time).total_seconds()
        health_kline = recorded_quote_health(
            last_quote,
            replay_now=kline_time,
            quote_replay_time=last_quote_time,
            stale_after_seconds=stale_after_seconds,
        )
        health_host = recorded_quote_health(
            last_quote,
            replay_now=host_time,
            quote_replay_time=last_quote_time,
            stale_after_seconds=stale_after_seconds,
        )
        e_before_host = (
            candle.exchange_event_timestamp is not None
            and candle.exchange_event_timestamp < candle.received_at
        )
        if health_kline.get("healthy") is not True:
            if age_kline < 0:
                classification = "negative_age_mixed_clock"
            elif age_kline > stale_after_seconds:
                classification = "age_exceeds_threshold_on_kline_e"
            else:
                classification = "unhealthy_other"
        else:
            classification = "fresh_on_kline_e"
        rows.append(
            {
                "open_time": candle.open_time.isoformat(),
                "close_time": candle.close_time.isoformat(),
                "kline_e": kline_time.isoformat(),
                "host_receive": host_time.isoformat(),
                "last_quote_received": last_quote_time.isoformat(),
                "age_vs_kline_e_seconds": age_kline,
                "age_vs_host_receive_seconds": age_host,
                "fresh_on_kline_e": health_kline.get("healthy") is True,
                "fresh_on_host_receive": health_host.get("healthy") is True,
                "kline_e_before_host_receive": e_before_host,
                "quote_in_reconnect_gap": _in_gap(last_quote_time, gaps)
                or _in_gap(host_time, gaps),
                "classification": classification,
            }
        )

    stale_kline = [row for row in rows if row.get("fresh_on_kline_e") is False]
    stale_host = [row for row in rows if row.get("fresh_on_host_receive") is False]
    classes = Counter(str(row.get("classification")) for row in stale_kline)
    ages_kline = [float(row["age_vs_kline_e_seconds"]) for row in stale_kline]
    ages_host_when_kline_stale = [
        float(row["age_vs_host_receive_seconds"]) for row in stale_kline
    ]
    would_clear_on_host = sum(
        1 for row in stale_kline if row.get("fresh_on_host_receive") is True
    )
    still_stale_on_host = sum(
        1 for row in stale_kline if row.get("fresh_on_host_receive") is False
    )
    negative = sum(1 for row in stale_kline if float(row["age_vs_kline_e_seconds"]) < 0)
    over_threshold = sum(
        1
        for row in stale_kline
        if float(row["age_vs_kline_e_seconds"]) > stale_after_seconds
    )
    e_before = sum(1 for row in rows if row.get("kline_e_before_host_receive"))
    kline_offsets = []
    for candle in dataset.candles:
        if candle.exchange_event_timestamp is not None:
            kline_offsets.append(
                (candle.exchange_event_timestamp - candle.received_at).total_seconds()
            )
    root_cause = (
        "The 3 s freshness rule is applied to (kline E − host quote received_at). "
        "Those clocks are not the same. Negative ages fail closed; ages above 3 s "
        "also fail. This is mixed-clock replay synchronization, not proof that the "
        "quote stream was idle for 3 s, and not a reason to widen the production "
        "freshness threshold."
    )
    return {
        "session_id": dataset.session_id,
        "stale_after_seconds": stale_after_seconds,
        "candles": len(dataset.candles),
        "quotes": len(dataset.quotes),
        "reconnect_gaps": len(gaps),
        "stale_on_kline_event_clock": len(stale_kline),
        "stale_on_host_receive_clock": len(stale_host),
        "stale_kline_that_are_fresh_on_host_clock": would_clear_on_host,
        "stale_kline_that_remain_stale_on_host_clock": still_stale_on_host,
        "negative_age_mixed_clock": negative,
        "age_exceeds_3s_on_kline_e": over_threshold,
        "candles_with_kline_e_before_host_receive": e_before,
        "stale_classifications": dict(classes),
        "median_kline_e_minus_host_receive": (
            statistics.median(kline_offsets) if kline_offsets else None
        ),
        "stale_age_vs_kline_e": _age_stats(ages_kline),
        "host_age_on_those_same_bars": _age_stats(ages_host_when_kline_stale),
        "root_cause": root_cause,
        "proposed_correction": {
            "policy": HOST_RECEIVE,
            "do_not": [
                "Increase the 3 s production freshness threshold",
                "Apply kline median offset to bookTicker timestamps",
                "Invent bookTicker E",
                "Silently drop stale decisions",
            ],
            "do": [
                "Evaluate candles only after host receive (already true of event order)",
                "Measure freshness on the host clock",
                "Keep kline_event runs as a Stage 2.4 reproduction",
                "Flag residual host-clock stale bars (true gaps / delayed quotes)",
            ],
        },
        "stage24_expected_stale_if_same_session": 69,
        "clock_policies": [KLINE_EVENT, HOST_RECEIVE],
        "stale_rows": stale_kline,
    }


def _age_stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"count": 0, "min": None, "median": None, "max": None}
    return {
        "count": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def write_stale_report(analysis: dict[str, object], path: str | Path) -> Path:
    output = assert_isolated_path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")
    return output
