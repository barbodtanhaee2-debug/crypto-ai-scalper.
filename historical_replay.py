"""Chronological historical replay with an isolated paper account."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Sequence

from backtester import BacktestSettings, _health, _position_for_strategy
from paper_broker import PaperBroker, PaperBrokerSettings
from strategy import (
    Bar,
    MomentumBreakoutStrategy,
    MovingAverageCrossoverStrategy,
    RangeMeanReversionStrategy,
    StrategyDecision,
    VolatilityTrendFollowingStrategy,
)

REPLAY_STRATEGIES = {
    "momentum_breakout": MomentumBreakoutStrategy,
    "range_mean_reversion": RangeMeanReversionStrategy,
    "moving_average_crossover": MovingAverageCrossoverStrategy,
    "volatility_trend_following": VolatilityTrendFollowingStrategy,
}
PLAYBACK_SPEEDS = (1, 2, 5, 10, 30, 60)
DEFAULT_LIVE_JOURNAL = Path("paper_broker.jsonl")
DEFAULT_LIVE_STATUS = Path("runtime_status.json")


def resolve_replay_journal(path: str | Path, *, live_journal: str | Path | None = None) -> Path:
    journal = Path(path).expanduser().resolve()
    forbidden = {
        DEFAULT_LIVE_JOURNAL.expanduser().resolve(),
        DEFAULT_LIVE_STATUS.expanduser().resolve(),
    }
    if live_journal is not None:
        forbidden.add(Path(live_journal).expanduser().resolve())
    if journal in forbidden or journal.name in {"paper_broker.jsonl", "runtime_status.json"}:
        raise ValueError(
            "Replay journal must not use the live paper runtime journal or status file."
        )
    return journal


def make_strategy(name: str):
    try:
        return REPLAY_STRATEGIES[name]()
    except KeyError as error:
        raise ValueError(f"Unknown replay strategy: {name}") from error


class HistoricalReplay:
    """Advance one completed candle at a time; never inspects future bars."""

    def __init__(
        self,
        candles: Sequence[Bar],
        *,
        strategy_name: str,
        journal_path: str | Path,
        settings: BacktestSettings | None = None,
        live_journal: str | Path | None = None,
    ) -> None:
        self.settings = settings or BacktestSettings()
        if self.settings.candle_interval_seconds < 60:
            raise ValueError("Replay requires a whole-minute candle interval.")
        self.candles = tuple(candles)
        if not self.candles:
            raise ValueError("Replay requires historical candles.")
        self.journal_path = resolve_replay_journal(
            journal_path,
            live_journal=live_journal,
        )
        self.strategy_name = strategy_name
        self._index = -1
        self._trades: list[dict] = []
        self._markers: list[dict[str, object]] = []
        self._closes: list[dict[str, object]] = []
        self._last_decision: StrategyDecision | None = None
        self.restart()

    @property
    def cursor(self) -> int:
        return self._index

    @property
    def finished(self) -> bool:
        return self._index >= len(self.candles) - 1

    @property
    def current_bar(self) -> Bar | None:
        if self._index < 0:
            return None
        return self.candles[self._index]

    def restart(self) -> None:
        if self.journal_path.exists():
            self.journal_path.unlink()
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        strategy = make_strategy(self.strategy_name)
        strategy_settings = replace(
            strategy.settings,
            fee_rate_per_side=self.settings.fee_rate_per_side,
            slippage_rate_per_side=self.settings.effective_slippage_rate_per_side,
            risk_fraction=self.settings.risk_fraction,
            max_holding_bars=(
                strategy.settings.max_holding_bars
                * (self.settings.candle_interval_seconds // 60)
            ),
        )
        self._strategy = type(strategy)(strategy_settings)
        self.broker = PaperBroker(
            PaperBrokerSettings(
                initial_cash=self.settings.initial_equity,
                fee_rate_per_side=self.settings.fee_rate_per_side,
                slippage_rate_per_side=self.settings.effective_slippage_rate_per_side,
                risk_fraction=self.settings.risk_fraction,
                daily_loss_limit_fraction=self.settings.daily_loss_limit_fraction,
                max_drawdown_fraction=self.settings.max_drawdown_fraction,
                max_market_data_age_seconds=max(
                    float(self.settings.candle_interval_seconds), 1.0
                ),
                journal_path=self.journal_path,
                allow_simulated_shorts=False,
            )
        )
        self._index = -1
        self._trades = []
        self._markers = []
        self._closes = []
        self._last_decision = None

    def available_candles(self) -> tuple[Bar, ...]:
        if self._index < 0:
            return ()
        return self.candles[: self._index + 1]

    def step(self) -> bool:
        if self.finished:
            return False
        nxt = self._index + 1
        candle = self.candles[nxt]
        available = self.candles[: nxt + 1]
        health = _health(candle, self.settings.spread_fraction)
        self.broker.mark_to_market(health, timestamp=candle.timestamp)
        decision = self._strategy.evaluate(
            available,
            decision_time=candle.timestamp,
            market_health=health,
            equity=self.broker.equity,
            position=_position_for_strategy(self.broker),
        )
        if decision.action in ("entry", "exit"):
            result = self.broker.process(decision, market_health=health)
            if result.accepted:
                marker = {
                    "action": decision.action,
                    "timestamp": candle.timestamp.isoformat(),
                    "fill_price": result.fill_price,
                    "reason": decision.reason,
                }
                self._markers.append(marker)
                if decision.action == "exit":
                    self._trades.append(
                        {
                            "strategy": decision.strategy,
                            "direction": decision.direction,
                            "reason": decision.reason,
                            "timestamp": candle.timestamp.isoformat(),
                            "fill_price": result.fill_price,
                            "quantity_btc": result.quantity_btc,
                        }
                    )
        self._last_decision = decision
        self._closes.append(
            {
                "timestamp": candle.timestamp.isoformat(),
                "close": candle.close,
            }
        )
        self._index = nxt
        return True

    def run_all(self) -> None:
        while self.step():
            pass

    def snapshot(self) -> dict[str, object]:
        bar = self.current_bar
        position = self.broker.position
        return {
            "strategy": self.strategy_name,
            "cursor": self._index,
            "finished": self.finished,
            "clock": bar.timestamp.isoformat() if bar is not None else None,
            "close": bar.close if bar is not None else None,
            "equity": self.broker.equity if bar is not None else self.broker.cash,
            "cash": self.broker.cash,
            "realized_pnl": self.broker.realized_pnl,
            "open_position": None if position is None else {
                "direction": position.direction,
                "entry_price": position.entry_price,
                "stop_price": position.stop_price,
                "target_price": position.target_price,
                "quantity_btc": position.quantity_btc,
            },
            "last_decision": None if self._last_decision is None else {
                "action": self._last_decision.action,
                "reason": self._last_decision.reason,
                "timestamp": self._last_decision.timestamp.isoformat(),
            },
            "trades": list(self._trades),
            "markers": list(self._markers),
            "price_series": list(self._closes),
            "journal_path": str(self.journal_path),
        }
