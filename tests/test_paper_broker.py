import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    LIVE_EXECUTION_ENABLED,
    PAPER_TRADING_ONLY,
    STARTING_BALANCE_AMOUNT,
    STARTING_BALANCE_CURRENCY,
)
from dashboard_data import load_broker_journal
from paper_broker import (
    EXIT_FILL_DECISION_PRICE,
    PaperBroker,
    PaperBrokerSettings,
)
from strategy import SimulatedPosition, StrategyDecision


class PaperBrokerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        self.tempdir = tempfile.TemporaryDirectory()
        self.journal = Path(self.tempdir.name) / "broker.jsonl"
        self.broker = PaperBroker(PaperBrokerSettings(journal_path=self.journal))
        self.health = self.market_health(self.now)

    def tearDown(self):
        self.tempdir.cleanup()

    def market_health(self, timestamp, *, bid=99.99, ask=100.01, **updates):
        status = {
            "healthy": True,
            "entry_allowed": True,
            "connection_status": "connected",
            "bid": bid,
            "ask": ask,
            "spread": ask - bid,
            "message_timestamp": timestamp.isoformat(),
            "last_valid_update": timestamp.isoformat(),
            "age_seconds": 0.0,
        }
        status.update(updates)
        return status

    def entry(self, direction="long", *, timestamp=None, quantity=0.8):
        timestamp = timestamp or self.now
        if direction == "long":
            reference, stop, target = 100.01, 99.8, 101
        else:
            reference, stop, target = 99.99, 100.2, 99
        position = SimulatedPosition(
            "test_strategy", direction, reference, quantity, stop, target, timestamp
        )
        return StrategyDecision(
            "entry",
            "test_strategy",
            direction,
            timestamp,
            "synthetic test entry",
            {"quantity_btc": quantity},
            position,
        )

    def exit(self, direction, price, *, timestamp=None, reason="Synthetic exit"):
        return StrategyDecision(
            "exit",
            "test_strategy",
            direction,
            timestamp or self.now + timedelta(minutes=1),
            reason,
            {"exit_price": price},
        )

    def journal_events(self):
        return [
            json.loads(line)
            for line in self.journal.read_text(encoding="utf-8").splitlines()
        ]

    def test_starting_cash_and_initial_accounting(self):
        self.assertEqual(self.broker.cash, 100.0)
        self.assertEqual(self.broker.equity, 100.0)
        self.assertEqual(self.broker.realized_pnl, 0.0)
        self.assertEqual(self.broker.unrealized_pnl, 0.0)
        initialized = self.journal_events()[0]
        self.assertEqual(initialized["details"]["currency_basis"], "USDT")
        self.assertEqual(initialized["state"]["currency"], "USDT")

    def test_long_entry_and_exit_apply_costs_once_and_reconcile_pnl(self):
        result = self.broker.process(self.entry("long"), market_health=self.health)
        self.assertTrue(result.accepted)
        self.assertEqual(result.action, "entry")
        self.assertLess(self.broker.cash, 100.0)
        self.assertIsNotNone(self.broker.position)
        self.assertAlmostEqual(
            self.broker.equity,
            self.broker.cash + self.broker.position.quantity_btc * 99.99,
        )
        entry_event = self.journal_events()[-1]
        self.assertEqual(entry_event["event"], "entry_fill")
        self.assertGreater(entry_event["details"]["fee"], 0)
        self.assertGreater(entry_event["details"]["spread_cost_estimate"], 0)
        self.assertGreater(entry_event["details"]["slippage_cost_estimate"], 0)

        exit_time = self.now + timedelta(minutes=1)
        close = self.broker.process(
            self.exit("long", 101, timestamp=exit_time),
            market_health=self.market_health(exit_time, bid=100.99, ask=101.01),
        )
        self.assertTrue(close.accepted)
        self.assertEqual(close.action, "exit")
        self.assertAlmostEqual(close.fill_price, 100.99 * (1 - self.broker.settings.slippage_rate_per_side))
        self.assertIsNone(self.broker.position)
        self.assertAlmostEqual(self.broker.equity, 100 + self.broker.realized_pnl)
        trade = self.journal_events()[-1]["details"]
        self.assertEqual(self.journal_events()[-1]["event"], "trade_closed")
        self.assertAlmostEqual(
            trade["net_realized_pnl"],
            trade["gross_pnl"] - trade["entry_fee"] - trade["exit_fee"],
        )
        self.assertGreater(trade["total_costs"], trade["entry_fee"] + trade["exit_fee"])

    def test_short_entry_is_explicitly_rejected_for_spot(self):
        result = self.broker.process(self.entry("short"), market_health=self.health)
        self.assertIsNone(self.broker.position)
        self.assertFalse(result.accepted)
        self.assertIn("Binance Spot", result.reason)
        self.assertEqual(self.journal_events()[-1]["event"], "risk_rejection")
        self.assertIn("long-only", self.journal_events()[-1]["details"]["reason"])
        with self.assertRaisesRegex(ValueError, "does not support short"):
            PaperBrokerSettings(allow_simulated_shorts=True)

    def test_unverifiable_exit_stays_pending_then_closes_once_on_fresh_quote(self):
        self.broker.process(self.entry("long"), market_health=self.health)
        timestamp = self.now + timedelta(minutes=1)
        unhealthy = self.market_health(
            timestamp,
            healthy=False,
            entry_allowed=False,
            connection_status="disconnected",
        )
        stopped = self.broker.process(
            self.exit("long", 99.8, timestamp=timestamp, reason="Stop reached."),
            market_health=unhealthy,
        )
        self.assertFalse(stopped.accepted)
        self.assertEqual(stopped.action, "exit_pending")
        self.assertIsNotNone(self.broker.position)
        self.assertEqual(self.broker.exit_pending["trigger"], "stop_loss")
        self.assertTrue(self.broker.exit_pending["unresolved_simulated_exposure"])
        self.assertFalse(self.broker.exit_pending["loss_bound_guaranteed"])
        cash_before_restore = self.broker.cash
        entry_fee_before_restore = self.broker.position.entry_fee
        self.assertEqual(self.journal_events()[-1]["event"], "exit_pending")
        duplicate = self.broker.process(
            self.exit("long", 99.8, timestamp=timestamp, reason="Stop reached."),
            market_health=unhealthy,
        )
        self.assertEqual(duplicate.action, "exit_pending")
        self.assertEqual(
            sum(event["event"] == "exit_pending" for event in self.journal_events()),
            1,
        )
        self.broker = PaperBroker(PaperBrokerSettings(journal_path=self.journal))
        self.assertEqual(self.broker.exit_pending["trigger"], "stop_loss")
        self.assertAlmostEqual(self.broker.cash, cash_before_restore)
        self.assertAlmostEqual(self.broker.position.entry_fee, entry_fee_before_restore)

        fresh_time = timestamp + timedelta(seconds=1)
        fresh = self.market_health(fresh_time, bid=99.7, ask=99.72)
        closed = self.broker.process_protective_quote(fresh, timestamp=fresh_time)
        self.assertTrue(closed.accepted)
        self.assertIsNone(self.broker.position)
        events_after_close = self.journal_events()
        self.assertEqual(sum(event["event"] == "trade_closed" for event in events_after_close), 1)
        self.assertEqual(sum(event["event"] == "exit_pending" for event in events_after_close), 1)
        exit_fee = events_after_close[-1]["details"]["exit_fee"]
        self.assertIsNone(
            self.broker.process_protective_quote(fresh, timestamp=fresh_time)
        )
        self.assertEqual(
            sum(event["event"] == "trade_closed" for event in self.journal_events()),
            1,
        )
        self.assertEqual(self.journal_events()[-1]["details"]["exit_fee"], exit_fee)
        self.assertLess(self.broker.realized_pnl, 0)

    def test_pending_exit_keeps_last_mark_equity_without_fabricating_a_fill(self):
        self.broker.process(self.entry("long"), market_health=self.health)
        equity_before = self.broker.equity
        last_bid = self.broker.last_bid
        timestamp = self.now + timedelta(minutes=1)
        unhealthy = self.market_health(
            timestamp,
            healthy=False,
            entry_allowed=False,
            connection_status="disconnected",
        )
        pending = self.broker.process(
            self.exit("long", 99.8, timestamp=timestamp, reason="Stop reached."),
            market_health=unhealthy,
        )
        mark = self.broker.mark_to_market(unhealthy, timestamp=timestamp)

        self.assertEqual(pending.action, "exit_pending")
        self.assertFalse(mark.accepted)
        self.assertEqual(self.broker.last_bid, last_bid)
        self.assertAlmostEqual(self.broker.equity, equity_before)
        self.assertIsNotNone(self.broker.position)
        self.assertTrue(self.broker.exit_pending["unresolved_simulated_exposure"])
        self.assertFalse(any(
            event["event"] == "trade_closed" for event in self.journal_events()
        ))

    def test_decision_exit_price_mode_changes_only_the_exit_reference(self):
        quote_exit_time = self.now + timedelta(minutes=1)
        self.broker.process(self.entry("long"), market_health=self.health)
        quote_exit = self.broker.process(
            self.exit("long", 99.8, timestamp=quote_exit_time, reason="Stop reached."),
            market_health=self.market_health(quote_exit_time, bid=99.5, ask=99.52),
        )
        quote_fill = quote_exit.fill_price
        quote_entry = [
            event["details"]["fill_price"]
            for event in self.journal_events()
            if event["event"] == "entry_fill"
        ][0]

        decision_journal = Path(self.tempdir.name) / "decision.jsonl"
        decision_broker = PaperBroker(
            PaperBrokerSettings(
                journal_path=decision_journal,
                exit_fill_mode=EXIT_FILL_DECISION_PRICE,
            )
        )
        decision_broker.process(self.entry("long"), market_health=self.health)
        decision_exit = decision_broker.process(
            self.exit("long", 99.8, timestamp=quote_exit_time, reason="Stop reached."),
            market_health=self.market_health(quote_exit_time, bid=99.5, ask=99.52),
        )
        decision_entry = [
            json.loads(line)["details"]["fill_price"]
            for line in decision_journal.read_text(encoding="utf-8").splitlines()
            if json.loads(line)["event"] == "entry_fill"
        ][0]

        self.assertAlmostEqual(quote_entry, decision_entry)
        self.assertAlmostEqual(quote_fill, 99.5 * (1 - 0.0005))
        self.assertAlmostEqual(decision_exit.fill_price, 99.8 * (1 - 0.0005))
        self.assertNotAlmostEqual(quote_fill, decision_exit.fill_price)

    def test_unchanged_market_marks_do_not_append_duplicate_snapshots(self):
        first_mark = self.broker.mark_to_market(
            self.health,
            timestamp=self.now,
        )
        self.assertTrue(first_mark.accepted)
        before = len(self.journal_events())

        later = self.now + timedelta(seconds=5)
        unchanged_health = self.market_health(later)
        unchanged_mark = self.broker.mark_to_market(
            unchanged_health,
            timestamp=later,
        )

        self.assertTrue(unchanged_mark.accepted)
        self.assertIn("unchanged", unchanged_mark.reason)
        self.assertEqual(len(self.journal_events()), before)

        changed_health = self.market_health(
            later + timedelta(seconds=5),
            bid=100.00,
            ask=100.02,
        )
        self.broker.mark_to_market(
            changed_health,
            timestamp=later + timedelta(seconds=5),
        )
        self.assertEqual(
            sum(
                event["event"] == "mark_to_market"
                for event in self.journal_events()
            ),
            2,
        )

    def test_missing_or_stale_exit_quote_never_fabricates_a_fill(self):
        self.broker.process(self.entry(), market_health=self.health)
        timestamp = self.now + timedelta(minutes=1)
        stale = self.market_health(
            timestamp - timedelta(seconds=5),
            age_seconds=5,
        )
        for health in (stale, {}):
            result = self.broker.process(
                self.exit("long", 99.8, timestamp=timestamp, reason="Stop reached."),
                market_health=health,
            )
            self.assertFalse(result.accepted)
            self.assertEqual(result.action, "exit_pending")
            self.assertIsNotNone(self.broker.position)
            self.assertFalse(any(
                event["event"] == "trade_closed" for event in self.journal_events()
            ))
        self.assertEqual(
            sum(event["event"] == "exit_pending" for event in self.journal_events()),
            1,
        )

    def test_duplicate_entry_and_second_exit_are_rejected(self):
        first = self.broker.process(self.entry(), market_health=self.health)
        second = self.broker.process(self.entry(), market_health=self.health)
        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertIn("already exists", second.reason)

        timestamp = self.now + timedelta(minutes=1)
        self.broker.process(
            self.exit("long", 100, timestamp=timestamp),
            market_health=self.market_health(timestamp),
        )
        denied = self.broker.process(
            self.exit("long", 100, timestamp=timestamp + timedelta(minutes=1)),
            market_health=self.market_health(timestamp + timedelta(minutes=1)),
        )
        self.assertFalse(denied.accepted)
        self.assertIn("No open position", denied.reason)

    def test_unhealthy_stale_and_invalid_market_data_reject_entries(self):
        stale = self.market_health(
            self.now - timedelta(seconds=5), age_seconds=5
        )
        unhealthy = self.market_health(self.now, healthy=False, entry_allowed=False)
        invalid_quote = self.market_health(self.now, bid=101, ask=100)
        invalid_time = self.market_health(self.now, last_valid_update="invalid")
        inconsistent_time = self.market_health(
            self.now,
            message_timestamp=(self.now + timedelta(seconds=1)).isoformat(),
        )
        for market_data in (
            stale, unhealthy, invalid_quote, invalid_time, inconsistent_time
        ):
            with self.subTest(market_data=market_data):
                broker = PaperBroker(PaperBrokerSettings(journal_path=self.journal))
                result = broker.process(self.entry(), market_health=market_data)
                self.assertFalse(result.accepted)
                self.assertIsNone(broker.position)

    def test_invalid_size_and_over_budget_risk_are_rejected(self):
        zero_size = self.entry(quantity=0)
        oversized_risk = self.entry(quantity=1.5)
        for decision in (zero_size, oversized_risk):
            result = self.broker.process(decision, market_health=self.health)
            self.assertFalse(result.accepted)
            self.assertIsNone(self.broker.position)

    def test_daily_loss_and_drawdown_limits_block_subsequent_entries(self):
        for limit_name in ("daily_loss_limit_fraction", "max_drawdown_fraction"):
            with self.subTest(limit=limit_name), tempfile.TemporaryDirectory() as root:
                settings = PaperBrokerSettings(
                    journal_path=Path(root) / "risk.jsonl",
                    daily_loss_limit_fraction=(
                        0.001 if limit_name == "daily_loss_limit_fraction" else 0.99
                    ),
                    max_drawdown_fraction=(
                        0.001 if limit_name == "max_drawdown_fraction" else 0.99
                    ),
                )
                broker = PaperBroker(settings)
                broker.process(self.entry(quantity=0.8), market_health=self.health)
                timestamp = self.now + timedelta(minutes=1)
                lower = self.market_health(timestamp, bid=99.0, ask=99.02)
                broker.mark_to_market(lower, timestamp=timestamp)
                broker.process(
                    self.exit("long", 99.0, timestamp=timestamp),
                    market_health=lower,
                )
                next_time = timestamp + timedelta(minutes=1)
                next_entry = self.entry(timestamp=next_time)
                rejected = broker.process(
                    next_entry,
                    market_health=self.market_health(next_time),
                )
                self.assertFalse(rejected.accepted)
                self.assertTrue(
                    ("daily-loss" if limit_name == "daily_loss_limit_fraction"
                     else "drawdown") in rejected.reason.lower()
                )

    def test_unhealthy_market_does_not_prevent_strategy_stop_signal(self):
        from strategy import Bar, MomentumBreakoutStrategy

        bars = [
            Bar(
                self.now + timedelta(minutes=index),
                100,
                100.2,
                99.8,
                100,
            )
            for index in range(20)
        ]
        bars.extend(
            (
                Bar(self.now + timedelta(minutes=20), 100.3, 100.4, 100, 100.3),
                Bar(self.now + timedelta(minutes=21), 100, 100.1, 99.5, 99.7),
            )
        )
        position = SimulatedPosition(
            "momentum_breakout", "long", 100.01, 0.1, 99.8, 101,
            self.now + timedelta(minutes=20),
        )
        result = MomentumBreakoutStrategy().evaluate(
            bars,
            decision_time=bars[-1].timestamp,
            market_health={"healthy": False, "entry_allowed": False},
            equity=100,
            position=position,
        )
        self.assertEqual(result.action, "exit")
        self.assertIn("Stop reached", result.reason)

    def test_persistent_journal_restores_account_and_records_trade_events(self):
        self.broker.process(self.entry(), market_health=self.health)
        legacy_records = self.journal_events()
        for record in legacy_records:
            record["state"].pop("currency", None)
        self.journal.write_text(
            "".join(json.dumps(record) + "\n" for record in legacy_records),
            encoding="utf-8",
        )
        restored = PaperBroker(PaperBrokerSettings(journal_path=self.journal))
        self.assertIsNotNone(restored.position)
        self.assertEqual(restored.position.direction, "long")
        self.assertAlmostEqual(restored.cash, self.broker.cash)
        self.assertAlmostEqual(restored.equity, self.broker.equity)
        self.assertAlmostEqual(
            restored.position.entry_fee,
            self.broker.position.entry_fee,
        )

        timestamp = self.now + timedelta(minutes=1)
        restored.process(
            self.exit("long", 101, timestamp=timestamp),
            market_health=self.market_health(timestamp, bid=100.99, ask=101.01),
        )
        realized_after_close = restored.realized_pnl
        events = self.journal_events()
        self.assertEqual(events[0]["event"], "account_initialized")
        self.assertEqual(events[-1]["event"], "trade_closed")
        self.assertTrue(all("timestamp" in event and "state" in event for event in events))
        restored_closed = PaperBroker(PaperBrokerSettings(journal_path=self.journal))
        self.assertIsNone(restored_closed.position)
        self.assertAlmostEqual(restored_closed.realized_pnl, realized_after_close)
        self.assertEqual(
            sum(event["event"] == "entry_fill" for event in self.journal_events()),
            1,
        )
        self.assertEqual(
            sum(event["event"] == "trade_closed" for event in self.journal_events()),
            1,
        )

    def test_partial_or_malformed_final_record_fails_restore_without_rewriting_journal(self):
        self.broker.process(self.entry(), market_health=self.health)
        valid_journal = self.journal.read_bytes()
        suffixes = (
            (b'{"event":', "not newline-terminated"),
            (b"{not-json}\n", "Invalid broker journal JSON"),
            (
                b'{"timestamp":"2026-10-21T12:00:00+00:00",'
                b'"event":"mark_to_market","details":{},"state":{}}',
                "not newline-terminated",
            ),
            (b"\xff\n", "invalid UTF-8"),
        )
        for suffix, expected_error in suffixes:
            with self.subTest(suffix=suffix):
                self.journal.write_bytes(valid_journal + suffix)
                original = self.journal.read_bytes()

                with self.assertRaisesRegex(ValueError, expected_error):
                    PaperBroker(PaperBrokerSettings(journal_path=self.journal))

                self.assertEqual(self.journal.read_bytes(), original)
                dashboard_journal = load_broker_journal(self.journal)
                self.assertGreaterEqual(len(dashboard_journal.events), 2)
                self.assertTrue(dashboard_journal.errors)

    def test_restore_rejects_unsupported_short_position(self):
        self.broker.process(self.entry(), market_health=self.health)
        records = self.journal_events()
        records[-1]["state"]["position"]["direction"] = "short"
        self.journal.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "unsupported Spot position"):
            PaperBroker(PaperBrokerSettings(journal_path=self.journal))

    def test_stage_one_paper_safety_defaults_remain_unchanged(self):
        self.assertIs(PAPER_TRADING_ONLY, True)
        self.assertIs(LIVE_EXECUTION_ENABLED, False)
        self.assertEqual(STARTING_BALANCE_AMOUNT, 100.0)
        self.assertEqual(STARTING_BALANCE_CURRENCY, "USDT")
        self.assertEqual(PaperBrokerSettings().initial_cash, 100.0)


if __name__ == "__main__":
    unittest.main()
