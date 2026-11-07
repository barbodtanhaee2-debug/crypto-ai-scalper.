import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dashboard_data import (
    available_metric,
    load_backtest_period,
    load_broker_journal,
    load_broker_portfolio,
    load_runtime_status,
    runtime_health_label,
    runtime_status_label,
    summarize_journal,
)


class DashboardDataTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.journal_path = self.root / "paper_broker.jsonl"

    def tearDown(self):
        self.tempdir.cleanup()

    def write_journal(self, events):
        self.journal_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )

    def account_event(
        self,
        *,
        event="mark_to_market",
        timestamp="2026-10-21T12:00:00+00:00",
        state=None,
        details=None,
    ):
        return {
            "timestamp": timestamp,
            "event": event,
            "details": details or {},
            "state": state or {
                "initial_cash": 100,
                "cash": 100,
                "realized_pnl": 0,
                "position": None,
                "peak_equity": 100,
                "day_key": "2026-10-21",
                "day_start_equity": 100,
                "last_bid": 99.99,
                "last_ask": 100.01,
            },
        }

    def test_missing_and_empty_journal_are_explicit_and_safe(self):
        missing = load_broker_journal(self.root / "missing.jsonl")
        empty_path = self.root / "empty.jsonl"
        empty_path.write_text("", encoding="utf-8")
        empty = load_broker_journal(empty_path)

        self.assertFalse(missing.exists)
        self.assertTrue(missing.errors)
        self.assertTrue(empty.exists)
        self.assertEqual(empty.events, ())
        self.assertTrue(empty.errors)
        summary = summarize_journal(empty)
        self.assertIsNone(summary["cash"])
        self.assertIsNone(summary["market_record"]["connection_status"])
        self.assertIsNone(summary["market_record"]["entry_allowed"])

    def test_malformed_lines_are_reported_without_crashing(self):
        valid = self.account_event()
        self.journal_path.write_text(
            "{broken json\n"
            + json.dumps({"event": "bad shape"})
            + "\n"
            + json.dumps(valid)
            + "\n",
            encoding="utf-8",
        )
        data = load_broker_journal(self.journal_path)

        self.assertEqual(len(data.events), 1)
        self.assertEqual(len(data.errors), 2)
        self.assertEqual(summarize_journal(data)["equity"], 100)

    def test_incremental_journal_reader_and_portfolio_follow_appends(self):
        first = self.account_event()
        self.write_journal((first,))

        first_journal, first_portfolio = load_broker_portfolio(self.journal_path)
        cached_journal, cached_portfolio = load_broker_portfolio(self.journal_path)
        self.assertIs(cached_journal, first_journal)
        self.assertIs(cached_portfolio, first_portfolio)
        self.assertEqual(first_portfolio["cash"], 100)

        next_event = self.account_event(
            timestamp="2026-10-21T12:00:05+00:00",
            state={
                **first["state"],
                "cash": 101,
                "realized_pnl": 1,
                "last_bid": 100.99,
                "last_ask": 101.01,
            },
        )
        with self.journal_path.open("a", encoding="utf-8") as source:
            source.write(json.dumps(next_event) + "\n")

        updated_journal, updated_portfolio = load_broker_portfolio(self.journal_path)
        self.assertIsNot(updated_journal, first_journal)
        self.assertEqual(len(updated_journal.events), 2)
        self.assertEqual(updated_portfolio["cash"], 101)
        self.assertEqual(updated_portfolio["realized_pnl"], 1)

        self.write_journal((first,))
        replaced_journal, replaced_portfolio = load_broker_portfolio(self.journal_path)
        self.assertEqual(len(replaced_journal.events), 1)
        self.assertEqual(replaced_portfolio["cash"], 100)

    def test_incomplete_final_line_is_omitted_until_completed(self):
        first = self.account_event()
        self.journal_path.write_text(
            json.dumps(first) + "\n"
            + '{"timestamp":"2026-10-21T12:00:05+00:00","event":"mark_to_market",'
            + '"details":{},"state":',
            encoding="utf-8",
        )

        partial = load_broker_journal(self.journal_path)
        self.assertEqual(len(partial.events), 1)
        self.assertTrue(any("incomplete final journal record" in error for error in partial.errors))
        partial_summary = summarize_journal(partial)
        self.assertEqual(partial_summary["cash"], 100)

        with self.journal_path.open("a", encoding="utf-8") as source:
            source.write('{"cash":101,"initial_cash":100,"realized_pnl":1,"position":null}}\n')
        completed = load_broker_journal(self.journal_path)
        self.assertEqual(len(completed.events), 2)
        self.assertFalse(any("incomplete final" in error for error in completed.errors))
        self.assertEqual(summarize_journal(completed)["cash"], 101)

    def test_portfolio_metrics_fees_drawdown_and_missing_fields_are_accurate(self):
        initial_state = self.account_event(event="account_initialized")["state"]
        initial = self.account_event(
            event="account_initialized",
            timestamp="2026-10-21T12:00:00+00:00",
            state=initial_state,
        )
        closed_state = {
            **initial_state,
            "cash": 104,
            "realized_pnl": 4,
            "peak_equity": 110,
            "last_bid": 104,
            "last_ask": 104.02,
        }
        closed_trade = self.account_event(
            event="trade_closed",
            timestamp="2026-10-21T12:02:00+00:00",
            state=closed_state,
            details={
                "strategy": "momentum_breakout",
                "direction": "long",
                "entry_timestamp": "2026-10-21T12:00:00+00:00",
                "exit_timestamp": "2026-10-21T12:02:00+00:00",
                "entry_fill_price": 100,
                "exit_fill_price": 105,
                "quantity_btc": 1,
                "entry_fee": 0.1,
                "exit_fee": 0.1,
                "total_costs": 0.4,
                "gross_pnl": 5,
                "net_realized_pnl": 4.8,
                "reason": "Target reached.",
            },
        )
        loss_state = {
            **closed_state,
            "cash": 102,
            "realized_pnl": 2,
            "last_bid": 102,
            "last_ask": 102.02,
        }
        loss_trade = self.account_event(
            event="trade_closed",
            timestamp="2026-10-21T12:03:00+00:00",
            state=loss_state,
            details={
                "strategy": "range_mean_reversion",
                "direction": "short",
                "entry_timestamp": "2026-10-21T12:02:00+00:00",
                "exit_timestamp": "2026-10-21T12:03:00+00:00",
                "entry_fill_price": 104,
                "exit_fill_price": 102,
                "quantity_btc": 1,
                "entry_fee": 0.2,
                "exit_fee": 0.2,
                "total_costs": 0.6,
                "gross_pnl": -2,
                "net_realized_pnl": -2.4,
                "reason": "Stop reached.",
            },
        )
        self.write_journal((initial, closed_trade, loss_trade))
        summary = summarize_journal(load_broker_journal(self.journal_path))

        self.assertEqual(summary["cash"], 104)
        self.assertEqual(summary["account_currency"], "USDT")
        self.assertEqual(summary["equity"], 104)
        self.assertEqual(summary["realized_pnl"], 4)
        self.assertEqual(summary["unrealized_pnl"], 0)
        self.assertAlmostEqual(summary["total_fees"], 0.2)
        self.assertEqual(summary["metrics"]["trade_count"], 1)
        self.assertEqual(summary["metrics"]["win_rate"], 1)
        self.assertIsNone(summary["metrics"]["profit_factor"])
        self.assertEqual(summary["metrics"]["net_return_fraction"], 0.04)
        self.assertEqual(summary["metrics"]["maximum_drawdown_fraction"], 0)
        self.assertTrue(any("unsupported short-position" in error for error in summary["errors"]))
        self.assertEqual(summary["market_record"]["connection_status"], None)
        self.assertIsNone(summary["risk"]["daily_loss_limit"])
        self.assertIsNone(summary["risk"]["trading_lockout"])
        self.assertEqual(summary["trade_history"][0]["reason"], "Target reached.")

    def test_profit_factor_is_undefined_without_losses_and_missing_pnl_is_unavailable(self):
        event = self.account_event(
            event="trade_closed",
            details={"net_realized_pnl": 5},
        )
        self.write_journal((event,))
        no_loss = summarize_journal(load_broker_journal(self.journal_path))
        self.assertIsNone(no_loss["metrics"]["profit_factor"])
        self.assertIn("undefined", no_loss["metrics"]["profit_factor_note"])

        event["details"] = {}
        self.write_journal((event,))
        missing_pnl = summarize_journal(load_broker_journal(self.journal_path))
        self.assertIsNone(missing_pnl["metrics"]["profit_factor"])
        self.assertIsNone(missing_pnl["metrics"]["win_rate"])
        self.assertIsNone(missing_pnl["total_fees"])

    def test_open_position_uses_recorded_mark_and_fee_only(self):
        position = {
            "strategy": "momentum_breakout",
            "direction": "long",
            "quantity_btc": 0.5,
            "entry_price": 100,
            "entry_reference_price": 100,
            "entry_fee": 0.05,
            "entry_spread_fraction": 0.001,
            "stop_price": 99,
            "target_price": 101,
            "entry_timestamp": "2026-10-21T12:00:00+00:00",
            "entry_spread_cost": 0.025,
            "entry_slippage_cost": 0.025,
        }
        state = {
            "initial_cash": 100,
            "cash": 49.95,
            "realized_pnl": 0,
            "position": position,
            "peak_equity": 100,
            "day_key": "2026-10-21",
            "day_start_equity": 100,
            "last_bid": 100.5,
            "last_ask": 100.6,
        }
        self.write_journal((self.account_event(state=state),))
        summary = summarize_journal(load_broker_journal(self.journal_path))

        self.assertAlmostEqual(summary["equity"], 100.2)
        self.assertAlmostEqual(summary["unrealized_pnl"], 0.2)
        self.assertEqual(summary["total_fees"], 0.05)
        self.assertEqual(summary["open_position"], position)

    def test_malformed_and_missing_backtest_results_and_trades(self):
        absent = load_backtest_period(self.root / "absent")
        self.assertIsNone(absent["results"])
        self.assertEqual(absent["trades"], [])
        self.assertEqual(len(absent["errors"]), 2)

        directory = self.root / "bad"
        directory.mkdir()
        (directory / "results.json").write_text("[]", encoding="utf-8")
        (directory / "trades.csv").write_text("not,a,known,schema\n1,2,3,4\n", encoding="utf-8")
        loaded = load_backtest_period(directory)
        self.assertIsNone(loaded["results"])
        self.assertEqual(loaded["trades"], [])
        self.assertIn("must contain a JSON object", loaded["errors"][0])
        self.assertTrue(any("missing expected fields" in error for error in loaded["errors"]))

    def test_valid_backtest_metrics_are_read_separately(self):
        development = self.root / "development"
        oos = self.root / "out_of_sample"
        development.mkdir()
        oos.mkdir()
        result = {
            "strategy": "momentum_breakout",
            "overall": {
                "initial_equity": 100,
                "final_equity": 102,
                "net_return_fraction": 0.02,
                "maximum_drawdown_fraction": 0.01,
                "profit_factor": 2,
                "win_rate": 0.5,
                "trade_count": 2,
                "total_fees": 0.4,
            },
        }
        (development / "results.json").write_text(json.dumps(result), encoding="utf-8")
        (oos / "results.json").write_text(
            json.dumps({
                **result,
                "overall": {**result["overall"], "final_equity": 99, "net_return_fraction": -0.01},
            }),
            encoding="utf-8",
        )
        (development / "trades.csv").write_text("direction\nlong\n", encoding="utf-8")
        (oos / "trades.csv").write_text("direction\nshort\n", encoding="utf-8")

        dev_data = load_backtest_period(development)
        oos_data = load_backtest_period(oos)
        self.assertEqual(
            available_metric(dev_data["results"], "overall", "final_equity"),
            102,
        )
        self.assertIsNone(oos_data["results"])
        self.assertEqual(oos_data["trades"], [])
        self.assertTrue(any("unsupported short trade" in error for error in oos_data["errors"]))
        self.assertIsNone(available_metric(oos_data["results"], "overall", "missing"))
        self.assertNotEqual(dev_data["results"], oos_data["results"])

    def test_short_metrics_exclude_entire_saved_spot_report(self):
        directory = self.root / "short_metrics"
        directory.mkdir()
        (directory / "results.json").write_text(
            json.dumps({
                "overall": {"trade_count": 2, "final_equity": 130},
                "short": {"trade_count": 1},
            }),
            encoding="utf-8",
        )
        (directory / "trades.csv").write_text(
            "entry_timestamp,exit_timestamp,direction,entry_fill_price,"
            "exit_fill_price,entry_fee,exit_fee,net_realized_pnl\n",
            encoding="utf-8",
        )

        loaded = load_backtest_period(directory)

        self.assertIsNone(loaded["results"])
        self.assertTrue(any("unsupported short trades" in error for error in loaded["errors"]))

    def test_runtime_status_label_distinguishes_fresh_stale_and_stopped(self):
        status_path = self.root / "runtime_status.json"
        now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        status_path.write_text(json.dumps({
            "process_running": True,
            "process_heartbeat": now.isoformat(),
            "latest_valid_market_data_timestamp": now.isoformat(),
            "websocket_messages_received": 12,
            "entry_diagnostics": {"completed_candle_count": 2},
        }), encoding="utf-8")

        fresh = load_runtime_status(status_path, now=now)
        stale = load_runtime_status(
            status_path, now=now.replace(minute=1)
        )
        status_path.write_text(json.dumps({
            "process_running": False,
            "process_heartbeat": now.isoformat(),
        }), encoding="utf-8")
        stopped = load_runtime_status(status_path, now=now)

        self.assertEqual(runtime_status_label(fresh), "Running (fresh heartbeat)")
        self.assertEqual(runtime_status_label(stale), "Stale or invalid heartbeat")
        self.assertIsNone(stale.data["websocket_messages_received"])
        self.assertIsNone(stale.data["entry_diagnostics"])
        self.assertEqual(
            stale.data["last_recorded_market_data_timestamp"],
            now.isoformat(),
        )
        self.assertEqual(runtime_status_label(stopped), "Stopped")

    def test_runtime_health_label_distinguishes_ready_warming_disconnected_and_stale(self):
        status_path = self.root / "runtime_status.json"
        record = {
            "process_running": True,
            "process_heartbeat": "2026-10-22T12:00:00+00:00",
            "connection_status": "connected",
            "market_data_healthy": True,
            "latest_valid_market_data_timestamp": "2026-10-22T12:00:00+00:00",
            "market_data_age_seconds": 0,
            "market_data_stale_limit_seconds": 3,
            "spread": 0.02,
            "entry_permission": True,
            "paper_only": True,
            "risk_lockout": False,
            "open_position": False,
            "journal_ok": True,
            "status_write_ok": True,
            "candle_fresh": True,
            "candle_connection_status": "connected",
            "candle_data_healthy": True,
            "candle_data_age_seconds": 0,
            "candle_data_stale_limit_seconds": 90,
            "candle_interval": "5m",
            "candle_interval_seconds": 300,
            "latest_completed_candle_timestamp": "2026-10-22T11:59:59.999000+00:00",
            "entry_diagnostics": {
                "completed_candle_count": 21,
                "required_completed_candle_count": 21,
                "warmup_candles_remaining": 0,
                "candle_history_contiguous": True,
                "strategy_warmup_ready": True,
            },
            "exit_pending": None,
        }
        status_path.write_text(json.dumps(record), encoding="utf-8")
        healthy = load_runtime_status(
            status_path,
            now=datetime(2026, 10, 22, 12, 0, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(
            runtime_health_label(healthy),
            "Healthy (quote and candle feeds ready)",
        )
        self.assertEqual(healthy.data["candle_interval"], "5m")

        stale_quote_record = {
            **record,
            "latest_valid_market_data_timestamp": "2026-10-22T11:59:56+00:00",
        }
        status_path.write_text(json.dumps(stale_quote_record), encoding="utf-8")
        stale_quote = load_runtime_status(
            status_path,
            now=datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            stale_quote.data["market_data_age_seconds"],
            4.0,
        )
        self.assertFalse(stale_quote.data["market_data_healthy"])
        self.assertEqual(runtime_health_label(stale_quote), "Stale or unhealthy quote feed")
        self.assertFalse(stale_quote.data["entry_permission"])
        self.assertIn("stale or unhealthy", stale_quote.data["entry_permission_reason"])

        stale_candle_record = {
            **record,
            "latest_completed_candle_timestamp": (
                datetime(2026, 10, 22, 11, 58, 29, tzinfo=timezone.utc).isoformat()
            ),
        }
        status_path.write_text(json.dumps(stale_candle_record), encoding="utf-8")
        stale_candle = load_runtime_status(
            status_path,
            now=datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc),
        )
        self.assertFalse(stale_candle.data["candle_fresh"])
        self.assertEqual(runtime_health_label(stale_candle), "Stale candle data")
        self.assertFalse(stale_candle.data["entry_permission"])

        waiting_candle_record = {
            **record,
            "candle_fresh": False,
            "candle_data_healthy": False,
        }
        status_path.write_text(json.dumps(waiting_candle_record), encoding="utf-8")
        waiting_candle = load_runtime_status(
            status_path,
            now=datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc),
        )
        self.assertLess(waiting_candle.data["candle_data_age_seconds"], 90)
        self.assertEqual(
            runtime_health_label(waiting_candle),
            "Stale or unhealthy candle feed",
        )

        warming_record = {
            **record,
            "candle_fresh": True,
            "entry_permission": False,
            "entry_diagnostics": {
                "completed_candle_count": 18,
                "required_completed_candle_count": 21,
                "warmup_candles_remaining": 3,
            },
        }
        status_path.write_text(json.dumps(warming_record), encoding="utf-8")
        warming = load_runtime_status(
            status_path,
            now=datetime(2026, 10, 22, 12, 0, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(runtime_health_label(warming), "Warming up candles")

        disconnected_record = {
            **record,
            "connection_status": "disconnected",
            "market_data_healthy": False,
            "entry_permission": False,
        }
        status_path.write_text(json.dumps(disconnected_record), encoding="utf-8")
        disconnected = load_runtime_status(
            status_path,
            now=datetime(2026, 10, 22, 12, 0, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(
            runtime_health_label(disconnected),
            "Disconnected or unhealthy quote feed",
        )

        disconnected_candle = {
            **record,
            "candle_connection_status": "disconnected",
            "candle_data_healthy": False,
            "entry_permission": False,
            "entry_diagnostics": {
                "completed_candle_count": 0,
                "required_completed_candle_count": 21,
                "candle_history_contiguous": False,
                "strategy_warmup_ready": False,
            },
        }
        status_path.write_text(json.dumps(disconnected_candle), encoding="utf-8")
        unhealthy_candle = load_runtime_status(
            status_path,
            now=datetime(2026, 10, 22, 12, 0, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(
            runtime_health_label(unhealthy_candle),
            "Disconnected or unhealthy candle feed",
        )

        unavailable_readiness = {
            **record,
            "entry_diagnostics": {},
        }
        status_path.write_text(json.dumps(unavailable_readiness), encoding="utf-8")
        unknown_readiness = load_runtime_status(
            status_path,
            now=datetime(2026, 10, 22, 12, 0, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(runtime_health_label(unknown_readiness), "Readiness unavailable")
        self.assertFalse(unknown_readiness.data["entry_permission"])

        stale = load_runtime_status(
            status_path,
            now=datetime(2026, 10, 22, 12, 0, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(runtime_health_label(stale), "Stale or invalid heartbeat")
        self.assertIsNone(stale.data["candle_connection_status"])
        self.assertIsNone(stale.data["candle_interval"])
        self.assertIsNone(stale.data["exit_pending"])

    def test_missing_and_malformed_runtime_status_are_unavailable(self):
        missing = load_runtime_status(self.root / "missing-status.json")
        self.assertEqual(runtime_status_label(missing), "Unavailable")
        self.assertEqual(runtime_health_label(missing), "Unavailable")

        malformed_path = self.root / "malformed-status.json"
        malformed_path.write_text("{invalid", encoding="utf-8")
        malformed = load_runtime_status(malformed_path)
        self.assertIsNone(malformed.process_running)
        self.assertEqual(runtime_status_label(malformed), "Unavailable")
        self.assertEqual(runtime_health_label(malformed), "Unavailable")

    def test_pending_exit_blocks_dashboard_entry_permission(self):
        status_path = self.root / "pending-exit-status.json"
        record = {
            "process_running": True,
            "process_heartbeat": "2026-10-22T12:00:00+00:00",
            "connection_status": "connected",
            "market_data_healthy": True,
            "latest_valid_market_data_timestamp": "2026-10-22T12:00:00+00:00",
            "market_data_age_seconds": 0,
            "market_data_stale_limit_seconds": 3,
            "spread": 0.02,
            "entry_permission": True,
            "paper_only": True,
            "risk_lockout": False,
            "open_position": True,
            "journal_ok": True,
            "status_write_ok": True,
            "candle_fresh": True,
            "candle_connection_status": "connected",
            "candle_data_healthy": True,
            "entry_diagnostics": {
                "completed_candle_count": 21,
                "required_completed_candle_count": 21,
                "candle_history_contiguous": True,
                "strategy_warmup_ready": True,
            },
            "exit_pending": {
                "trigger": "stop_loss",
                "reason": "Stop triggered during stale quote.",
                "awaiting": "fresh executable bid",
            },
        }
        status_path.write_text(json.dumps(record), encoding="utf-8")

        parsed = load_runtime_status(
            status_path,
            now=datetime(2026, 10, 22, 12, 0, 1, tzinfo=timezone.utc),
        )

        self.assertFalse(parsed.data["entry_permission"])
        self.assertEqual(parsed.data["exit_pending"]["trigger"], "stop_loss")
        self.assertIn(
            "pending fresh-quote verification",
            parsed.data["entry_permission_reason"],
        )

        record["exit_pending"] = ["malformed"]
        status_path.write_text(json.dumps(record), encoding="utf-8")
        malformed_pending = load_runtime_status(
            status_path,
            now=datetime(2026, 10, 22, 12, 0, 1, tzinfo=timezone.utc),
        )
        self.assertFalse(malformed_pending.data["entry_permission"])
        self.assertIsNone(malformed_pending.data["exit_pending"])
        self.assertTrue(any(
            "exit-pending state is malformed" in error
            for error in malformed_pending.errors
        ))

    def test_dashboard_data_loaders_reread_updated_files(self):
        status_path = self.root / "runtime_status.json"
        journal_path = self.journal_path
        backtest_path = self.root / "development"
        backtest_path.mkdir()
        now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        status_record = {
            "process_running": True,
            "process_heartbeat": now.isoformat(),
        }
        status_path.write_text(json.dumps(status_record), encoding="utf-8")
        self.write_journal((self.account_event(),))
        (backtest_path / "results.json").write_text(
            json.dumps({"overall": {"final_equity": 100}}), encoding="utf-8"
        )
        (backtest_path / "trades.csv").write_text(
            "entry_timestamp,exit_timestamp,direction,entry_fill_price,"
            "exit_fill_price,entry_fee,exit_fee,net_realized_pnl\n",
            encoding="utf-8",
        )

        first_status = load_runtime_status(status_path, now=now)
        first_journal = load_broker_journal(journal_path)
        first_backtest = load_backtest_period(backtest_path)

        status_record["process_heartbeat"] = (now.replace(second=1)).isoformat()
        status_path.write_text(json.dumps(status_record), encoding="utf-8")
        self.write_journal((
            self.account_event(),
            self.account_event(timestamp="2026-10-21T12:01:00+00:00"),
        ))
        (backtest_path / "results.json").write_text(
            json.dumps({"overall": {"final_equity": 101}}), encoding="utf-8"
        )

        second_status = load_runtime_status(status_path, now=now)
        second_journal = load_broker_journal(journal_path)
        second_backtest = load_backtest_period(backtest_path)

        self.assertNotEqual(
            first_status.data["process_heartbeat"],
            second_status.data["process_heartbeat"],
        )
        self.assertEqual(len(first_journal.events), 1)
        self.assertEqual(len(second_journal.events), 2)
        self.assertEqual(first_backtest["results"]["overall"]["final_equity"], 100)
        self.assertEqual(second_backtest["results"]["overall"]["final_equity"], 101)


if __name__ == "__main__":
    unittest.main()
