"""Isolated offline quote-tape replay; does not change production trading code."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtester import BacktestSettings, Backtester, _position_for_strategy
from historical_replay import make_strategy, resolve_replay_journal
from paper_broker import PaperBroker, PaperBrokerSettings
from quote_tape_dataset import CandleRecord, QuoteRecord, QuoteTapeDataset, load_quote_tape_session
from strategy import Bar, StrategyDecision

LIVE_JOURNAL = Path("paper_broker.jsonl")
QUOTE_STALE_SECONDS = 3.0
MARK_INTERVAL_SECONDS = 5.0

CLOCK_ASSUMPTIONS = (
    "Event order uses host received_at_utc only; bookTicker E is not invented.",
    "Candle strategy decision_time uses recorded kline E when present so bar close T is not treated as look-ahead.",
    "Median kline (E - received_at) is a host/kline clock observation, not a verified bookTicker exchange-to-host offset, and is not applied to quote timestamps.",
    "Quote freshness at a candle decision is (decision_time - last quote received_at), which mixes kline E with host receive time.",
    "Reconnect gaps contain no quotes; missing windows are not interpolated.",
    "Ordinary bookTicker update_id skips are not treated as dropped stored records.",
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def replay_time(received_at: datetime, offset_seconds: float | None) -> datetime:
    """Deprecated helper kept for older tests; Stage 2.2 does not shift quotes."""
    if offset_seconds is None:
        return received_at
    return received_at + timedelta(seconds=offset_seconds)


def quote_order_time(quote: QuoteRecord) -> datetime:
    return quote.received_at


def candle_order_time(candle: CandleRecord) -> datetime:
    return candle.received_at


def candle_decision_time(candle: CandleRecord) -> datetime:
    if candle.exchange_event_timestamp is not None:
        return candle.exchange_event_timestamp
    return candle.received_at


def recorded_quote_health(
    quote: QuoteRecord,
    *,
    replay_now: datetime,
    quote_replay_time: datetime,
    stale_after_seconds: float,
) -> dict[str, object]:
    """Build PaperBroker market_health from a recorded bookTicker, not OHLC."""
    age = (replay_now - quote_replay_time).total_seconds()
    fresh = 0 <= age <= stale_after_seconds
    return {
        "healthy": fresh,
        "entry_allowed": fresh,
        "connection_status": "connected",
        "bid": quote.bid,
        "ask": quote.ask,
        "spread": quote.spread,
        "message_timestamp": quote_replay_time.isoformat(),
        "last_valid_update": quote_replay_time.isoformat(),
        "age_seconds": age,
        "reason": (
            "Recorded public bookTicker quote."
            if fresh else
            "Recorded quote is stale versus the replay clock."
        ),
        "quote_source": "recorded_bookTicker",
        "update_id": quote.update_id,
        "bid_quantity": quote.bid_quantity,
        "ask_quantity": quote.ask_quantity,
        "exchange_event_timestamp": _iso(quote.exchange_event_timestamp),
        "host_received_at_utc": quote.received_at.isoformat(),
    }


def load_reconnect_gaps(session_dir: Path) -> list[dict[str, object]]:
    path = Path(session_dir) / "events.jsonl"
    gaps: list[dict[str, object]] = []
    if not path.exists():
        return gaps
    pending = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("event") == "disconnect" and event.get("stream") == "quote":
            pending = event
        elif event.get("event") == "reconnect" and event.get("stream") == "quote":
            start = _parse_dt(event.get("gap_started_utc")) or _parse_dt(
                pending.get("timestamp_utc") if pending else None
            )
            end = _parse_dt(event.get("recovered_at_utc")) or _parse_dt(
                event.get("timestamp_utc")
            )
            gaps.append(
                {
                    "disconnect_utc": pending.get("timestamp_utc") if pending else None,
                    "reconnect_utc": event.get("timestamp_utc"),
                    "reason": pending.get("reason") if pending else None,
                    "gap_seconds": event.get("gap_seconds"),
                    "start": start,
                    "end": end,
                }
            )
            pending = None
    return gaps


@dataclass
class QuoteTapeReplay:
    dataset: QuoteTapeDataset
    journal_path: Path
    strategy_name: str = "momentum_breakout"
    settings: BacktestSettings | None = None
    stale_after_seconds: float = QUOTE_STALE_SECONDS

    def __post_init__(self) -> None:
        self.settings = self.settings or BacktestSettings(
            candle_interval_seconds=60
        )
        self.journal_path = resolve_replay_journal(
            self.journal_path, live_journal=LIVE_JOURNAL
        )
        self.offset_seconds = self.dataset.host_clock_offset_seconds
        self._events: list[dict[str, object]] = []
        self._trades: list[dict[str, object]] = []
        self.broker: PaperBroker | None = None
        self._last_decision: StrategyDecision | None = None

    def _prepare(self) -> None:
        if self.journal_path.exists():
            self.journal_path.unlink()
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        settings = self.settings
        assert settings is not None
        strategy = make_strategy(self.strategy_name)
        strategy_settings = replace(
            strategy.settings,
            fee_rate_per_side=settings.fee_rate_per_side,
            slippage_rate_per_side=settings.effective_slippage_rate_per_side,
            risk_fraction=settings.risk_fraction,
            max_holding_bars=(
                strategy.settings.max_holding_bars
                * (settings.candle_interval_seconds // 60)
            ),
        )
        self._strategy = type(strategy)(strategy_settings)
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
        last_quote: QuoteRecord | None = None
        last_quote_time: datetime | None = None
        last_mark: datetime | None = None
        bars: list[Bar] = []
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
                decision_time = candle_decision_time(candle)
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

        return self._summary(
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
            clock_assumptions=list(CLOCK_ASSUMPTIONS),
            kline_host_offset_seconds=self.offset_seconds,
            kline_offset_applied_to_quotes=False,
        )

    def _note_broker_result(self, timestamp, result, **extra) -> None:
        record = {
            "timestamp": _iso(timestamp),
            "accepted": result.accepted,
            "action": result.action,
            "reason": result.reason,
            "fill_price": result.fill_price,
            "quantity_btc": result.quantity_btc,
            "source": extra.get("source"),
        }
        if extra.get("quote") is not None:
            quote = extra["quote"]
            record["update_id"] = quote.update_id
            record["recorded_bid"] = quote.bid
            record["recorded_ask"] = quote.ask
            record["recorded_spread"] = quote.spread
        if extra.get("decision") is not None:
            record["strategy_action"] = extra["decision"].action
            record["strategy_reason"] = extra["decision"].reason
            record["strategy_exit_price"] = extra["decision"].inputs.get("exit_price")
        self._events.append(record)
        if result.accepted and result.action in ("entry", "exit"):
            self._trades.append(record)

    def _summary(self, **metrics) -> dict[str, object]:
        broker = self.broker
        assert broker is not None
        trades = self._closed_trades()
        return {
            "path": "recorded_bookTicker_protective_quote",
            "strategy": self.strategy_name,
            "journal_path": str(self.journal_path),
            "host_clock_offset_seconds": self.offset_seconds,
            "clock_offset_method": self.dataset.clock_offset_method,
            "quote_event_timestamps": "unavailable_on_bookTicker",
            "stale_after_seconds": self.stale_after_seconds,
            "cash": broker.cash,
            "equity": broker.equity if broker.position is None or broker.last_bid is not None else broker.cash,
            "open_position": broker.position is not None,
            "unresolved_positions": int(broker.position is not None),
            "exit_pending": broker.exit_pending,
            "force_liquidated_at_end": False,
            "trades": trades,
            "trade_count": len(trades),
            "events": list(self._events),
            **metrics,
        }

    def _closed_trades(self) -> list[dict[str, object]]:
        if self.broker is None or not self.journal_path.exists():
            return []
        closed = []
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") == "trade_closed":
                closed.append(event["details"])
        return closed


def settings_equity(broker: PaperBroker) -> float:
    try:
        return broker.equity
    except RuntimeError:
        return broker.cash


def journal_trade_fingerprint(path: Path) -> list[dict[str, object]]:
    """Comparable journal slice; drops wall-clock account_initialized time."""
    records = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") == "account_initialized":
            details = dict(event.get("details") or {})
            records.append(
                {
                    "event": "account_initialized",
                    "details": {
                        "starting_cash": details.get("starting_cash"),
                        "currency_basis": details.get("currency_basis"),
                    },
                }
            )
            continue
        records.append(event)
    return records


def run_candle_only_comparison(
    dataset: QuoteTapeDataset,
    *,
    strategy_name: str,
    settings: BacktestSettings,
    output_dir: Path,
) -> dict[str, object]:
    """Backtester path using synthetic close quotes, labeled as such."""
    bars = tuple(candle.as_bar() for candle in dataset.candles)
    strategy = make_strategy(strategy_name)
    results = Backtester(settings).run(bars, strategy, output_dir=output_dir)
    results["execution_prices"] = (
        "synthetic: candle close ± BacktestSettings.spread_fraction; "
        "not recorded bookTicker bid/ask"
    )
    results["path"] = "synthetic_candle_close_quote"
    results["force_liquidated_at_end"] = bool(
        (results.get("overall") or {}).get("period_boundary_force_liquidation")
    )
    return results


def write_trade_comparison_csv(
    path: Path, paired: list[dict[str, object]]
) -> None:
    fields = [
        "index",
        "quote_exit_timestamp",
        "candle_exit_timestamp",
        "quote_trigger",
        "candle_reason",
        "quote_exit_fill",
        "candle_synthetic_exit_fill",
        "fill_difference",
        "quote_net_pnl",
        "candle_net_pnl",
        "net_pnl_difference",
        "quote_gross_pnl",
        "candle_gross_pnl",
        "quote_fees",
        "candle_fees",
        "quote_entry_fill",
        "candle_entry_fill",
        "time_difference_seconds",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in paired:
            writer.writerow(row)


def compare_paths(
    dataset: QuoteTapeDataset,
    *,
    strategy_name: str = "momentum_breakout",
    output_dir: str | Path,
) -> dict[str, object]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    settings = BacktestSettings(candle_interval_seconds=60)
    quote_replay = QuoteTapeReplay(
        dataset,
        journal_path=output / "quote_replay_journal.jsonl",
        strategy_name=strategy_name,
        settings=settings,
    )
    quote_result = quote_replay.run()
    candle_dir = output / "candle_backtest"
    candle_result = run_candle_only_comparison(
        dataset,
        strategy_name=strategy_name,
        settings=settings,
        output_dir=candle_dir,
    )
    quote_trades = quote_result["trades"]
    paired = []
    fairness = [
        "Candle-only fills are synthetic close ± spread_fraction, not tape bids.",
        "Quote path uses recorded bookTicker and PaperBroker.process_protective_quote.",
        *CLOCK_ASSUMPTIONS,
        "Unresolved open positions are not force-closed on the quote path; the backtester may force-liquidate at period end.",
        "Reconnect gaps are preserved as missing quotes; fills are not fabricated in those windows.",
    ]
    candle_rows = []
    trades_csv = candle_dir / "trades.csv"
    if trades_csv.exists():
        with trades_csv.open("r", encoding="utf-8", newline="") as source:
            candle_rows = list(csv.DictReader(source))
    for index in range(min(len(quote_trades), len(candle_rows))):
        qt = quote_trades[index]
        ct = candle_rows[index]
        quote_fill = qt.get("exit_fill_price")
        candle_fill = float(ct["exit_fill_price"])
        quote_pnl = qt.get("net_realized_pnl")
        candle_pnl = float(ct["net_realized_pnl"])
        quote_exit = qt.get("exit_timestamp")
        candle_exit = ct.get("exit_timestamp")
        time_difference = None
        if isinstance(quote_exit, str) and isinstance(candle_exit, str):
            left = _parse_dt(quote_exit)
            right = _parse_dt(candle_exit)
            if left is not None and right is not None:
                time_difference = (left - right).total_seconds()
        paired.append(
            {
                "index": index,
                "quote_exit_timestamp": quote_exit,
                "candle_exit_timestamp": candle_exit,
                "time_difference_seconds": time_difference,
                "quote_trigger": qt.get("trigger"),
                "candle_reason": ct.get("reason"),
                "quote_exit_fill": quote_fill,
                "candle_synthetic_exit_fill": candle_fill,
                "fill_difference": (
                    None if quote_fill is None else quote_fill - candle_fill
                ),
                "quote_entry_fill": qt.get("entry_fill_price"),
                "candle_entry_fill": float(ct["entry_fill_price"]),
                "quote_gross_pnl": qt.get("gross_pnl"),
                "candle_gross_pnl": float(ct["gross_pnl"]),
                "quote_net_pnl": quote_pnl,
                "candle_net_pnl": candle_pnl,
                "net_pnl_difference": (
                    None if quote_pnl is None else quote_pnl - candle_pnl
                ),
                "quote_fees": (qt.get("entry_fee") or 0) + (qt.get("exit_fee") or 0),
                "candle_fees": float(ct["entry_fee"]) + float(ct["exit_fee"]),
            }
        )
    if len(quote_trades) != len(candle_rows):
        fairness.append(
            f"Trade counts differ (quote {len(quote_trades)} vs synthetic candle "
            f"{len(candle_rows)}); unpaired trades are not a fair fill comparison."
        )
    csv_path = output / "trade_level_comparison.csv"
    write_trade_comparison_csv(csv_path, paired)
    comparison = {
        "session_id": dataset.session_id,
        "strategy": strategy_name,
        "settings": {
            "initial_equity": settings.initial_equity,
            "fee_rate_per_side": settings.fee_rate_per_side,
            "slippage_rate_per_side": settings.effective_slippage_rate_per_side,
            "spread_fraction_candle_only": settings.spread_fraction,
            "risk_fraction": settings.risk_fraction,
        },
        "quote_path": {
            **{key: value for key, value in quote_result.items() if key != "events"},
            "event_count": len(quote_result["events"]),
            "accepted_events": [
                event for event in quote_result["events"] if event.get("accepted")
            ],
        },
        "candle_path": {
            "path": candle_result["path"],
            "execution_prices": candle_result["execution_prices"],
            "overall": candle_result.get("overall"),
            "assumptions": candle_result.get("assumptions"),
            "force_liquidated_at_end": candle_result.get("force_liquidated_at_end"),
        },
        "paired_trades": paired,
        "trade_level_comparison_csv": str(csv_path),
        "fairness_limits": fairness,
        "unresolved_quote_positions": quote_result["unresolved_positions"],
        "stale_protective_observations": quote_result.get("stale_protective"),
        "stale_candle_decisions": quote_result.get("stale_candle_decisions"),
        "pending_exit_durations_seconds": quote_result.get("pending_intervals"),
        "stop_target_conflicts_on_candle": quote_result.get("stop_target_conflicts"),
        "gaps_while_position_open": quote_result.get("gaps_while_position_open"),
    }
    (output / "comparison.json").write_text(
        json.dumps(comparison, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return comparison


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--strategy", default="momentum_breakout")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    dataset = load_quote_tape_session(args.session)
    compare_paths(
        dataset, strategy_name=args.strategy, output_dir=args.output
    )


if __name__ == "__main__":
    main()
