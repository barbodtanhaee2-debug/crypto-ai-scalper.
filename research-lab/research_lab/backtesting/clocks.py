"""Decision-clock policies for research replay. Production replay is unchanged."""

from __future__ import annotations

from quote_tape_dataset import CandleRecord
from quote_tape_replay import candle_decision_time

KLINE_EVENT = "kline_event"
HOST_RECEIVE = "host_receive"
CLOCK_POLICIES = frozenset({KLINE_EVENT, HOST_RECEIVE})

CLOCK_NOTES = {
    KLINE_EVENT: (
        "Stage 2.4 policy: strategy decision_time is kline E when present. "
        "Freshness mixes kline E with host quote received_at. Do not use this "
        "as the default for new research."
    ),
    HOST_RECEIVE: (
        "Lab correction: decision_time is the host receive time of the closed "
        "candle. Freshness compares two host clocks. The 3 s threshold is unchanged. "
        "kline E is recorded for audit only and is not applied to quotes."
    ),
}


def research_decision_time(candle: CandleRecord, policy: str):
    if policy == HOST_RECEIVE:
        return candle.received_at
    if policy == KLINE_EVENT:
        return candle_decision_time(candle)
    raise ValueError(f"Unknown decision clock policy: {policy}")
