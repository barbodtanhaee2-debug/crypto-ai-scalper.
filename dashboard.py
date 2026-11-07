"""Read-only localhost dashboard for paper-broker and saved backtest files."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from backtester import BacktestSettings, load_candles_csv
from dashboard_data import (
    load_backtest_period,
    load_broker_portfolio,
    load_runtime_status,
    runtime_health_label,
    runtime_status_label,
)
from historical_replay import (
    PLAYBACK_SPEEDS,
    REPLAY_STRATEGIES,
    HistoricalReplay,
)

LIVE_REFRESH_INTERVALS = (1, 2, 3, 5, 10, 30)
DEFAULT_LIVE_REFRESH_SECONDS = 1


def _display(value: object, *, precision: int = 4, suffix: str = "") -> str:
    if value is None:
        return "Unavailable"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (float, int)):
        return f"{value:,.{precision}f}{suffix}"
    return str(value)


def _fraction(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.2%}"
    return "Unavailable"


def _render_records(records: list[dict], empty_message: str) -> None:
    if records:
        st.dataframe(records, use_container_width=True, hide_index=True)
    else:
        st.info(empty_message)


def _render_saved_period(title: str, directory: Path) -> None:
    st.subheader(title)
    period = load_backtest_period(directory)
    for error in period["errors"]:
        st.caption(error)
    results = period["results"]
    if not isinstance(results, dict):
        st.info("No valid saved result report for this period.")
        return

    st.caption(
        "Binance Spot long-only historical simulation; not live performance. "
        "Prices, equity, P&L, fees, and modeled costs are in USDT."
    )
    overall = results.get("overall")
    overall = overall if isinstance(overall, dict) else {}
    columns = st.columns(4)
    columns[0].metric("Initial equity (USDT)", _display(overall.get("initial_equity")))
    columns[1].metric("Final equity (USDT)", _display(overall.get("final_equity")))
    columns[2].metric("Net return", _fraction(overall.get("net_return_fraction")))
    columns[3].metric("Trades", _display(overall.get("trade_count"), precision=0))

    more = st.columns(4)
    more[0].metric(
        "Maximum drawdown",
        _fraction(overall.get("maximum_drawdown_fraction")),
    )
    more[1].metric("Win rate", _fraction(overall.get("win_rate")))
    profit_factor = overall.get("profit_factor")
    more[2].metric("Profit factor", _display(profit_factor))
    more[3].metric("Total fees (USDT)", _display(overall.get("total_fees")))
    st.caption(
        f"Total modeled costs: {_display(overall.get('total_costs'))} USDT "
        "(fees, spread, and slippage where reported)."
    )
    if overall.get("profit_factor_note"):
        st.caption(str(overall["profit_factor_note"]))

    sample = results.get("sample_period")
    sample = sample if isinstance(sample, dict) else {}
    st.write(
        f"Sample period: {_display(sample.get('start', results.get('data_start')))} "
        f"to {_display(sample.get('end', results.get('data_end')))}; "
        f"{_display(results.get('candles_processed'), precision=0)} candles."
    )
    with st.expander("Saved assumptions and parameters"):
        st.json({
            "assumptions": results.get("assumptions", "Unavailable"),
            "strategy_parameters": results.get("strategy_parameters", "Unavailable"),
        })
    _render_records(period["trades"], "No saved trades in this period.")


def _render_live_dashboard(
    journal_path: Path,
    runtime_status_path: Path,
    refresh_seconds: int,
) -> None:
    journal, portfolio = load_broker_portfolio(journal_path)
    runtime_status = load_runtime_status(runtime_status_path)
    runtime = runtime_status.data or {}
    state_label = runtime_status_label(runtime_status)
    if state_label == "Running (fresh heartbeat)":
        health_label = runtime_health_label(runtime_status)
        if health_label == "Healthy (quote and candle feeds ready)":
            st.success(f"Paper runtime: {health_label}.")
        elif health_label == "Warming up candles":
            st.info(f"Paper runtime: {health_label}.")
        else:
            st.warning(f"Paper runtime: {health_label}.")
        st.caption(f"Runtime status and portfolio files refresh every {refresh_seconds}s.")
    elif state_label == "Stale or invalid heartbeat":
        st.warning(
            "Paper runtime: Stale or invalid heartbeat. Runtime values are masked "
            "and must not be treated as live."
        )
    elif state_label == "Stopped":
        st.info("Paper runtime: Stopped. Runtime values are not live.")
    else:
        st.info("Paper runtime: Status unavailable.")
    for error in runtime_status.errors:
        st.caption(error)

    pending_exit = runtime.get("exit_pending")
    if isinstance(pending_exit, dict):
        st.warning(
            "A protective exit is pending fresh-quote verification. "
            "New entries remain blocked. Simulated exposure stays open; "
            "a fill is not fabricated from a stale quote, so losses can "
            "remain unresolved and can exceed the planned stop during an outage."
        )
        st.json(pending_exit)

    if journal.exists and journal.errors:
        st.warning(
            "The broker journal has incomplete or invalid records. Displayed "
            "portfolio values reflect only the latest complete valid record "
            "and may not represent the current account or pending exit."
        )

    st.header("Portfolio")
    if not journal.exists:
        st.info("Paper broker journal is unavailable; no portfolio state to display.")
    elif not journal.events:
        st.info("No valid broker state records are available.")
    else:
        columns = st.columns(5)
        columns[0].metric("Cash (USDT)", _display(portfolio["cash"]))
        columns[1].metric("Equity (USDT)", _display(portfolio["equity"]))
        columns[2].metric("Realized P&L (USDT)", _display(portfolio["realized_pnl"]))
        columns[3].metric("Unrealized P&L (USDT)", _display(portfolio["unrealized_pnl"]))
        columns[4].metric("Recorded fees (USDT)", _display(portfolio["total_fees"]))
        st.caption(
            f"Latest journal event: {_display(portfolio['latest_event_timestamp'])}. "
            "This is saved paper state and may be older than the runtime status."
        )
        position = portfolio["open_position"]
        if position is None:
            st.info("No open simulated position is recorded.")
        else:
            st.subheader("Recorded open simulated position")
            st.json(position)

    st.header("Market health")
    market = portfolio["market_record"]
    market_cols = st.columns(4)
    market_cols[0].metric(
        "Feed connection",
        _display(runtime.get("connection_status")),
    )
    market_cols[1].metric(
        "Feed healthy",
        _display(runtime.get("market_data_healthy")),
    )
    market_cols[2].metric(
        "Entry permission",
        _display(runtime.get("entry_permission")),
    )
    age = runtime.get("market_data_age_seconds")
    limit = runtime.get("market_data_stale_limit_seconds")
    freshness = (
        "Fresh" if isinstance(age, (int, float))
        and isinstance(limit, (int, float)) and 0 <= age <= limit
        else "Stale" if isinstance(age, (int, float))
        else "Unavailable"
    )
    market_cols[3].metric("Data freshness", freshness)
    st.write(
        "Last valid update (recorded): "
        f"{_display(runtime.get('last_recorded_market_data_timestamp'))}; "
        f"age: {_display(age, precision=1, suffix='s')}; "
        f"spread: {_display(runtime.get('spread'))}."
    )
    st.caption(
        "WebSocket messages — "
        f"received: {_display(runtime.get('websocket_messages_received'), precision=0)}, "
        f"accepted: {_display(runtime.get('websocket_messages_accepted'), precision=0)}, "
        f"rejected: {_display(runtime.get('websocket_messages_rejected'), precision=0)}; "
        f"last received: {_display(runtime.get('last_websocket_message_timestamp'))}; "
        f"last data error: {_display(runtime.get('last_market_data_error'))}."
    )
    st.caption(
        f"Last journaled mark: {_display(market['last_mark_timestamp'])}. "
        "A recorded timestamp is not proof of a currently running feed."
    )
    st.caption(
        "Entry permission / latest decision detail: "
        f"{_display(runtime.get('entry_permission_reason'))}."
    )
    diagnostics = runtime.get("entry_diagnostics")
    st.subheader("Entry and strategy readiness")
    if isinstance(diagnostics, dict):
        st.write(
            "Completed candles: "
            f"interval {_display(runtime.get('candle_interval'), suffix='')}; "
            f"{_display(diagnostics.get('completed_candle_count'), precision=0)} / "
            f"{_display(diagnostics.get('required_completed_candle_count'), precision=0)} "
            "required for strategy warm-up; "
            f"{_display(diagnostics.get('warmup_candles_remaining'), precision=0)} "
            "remaining. Current unfinished candle samples: "
            f"{_display(diagnostics.get('current_candle_samples'), precision=0)} / "
            f"{_display(diagnostics.get('required_candle_samples'), precision=0)}."
        )
        st.write(
            "Open position: "
            f"{_display(diagnostics.get('open_position'))}; risk lockout: "
            f"{_display(diagnostics.get('risk_lockout'))}; "
            f"risk status: daily loss "
            f"{_fraction(diagnostics.get('daily_loss_fraction'))}, drawdown "
            f"{_fraction(diagnostics.get('drawdown_fraction'))}."
        )
        gate_age = _display(
            diagnostics.get("entry_gate_market_data_age_seconds"),
            precision=1,
            suffix="s",
        )
        feed_age = _display(
            diagnostics.get("feed_reported_market_data_age_seconds"),
            precision=1,
            suffix="s",
        )
        st.caption(
            f"Age used by entry gate: {gate_age}; feed-reported age: {feed_age}."
        )
        blockers = diagnostics.get("readiness_blockers")
        if isinstance(blockers, list) and blockers:
            st.warning("Current entry/readiness blockers: " + " ".join(map(str, blockers)))
        elif isinstance(blockers, list):
            st.success("No entry-gate blockers are currently recorded.")
        else:
            st.info("Detailed entry-readiness diagnostics are unavailable.")

        decisions = diagnostics.get("strategy_decisions")
        if isinstance(decisions, dict):
            st.caption(
                "Last strategy evaluation: "
                f"{_display(diagnostics.get('strategy_evaluation_timestamp'))}."
            )
            st.json(decisions)
        else:
            st.info("No strategy evaluation has been recorded yet.")
    else:
        st.info("Detailed entry-readiness diagnostics are unavailable.")

    st.header("Trade history and rejections")
    st.caption("Recorded prices, P&L, fees, and costs are denominated in USDT.")
    _render_records(
        portfolio["trade_history"],
        "No closed trades are recorded in this broker journal.",
    )
    with st.expander("Risk and strategy rejections"):
        _render_records(
            portfolio["rejections"],
            "No rejection records are available.",
        )

    st.header("Binance Spot long-only paper performance")
    st.caption("Journal-derived simulation history, not live exchange performance.")
    metrics = portfolio["metrics"]
    perf_columns = st.columns(5)
    perf_columns[0].metric("Closed trades", _display(metrics["trade_count"], precision=0))
    perf_columns[1].metric("Net return", _fraction(metrics["net_return_fraction"]))
    perf_columns[2].metric("Maximum drawdown", _fraction(metrics["maximum_drawdown_fraction"]))
    perf_columns[3].metric("Win rate", _fraction(metrics["win_rate"]))
    perf_columns[4].metric("Profit factor", _display(metrics["profit_factor"]))
    if metrics.get("profit_factor_note"):
        st.caption(str(metrics["profit_factor_note"]))
    if portfolio["equity_curve"]:
        st.line_chart(
            {
                "timestamp": [point["timestamp"] for point in portfolio["equity_curve"]],
                "equity (USDT)": [
                    point["equity"] for point in portfolio["equity_curve"]
                ],
            },
            x="timestamp",
            y="equity (USDT)",
        )
    else:
        st.info("No recorded equity marks are available.")

    st.header("Risk status")
    risk = portfolio["risk"]
    risk_columns = st.columns(6)
    risk_columns[0].metric(
        "Daily loss",
        _fraction(runtime.get("daily_loss_fraction", risk["daily_loss_fraction"])),
    )
    risk_columns[1].metric(
        "Drawdown",
        _fraction(runtime.get("drawdown_fraction", risk["drawdown_fraction"])),
    )
    risk_columns[2].metric("Peak equity", _display(risk["peak_equity"]))
    risk_columns[3].metric(
        "Daily-loss limit",
        _fraction(runtime.get("daily_loss_limit_fraction", risk["daily_loss_limit"])),
    )
    risk_columns[4].metric(
        "Max-drawdown limit",
        _fraction(runtime.get("max_drawdown_fraction", risk["max_drawdown_limit"])),
    )
    risk_columns[5].metric("Trading lockout", _display(runtime.get("risk_lockout")))
    st.caption(
        "Risk values from runtime status are current only while its heartbeat is fresh. "
        "Journal-derived values are historical snapshots."
    )

    if journal.errors:
        with st.expander("Broker journal read warnings"):
            for error in journal.errors:
                st.warning(error)


def _render_backtests(backtest_root: Path) -> None:
    st.header("Binance Spot long-only backtests")
    st.caption("Historical candle simulations; results are not live performance.")
    if not backtest_root.is_dir():
        st.info("Backtest results directory is unavailable.")
    else:
        strategy_dirs = sorted(
            directory for directory in backtest_root.iterdir() if directory.is_dir()
        )
        if not strategy_dirs:
            st.info("No strategy result directories are present.")
        for strategy_dir in strategy_dirs:
            st.subheader(strategy_dir.name)
            development, out_of_sample = st.columns(2)
            with development:
                _render_saved_period(
                    "Development period",
                    strategy_dir / "development",
                )
            with out_of_sample:
                _render_saved_period(
                    "Out-of-sample period",
                    strategy_dir / "out_of_sample",
                )


def _replay_chart(snapshot: dict) -> None:
    series = snapshot.get("price_series") or []
    if not series:
        st.info("No completed candles in this replay yet.")
        return
    try:
        import altair as alt
        import pandas as pd
    except ImportError:
        st.line_chart(
            {
                "timestamp": [point["timestamp"] for point in series],
                "close": [point["close"] for point in series],
            },
            x="timestamp",
            y="close",
        )
        return
    price = pd.DataFrame(series)
    price["timestamp"] = pd.to_datetime(price["timestamp"], utc=True)
    chart = alt.Chart(price).mark_line(color="#4c78a8").encode(
        x=alt.X("timestamp:T", title="Completed candle close (UTC)"),
        y=alt.Y("close:Q", title="Close (USDT)"),
    )
    markers = snapshot.get("markers") or []
    if markers:
        marks = pd.DataFrame(markers)
        marks["timestamp"] = pd.to_datetime(marks["timestamp"], utc=True)
        points = (
            alt.Chart(marks)
            .mark_point(size=90, filled=True)
            .encode(
                x="timestamp:T",
                y="fill_price:Q",
                color=alt.Color(
                    "action:N",
                    scale=alt.Scale(
                        domain=["entry", "exit"],
                        range=["#2ca02c", "#d62728"],
                    ),
                ),
                tooltip=["action", "timestamp", "fill_price", "reason"],
            )
        )
        chart = chart + points
    st.altair_chart(chart.properties(height=360), use_container_width=True)


def _render_historical_replay(
    candle_path: Path,
    replay_journal: Path,
    live_journal: Path,
    strategy_name: str,
    interval_seconds: int,
    speed: int,
) -> None:
    st.header("Historical replay")
    st.caption(
        "Isolated paper account and journal. 1x advances one completed candle "
        "per playback second. Future candles are never used."
    )
    if "replay_playing" not in st.session_state:
        st.session_state.replay_playing = False
    if "replay" not in st.session_state:
        st.session_state.replay = None
    if "replay_error" not in st.session_state:
        st.session_state.replay_error = None

    controls = st.columns(5)
    load = controls[0].button("Load / reset source")
    play = controls[1].button("Play")
    pause = controls[2].button("Pause")
    step = controls[3].button("Step")
    restart = controls[4].button("Restart")

    if load:
        st.session_state.replay_playing = False
        try:
            candles = load_candles_csv(
                candle_path, interval_seconds=interval_seconds
            )
            st.session_state.replay = HistoricalReplay(
                candles,
                strategy_name=strategy_name,
                journal_path=replay_journal,
                settings=BacktestSettings(candle_interval_seconds=interval_seconds),
                live_journal=live_journal,
            )
            st.session_state.replay_error = None
        except (OSError, TypeError, ValueError) as error:
            st.session_state.replay = None
            st.session_state.replay_error = str(error)

    replay: HistoricalReplay | None = st.session_state.replay
    if replay is not None:
        if play:
            st.session_state.replay_playing = True
        if pause:
            st.session_state.replay_playing = False
        if restart:
            replay.restart()
            st.session_state.replay_playing = False
        if step:
            replay.step()
            st.session_state.replay_playing = False
        if st.session_state.replay_playing:
            for _ in range(speed):
                if not replay.step():
                    st.session_state.replay_playing = False
                    break

    if st.session_state.replay_error:
        st.error(st.session_state.replay_error)
        return
    if replay is None:
        st.info("Load a validated local candle CSV to start an isolated replay.")
        return

    snapshot = replay.snapshot()
    metrics = st.columns(5)
    metrics[0].metric("Replay clock", _display(snapshot["clock"]))
    metrics[1].metric("Close", _display(snapshot["close"]))
    metrics[2].metric("Equity (USDT)", _display(snapshot["equity"]))
    metrics[3].metric("Cash (USDT)", _display(snapshot["cash"]))
    metrics[4].metric("Closed exits", _display(len(snapshot["trades"]), precision=0))
    st.caption(
        f"Strategy {snapshot['strategy']}; journal {snapshot['journal_path']}; "
        f"playing={st.session_state.replay_playing}; speed={speed}x."
    )
    position = snapshot["open_position"]
    if position is None:
        st.info("Replay account has no open simulated position.")
    else:
        st.json(position)
    decision = snapshot["last_decision"]
    if decision is not None:
        st.caption(
            f"Last decision: {decision['action']} — {decision['reason']}"
        )
    _replay_chart(snapshot)
    _render_records(snapshot["markers"], "No simulated entries or exits yet.")


def main() -> None:
    st.set_page_config(page_title="Paper Trading Monitor", layout="wide")
    st.title("Paper Trading Monitor")
    st.caption(
        "Read-only local file viewer. It does not connect to a running bot, "
        "exchange, or order endpoint."
    )
    st.warning(
        "Paper-only dashboard. Saved journal values are historical snapshots; "
        "runtime status is shown only while its heartbeat is fresh."
    )
    with st.sidebar:
        st.header("Local read-only sources")
        journal_path = Path(
            st.text_input("Broker journal", value="paper_broker.jsonl")
        ).expanduser()
        runtime_status_path = Path(
            st.text_input("Runtime status", value="runtime_status.json")
        ).expanduser()
        backtest_root = Path(
            st.text_input("Backtest results directory", value="backtest-results")
        ).expanduser()
        refresh_seconds = st.selectbox(
            "Live data refresh (seconds)",
            options=LIVE_REFRESH_INTERVALS,
            index=LIVE_REFRESH_INTERVALS.index(DEFAULT_LIVE_REFRESH_SECONDS),
        )
        st.header("Historical replay source")
        replay_csv = Path(
            st.text_input(
                "Replay candle CSV",
                value="",
                help="Local backtester CSV; not the live paper journal.",
            )
        ).expanduser()
        replay_journal = Path(
            st.text_input(
                "Replay journal",
                value="historical-replay/journal.jsonl",
            )
        ).expanduser()
        replay_strategy = st.selectbox(
            "Replay strategy",
            options=list(REPLAY_STRATEGIES),
        )
        replay_interval = st.number_input(
            "Replay candle interval seconds",
            min_value=60,
            step=60,
            value=3600,
        )
        replay_speed = st.selectbox(
            "Replay speed",
            options=PLAYBACK_SPEEDS,
            index=0,
        )

    @st.fragment(run_every=refresh_seconds)
    def render_live_data() -> None:
        _render_live_dashboard(
            journal_path,
            runtime_status_path,
            refresh_seconds,
        )

    @st.fragment(run_every="30s")
    def render_backtest_data() -> None:
        _render_backtests(backtest_root)

    @st.fragment(run_every=1 if st.session_state.get("replay_playing") else None)
    def render_replay_data() -> None:
        _render_historical_replay(
            replay_csv,
            replay_journal,
            journal_path,
            replay_strategy,
            int(replay_interval),
            int(replay_speed),
        )

    render_live_data()
    render_backtest_data()
    render_replay_data()


if __name__ == "__main__":
    main()
