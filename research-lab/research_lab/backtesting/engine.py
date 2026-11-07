"""Lab quote replay. Fork of QuoteTapeReplay.run with a pluggable decision clock.

Does not modify quote_tape_replay.py. Default Stage 2.4 behaviour is available
via decision_clock=kline_event.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from backtester import BacktestSettings, _position_for_strategy
from historical_replay import resolve_replay_journal
from paper_broker import PaperBroker, PaperBrokerSettings
from quote_tape_dataset import QuoteRecord, QuoteTapeDataset
from quote_tape_replay import (
    CLOCK_ASSUMPTIONS,
    LIVE_JOURNAL,
    MARK_INTERVAL_SECONDS,
    QuoteTapeReplay,
    _iso,
    candle_order_time,
    load_reconnect_gaps,
    quote_order_time,
    recorded_quote_health,
    settings_equity,
)

from research_lab.backtesting.clocks import (
    CLOCK_NOTES,
    CLOCK_POLICIES,
    HOST_RECEIVE,
    research_decision_time,
)
from research_lab.isolation import assert_isolated_path
from research_lab.strategies.adapters import ProductionStrategyAdapter
from research_lab.strategies.registry import make_research_strategy


@dataclass
class ResearchQuoteReplay(QuoteTapeReplay):
    strategy_object: object | None = None
    decision_clock: str = HOST_RECEIVE

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.decision_clock not in CLOCK_POLICIES:
            raise ValueError(f"decision_clock must be one of {sorted(CLOCK_POLICIES)}")
        self.journal_path = assert_isolated_path(self.journal_path)
        self._clock_note = CLOCK_NOTES[self.decision_clock]

    def _prepare(self) -> None:
        if self.journal_path.exists():
            self.journal_path.unlink()
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_path = resolve_replay_journal(
            self.journal_path, live_journal=LIVE_JOURNAL
        )
        settings = self.settings
        assert settings is not None
        raw = self.strategy_object or make_research_strategy(self.strategy_name)
        raw = _apply_cost_settings(raw, settings)
        self._strategy = raw
        if hasattr(raw, "name"):
            self.strategy_name = raw.name
        self.broker = PaperBroker(
            PaperBrokerSettings(
                initial_cash=settings.initial_equity,
                fee_rate_per_side=settings.fee_rate_per_side,
                slippage_rate_per_side=settings.effective_slippage_rate_per_side,
                risk_fraction=settings.risk_fraction,
                daily_loss_limit_fraction=settings.daily_loss_limit_fraction,
                max_drawdown_fraction=settings.max_drawdown_fraction,
                max_market_data_age_seconds=self.stale_after_seconds,
                journal_path=self.journal_path,
                allow_simulated_shorts=False,
                exit_fill_mode=settings.exit_fill_mode,
            )
        )
        self._events = []
        self._trades = []
        self._last_decision = None

    def run(self) -> dict[str, object]:
        self._prepare()
        broker = self.broker
        assert broker is not None
        settings = self.settings
        assert settings is not None
        last_quote: QuoteRecord | None = None
        last_quote_time: datetime | None = None
        last_mark: datetime | None = None
        bars = []
        pending_started: datetime | None = None
        pending_intervals: list[float] = []
        stale_protective = 0
        stale_candle_decisions = 0
        stop_target_conflicts = 0
        decision_counts: Counter[str] = Counter()
        rejection_reasons: Counter[str] = Counter()
        entries_accepted = 0
        entries_rejected = 0
        quotes_processed = 0
        candles_processed = 0
        timing_flags: list[dict[str, object]] = []

        gaps = load_reconnect_gaps(self.dataset.session_dir)
        gap_exposure: list[dict[str, object]] = []
        open_before_gap = [False] * len(gaps)
        gap_seen = [False] * len(gaps)

        timeline: list[tuple[datetime, int, int, str, object]] = []
        seen_update_ids: set[int] = set()
        for quote in self.dataset.quotes:
            if quote.update_id is not None and quote.update_id in seen_update_ids:
                continue
            if quote.update_id is not None:
                seen_update_ids.add(quote.update_id)
            timeline.append(
                (quote_order_time(quote), 0, quote.sequence, "quote", quote)
            )
        for candle in self.dataset.candles:
            timeline.append(
                (candle_order_time(candle), 1, candle.sequence, "candle", candle)
            )
        timeline.sort()

        for event_time, _kind, _seq, name, payload in timeline:
            previously_pending = broker.exit_pending is not None
            for index, gap in enumerate(gaps):
                start = gap.get("start")
                end = gap.get("end")
                if not isinstance(start, datetime) or not isinstance(end, datetime):
                    continue
                if event_time < start:
                    open_before_gap[index] = broker.position is not None
                elif not gap_seen[index] and event_time >= end:
                    gap_seen[index] = True
                    if open_before_gap[index] or broker.position is not None:
                        gap_exposure.append(
                            {
                                **{
                                    key: gap[key]
                                    for key in (
                                        "disconnect_utc",
                                        "reconnect_utc",
                                        "reason",
                                        "gap_seconds",
                                    )
                                },
                                "position_open_entering_gap": open_before_gap[index],
                                "position_open_after_gap": broker.position is not None,
                                "quotes_fabricated_in_gap": False,
                            }
                        )
            if name == "quote":
                quote = payload
                assert isinstance(quote, QuoteRecord)
                quotes_processed += 1
                health = recorded_quote_health(
                    quote,
                    replay_now=event_time,
                    quote_replay_time=event_time,
                    stale_after_seconds=self.stale_after_seconds,
                )
                last_quote = quote
                last_quote_time = event_time
                protective = broker.process_protective_quote(
                    health, timestamp=event_time
                )
                if protective is None and broker.position is not None:
                    if health.get("healthy") is not True:
                        stale_protective += 1
                if protective is not None:
                    self._note_broker_result(
                        event_time, protective, source="protective_quote", quote=quote
                    )
                    if protective.accepted is False:
                        rejection_reasons[protective.reason] += 1
                should_mark = last_mark is None or (
                    event_time - last_mark
                ).total_seconds() >= MARK_INTERVAL_SECONDS
                if should_mark and health.get("healthy") is True:
                    broker.mark_to_market(health, timestamp=event_time)
                    last_mark = event_time
            else:
                candle = payload
                candles_processed += 1
                bars.append(candle.as_bar())
                decision_time = research_decision_time(candle, self.decision_clock)
                if candle.exchange_event_timestamp is not None:
                    if candle.exchange_event_timestamp < candle.received_at:
                        timing_flags.append(
                            {
                                "open_time": _iso(candle.open_time),
                                "issue": "kline_E_before_host_receive",
                                "kline_e": _iso(candle.exchange_event_timestamp),
                                "host_receive": _iso(candle.received_at),
                            }
                        )
                if last_quote is None or last_quote_time is None:
                    self._events.append(
                        {
                            "event": "candle_without_quote",
                            "timestamp": _iso(event_time),
                            "decision_time": _iso(decision_time),
                            "open_time": _iso(candle.open_time),
                        }
                    )
                    continue
                if last_quote_time > decision_time:
                    timing_flags.append(
                        {
                            "open_time": _iso(candle.open_time),
                            "issue": "quote_host_time_after_decision_clock",
                            "decision_time": _iso(decision_time),
                            "last_quote_received": _iso(last_quote_time),
                        }
                    )
                health = recorded_quote_health(
                    last_quote,
                    replay_now=decision_time,
                    quote_replay_time=last_quote_time,
                    stale_after_seconds=self.stale_after_seconds,
                )
                if health.get("healthy") is not True:
                    stale_candle_decisions += 1
                stop = broker.position.stop_price if broker.position else None
                target = broker.position.target_price if broker.position else None
                if (
                    broker.position is not None
                    and stop is not None
                    and target is not None
                    and candle.low <= stop
                    and candle.high >= target
                ):
                    stop_target_conflicts += 1
                decision = self._strategy.evaluate(
                    bars,
                    decision_time=decision_time,
                    market_health=health,
                    equity=settings_equity(broker),
                    position=_position_for_strategy(broker),
                )
                self._last_decision = decision
                decision_counts[decision.action] += 1
                if decision.action in ("entry", "exit"):
                    result = broker.process(decision, market_health=health)
                    self._note_broker_result(
                        decision_time, result, source="candle_decision", decision=decision
                    )
                    if decision.action == "entry":
                        if result.accepted:
                            entries_accepted += 1
                        else:
                            entries_rejected += 1
                            rejection_reasons[result.reason] += 1
                elif decision.action == "no_trade":
                    rejection_reasons[decision.reason] += 1
            currently_pending = broker.exit_pending is not None
            if currently_pending and not previously_pending:
                pending_started = event_time
            if previously_pending and not currently_pending and pending_started is not None:
                pending_intervals.append(
                    (event_time - pending_started).total_seconds()
                )
                pending_started = None

        if pending_started is not None:
            last_time = timeline[-1][0] if timeline else None
            if last_time is not None:
                pending_intervals.append((last_time - pending_started).total_seconds())

        summary = self._summary(
            stop_target_conflicts=stop_target_conflicts,
            stale_protective=stale_protective,
            stale_candle_decisions=stale_candle_decisions,
            pending_intervals=pending_intervals,
            reconnect_gaps=gaps,
            gaps_while_position_open=gap_exposure,
            decision_counts=dict(decision_counts),
            rejection_reasons=dict(rejection_reasons),
            entries_accepted=entries_accepted,
            entries_rejected=entries_rejected,
            quotes_processed=quotes_processed,
            candles_processed=candles_processed,
            clock_assumptions=list(CLOCK_ASSUMPTIONS)
            + [self._clock_note, "Lab engine does not invent bookTicker E."],
            kline_host_offset_seconds=self.offset_seconds,
            kline_offset_applied_to_quotes=False,
        )
        summary["decision_clock"] = self.decision_clock
        summary["path"] = "research_recorded_bookTicker"
        summary["timing_flags"] = timing_flags
        summary["timing_flag_counts"] = _count_issues(timing_flags)
        spec = getattr(self._strategy, "spec", None)
        if spec is not None:
            summary["strategy_spec"] = {
                "name": spec.name,
                "warmup_bars": spec.warmup_bars,
                "max_holding_bars": spec.max_holding_bars,
                "is_demonstration": spec.is_demonstration,
                "hypothesis_family": spec.hypothesis_family,
            }
        return summary


def _apply_cost_settings(strategy: object, settings: BacktestSettings) -> object:
    current = getattr(strategy, "settings", None)
    if current is None:
        return strategy
    changes = {}
    for field_name, value in (
        ("fee_rate_per_side", settings.fee_rate_per_side),
        ("slippage_rate_per_side", settings.effective_slippage_rate_per_side),
        ("risk_fraction", settings.risk_fraction),
    ):
        if hasattr(current, field_name):
            changes[field_name] = value
    if hasattr(current, "max_holding_bars"):
        changes["max_holding_bars"] = current.max_holding_bars * (
            settings.candle_interval_seconds // 60
        )
    if not changes:
        return strategy
    if all(getattr(current, name) == value for name, value in changes.items()):
        return strategy
    updated = replace(current, **changes)
    if isinstance(strategy, ProductionStrategyAdapter):
        inner = type(strategy._inner)(updated)
        return ProductionStrategyAdapter(strategy.name, inner=inner)
    return type(strategy)(updated)


def _count_issues(flags: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in flags:
        key = str(row.get("issue"))
        counts[key] = counts.get(key, 0) + 1
    return counts


def run_quote_experiment(
    dataset: QuoteTapeDataset,
    *,
    journal_path: Path,
    strategy_name: str,
    decision_clock: str = HOST_RECEIVE,
    settings: BacktestSettings | None = None,
    strategy_object: object | None = None,
    stale_after_seconds: float = 3.0,
) -> dict[str, object]:
    replay = ResearchQuoteReplay(
        dataset=dataset,
        journal_path=journal_path,
        strategy_name=strategy_name,
        settings=settings or BacktestSettings(candle_interval_seconds=60),
        stale_after_seconds=stale_after_seconds,
        strategy_object=strategy_object,
        decision_clock=decision_clock,
    )
    return replay.run()
