# Binance BTC Scalper

A lightweight, rules-based Binance Spot BTC scalper, developed in stages.
This project is currently at **Stage 8: Informational Market-Regime Detection**.

## Safety defaults

- Paper trading only: `PAPER_TRADING_ONLY = True`.
- Live execution disabled: `LIVE_EXECUTION_ENABLED = False`.
- Starting simulated balance: **100.0 USDT**. No EUR/USDT conversion is
  performed or implied.
- Binance Spot BTC/USDT best-bid/ask data uses its public WebSocket only;
  no API keys or authenticated endpoints are used.
- Deterministic momentum-breakout and range-only mean-reversion rules produce
  simulated signals. The paper broker is Binance Spot long-only: short-entry
  signals are explicitly rejected and cannot create positions or backtest
  trades.
- No exchange orders, live execution, leverage, or external signals are
  implemented. `StrategyExperiment` permits at most one simulated position;
  callers pass the active position back on subsequent evaluations.
- The historical backtester replays locally supplied BTC/USDT candles through
  each strategy separately and uses the Spot-only paper broker for simulated
  fills, costs, accounting, and risk limits. Unsupported short signals are
  rejected and excluded from Spot results.
- The Streamlit dashboard reads broker journals, runtime status, and separately
  saved development/out-of-sample results. It has no write path, trading
  controls, exchange connection, or order endpoint.
- The feed marks market health unhealthy when disconnected, stale, invalid,
  or awaiting a valid update after reconnect. Strategies require both
  `healthy` and `entry_allowed` from the feed before allowing an entry.

- The paper runtime uses only the public Binance Spot bookTicker WebSocket.
  It submits accepted strategy decisions to the local paper broker and writes
  an atomic `runtime_status.json`; no authenticated or order endpoints exist.
- Runtime strategy candles come from Binance's public Spot kline stream;
  supported runtime intervals are `1m`, `3m`, `5m`, `15m`, and `30m`
  (60, 180, 300, 900, and 1800 seconds). The same selected interval is used
  for the WebSocket subscription, parsed kline events, REST history requests,
  and contiguous-candle validation; unsupported intervals are rejected.
  Current bid/ask, spread, and quote freshness continue to come from the public
  bookTicker stream. The runtime attempts to seed warm-up from validated closed
  klines on Binance's unauthenticated public REST endpoint after every kline
  connection and reconnect, reconciling overlapping live candles by timestamp
  and OHLC.
  Candle timestamps use Binance's inclusive millisecond close time normalized
  to UTC for both REST history and live stream data.
  If history is unavailable or invalid, warm-up proceeds only from live closed
  candles. Gaps, out-of-order or conflicting candles, and kline reconnects
  reset contiguous history; a reconnect stays blocked while REST reseeding is
  in progress, and failed reseeding can rebuild warm-up only from subsequent
  real contiguous completed candles. No candles are fabricated. Entry
  permission requires fresh quote and candle feeds, a fresh completed candle,
  the full contiguous strategy warm-up, successful local persistence, valid
  paper-only settings, and no open position or broker risk lockout.
- Starting the runtime does not verify that a real Binance feed is reachable.
  The runtime and dashboard report only the connection state observed by the
  running process. Strategy parameters need a full warm-up before signals can
  be evaluated.

## Informational market-regime detector

`market_regime.py` classifies only completed BTC/USDT candles as `TRENDING`,
`SIDEWAYS`, `HIGH_VOLATILITY`, or `UNCERTAIN`. It is informational only and is
not connected to the runtime, strategy selection, or order simulation.

The detector requires at least 21 contiguous candles (20 returns); the default
interval is one minute, and `interval_seconds` selects another whole-minute
interval when needed. Input timestamps must be timezone-aware candle
**close-time** timestamps; CSV sources must provide this same close-time
meaning. A candle is used only when its timestamp plus 1 millisecond is at or
before the supplied `as_of`. Unfinished and future candles are excluded, while
invalid, duplicated, out-of-order, gapped, or insufficient completed history
returns `UNCERTAIN`.

The provisional heuristic thresholds are:

- `HIGH_VOLATILITY`: 20-return RMS log volatility is at least 0.35% per bar.
- `TRENDING`: absolute 20-return net movement is at least 1.0% and the
  efficiency ratio is at least 0.60.
- `SIDEWAYS`: absolute net movement is at most 0.30%, the high-low range is
  at most 1.50% of the final close, and the efficiency ratio is at most 0.35.
- Otherwise, `UNCERTAIN`.

Overlapping classifications use `HIGH_VOLATILITY`, then `TRENDING`, then
`SIDEWAYS` precedence. A qualified upward/downward trend direction remains
available as a separate diagnostic when high volatility takes precedence.
These transparent thresholds are provisional heuristics, not empirically
calibrated trading signals or claims about profitability.

## Strategy experiment

Both strategies use completed OHLC bars (one-minute by default) and only prior
bars for lookbacks. Bars after the decision timestamp reject the decision. The
default lookback is 20 bars; the momentum strategy enters on a close beyond the prior
high/low plus a 0.05% breakout buffer and targets 0.6%. The mean-reversion
strategy is enabled only when the prior range is at most 1.5% wide and enters
in the outer 20% of that range, targeting its midpoint.

Both use a 0.2% stop and a 10-bar maximum holding time by default. A stop wins
if both stop and target fall within one bar. Entries are rejected if the
observed spread exceeds 0.1%, or if estimated target movement does not exceed
round-trip costs plus a 0.1% minimum edge. The cost model assumes 0.1% fee per
side, 0.05% slippage per side, and one full observed spread per round trip.
These are configurable research assumptions and have not been verified against
actual Binance fees, fills, or slippage.

In the live paper runtime, protective stop/target checks also run on each
fresh, validated bookTicker update, using the bid to close a long. Exit fills
apply adverse slippage and fees once; the executable bid already incorporates
the spread. If a candle indicates an exit but no fresh executable quote is
available, the position remains open in an `exit_pending` state until a fresh
quote can verify and resolve it. New entries remain blocked while a position
or pending exit exists. A feed outage can delay an exit and can result in
losses beyond the planned risk budget; a stop is not guaranteed during an
outage.

Position sizing budgets 0.5% of simulated equity against the configured stop
distance plus estimated costs, and caps notional at equity (no leverage).
Realized losses can exceed the planned budget due to gaps, execution costs,
and the simplified fill assumptions. Signals and exits are logged with their
strategy, direction, timestamp, inputs, and rejection reason. This experiment
does not evaluate profitability; that requires historical data, realistic
costs, and out-of-sample testing.

Cash, equity, P&L, fees, and modeled costs are denominated in USDT throughout
the paper ledger and reports. This is not a converted €100 balance. Simulated
state must remain separate from any real account.

## Historical backtest

Use a local CSV containing `timestamp,open,high,low,close` rows. Timestamps
must be timezone-aware ISO-8601 values, represent completed-candle
availability, be strictly increasing, and have no missing intervals. The
backtester assumes the supplied data is BTC/USDT; it does not download or
verify a data source. Before interpreting results, independently verify and
record the source, symbol, interval, timestamp convention, date range,
continuity, and OHLC integrity. No historical dataset is bundled with this
project.

Example using a local one-minute candle file:

```powershell
python .\backtester.py --input .\btc_usdt_1m.csv --training-end 2026-08-01T00:00:00+00:00 --validation-end 2026-09-01T00:00:00+00:00 --output-dir .\backtest-results
```

The cutoffs are inclusive. Candles through `--training-end` form training;
candles after that through `--validation-end` form validation; later candles
form the out-of-sample test. Reports include all four fixed candidates in all
three periods. Candidate parameters are supplied before validation and are not
automatically tuned. Any candidate selection must use a prespecified
validation-only criterion; the workflow reports all candidates but does not
choose a winner because selection objectives are user-specific. Out-of-sample
results do not alter or select configurations. Every period uses an independent
paper account starting at the configured initial equity. Earlier candles are
available only for causal indicator warm-up, and warm-up-only bars are excluded
from scored performance. A period that has no scored bars after warm-up is
rejected.

The compared candidates are the existing `MomentumBreakoutStrategy` and
`RangeMeanReversionStrategy` baselines, plus:

- `MovingAverageCrossoverStrategy`: long entry when the 10-bar simple close
  average crosses above the 30-bar simple close average. Configure with
  `--ma-fast-period` and `--ma-slow-period`.
- `VolatilityTrendFollowingStrategy`: long entry when the close breaks above
  the preceding 20-bar high with a bullish close above the prior mean, while
  14-bar ATR divided by close is between 0.1% and 2.0%. Configure with
  `--trend-lookback-bars`, `--atr-period`, `--minimum-atr-fraction`, and
  `--maximum-atr-fraction`.

All candidates use the shared stop, target, risk sizing, and paper broker
accounting. They are replayed independently; the Stage 8 regime detector is
not involved. Results and trade histories are written as `results.json` and
`trades.csv` under each strategy/period directory, with a top-level
`comparison.json`.

Reports include completed trade count, net/gross P&L, return, drawdown,
win-rate, profit-factor, trade averages/expectancy, consecutive losses,
exposure, starting/ending USDT equity, and fees, spread, and slippage costs.
Fees are itemized separately; total transaction costs include fees plus
spread and slippage, while the component estimates exclude fees to prevent
double-counting. Undefined metrics are reported as `null` with an explanatory
note. End-of-period positions are force-liquidated at the last available
candle close with modeled costs, and this is flagged in the report. Scored
maximum drawdown is the peak-to-trough percentage decline in candle-close
equity snapshots, including marked-to-market positions. Market exposure is the
percentage of scored bars that began with a position open.

For cost sensitivity, every strategy and period is also replayed with fees and
base slippage multiplied by `--sensitivity-multiplier` (default 2); spread and
latency assumptions remain unchanged. This is a scenario comparison, not a
forecast or live-fill model.

Default execution assumptions are a 0.1% fee per side, 0.02% full spread,
0.05% slippage per side, and zero latency. Latency can be supplied through
`--latency-seconds`; its cost proxy adds `latency_seconds * 0.001%` adverse
slippage per side. These assumptions are configurable and are not verified
against actual Binance fees, spreads, fills, or latency. The input candle
interval defaults to 60 seconds and can be set with
`--candle-interval-seconds`; intervals must be whole minutes. Trades left open
at the end of a period are closed at the final available close with modeled
costs. Backtests are long-only Spot simulations; unsupported short signals
are rejected and do not enter reported trades.

Reports identify Binance Spot, long-only historical simulations and include
their sample period, closed-trade count, fees, and modeled costs. Backtest
execution is candle-based: each completed candle's close supplies a synthetic
reference quote, then the configured spread, slippage, and per-side fees are
modeled. OHLC data does not reveal true intrabar sequencing or executable
quotes. If a candle touches both stop and target, the strategy selects the
stop first; the simulated exit still uses the candle-close-derived quote, not
an asserted stop-price fill. Any position left open at a period's end is
liquidated against a synthetic bid derived from the final close with modeled
costs. These assumptions can differ materially from real execution, and
unsupported short data is excluded from Spot reports. Results are historical
simulations only, not live performance and do not establish profitability;
historical source quality, realistic costs, and out-of-sample review remain
necessary.

## Run the offline tests

From this directory, run:

```powershell
python -m unittest discover -s tests -v
```

## Local dashboard setup and launch

Install the pinned project dependencies:

```powershell
python -m pip install -r requirements.txt
```

From the project directory, start Streamlit bound to localhost:

```powershell
python -m streamlit run .\dashboard.py --server.address 127.0.0.1
```

The dashboard defaults to reading `paper_broker.jsonl` and
`backtest-results\`. Use the sidebar paths to point at existing local files.
It does not create missing source files or modify records. Runtime status is
shown as running only when its process heartbeat is recent; a stale heartbeat
does not imply a live market connection. Feed ages are recalculated from the
recorded quote/candle timestamps at each dashboard read, so a recently written
heartbeat cannot make stale market data appear fresh. A pending protective exit
is shown explicitly and keeps entry permission blocked. Runtime status and
broker-journal portfolio data refresh automatically every 1 second by default;
choose 1, 2, 3, 5, 10, or 30 seconds in the sidebar. Saved backtest results
refresh every 30 seconds. These scoped refreshes do not restart the runtime or
launch a separate background process. The dashboard incrementally reads
appended broker-journal lines and reuses its summary until the file changes;
truncation or replacement causes a safe reload. An incomplete final line is
excluded and warned about rather than presented as current account or
pending-exit state.

## Paper runtime

Start the paper-only runtime from this directory:

```powershell
python .\runtime.py
```

Optional local paths and freshness/reconnect settings are available, for
example:

```powershell
python .\runtime.py --journal .\paper_broker.jsonl --status .\runtime_status.json --market-stale-after-seconds 3 --max-reconnect-attempts 5
```

Set `--candle-interval-seconds 300` to use the supported `5m` interval instead
of the `1m` default. Supported runtime intervals are 60, 180, 300, 900, and
1800 seconds (`1m`, `3m`, `5m`, `15m`, and `30m`); other values are rejected.

Before accepting completed candles, the runtime samples Binance's public
`/api/v3/time` endpoint. It uses the request midpoint to correct the host UTC
clock only when the measured offset is within 5 seconds and request latency is
at most 2 seconds. The sample is refreshed every 30 seconds and expires after
120 seconds. A host-clock step greater than 0.5 seconds, an unavailable or
invalid sample, or an out-of-bound offset makes candle validation fail closed;
entries remain disabled until a fresh valid sample is available. Candle
interval, ordering, and freshness checks remain in force. Quote freshness still
uses the local receipt clock.

Stop with Ctrl+C. The runtime persists simulated account events in the broker
JSONL journal and atomically replaces its local runtime-status JSON record.
State-changing events, including entries, exits, fees, risk rejections, and
pending-exit transitions, retain their full journal snapshots. The periodic
five-second mark is appended only when its account or risk-mark snapshot
changes; unchanged quote marks do not create duplicate records. The journal is
append-only during normal operation. A malformed journal record prevents broker
recovery from proceeding, preserving earlier records for inspection instead of
silently restoring stale state or truncating the file.
Runtime status includes in-memory quote diagnostics: cumulative and current-
connection receive/accept/reject counts, bounded rejection categories, last
receive and last accepted-quote wall timestamps with monotonic ages, processing
and runtime-queue delays, event-loop lag, and reconnect attempts/count. These
are sampled only when status is written; no separate per-message diagnostic
log or journal record is added. Routine quote updates rely on the configured
runtime heartbeat for status persistence; exceptional failures and state
transitions can still write status immediately. A connected state alone never
means the quote is fresh.
The dashboard reads those files without modifying them:

```powershell
python -m streamlit run .\dashboard.py --server.address 127.0.0.1
```

### Optional public-feed smoke check

To check live public market-data connectivity without using or changing an
existing account journal, create a new timestamped directory and point the
runtime at files inside it. Confirm the chosen directory and both files do not
already exist before starting:

```powershell
$smokeDir = ".\stage5-smoke-$(Get-Date -Format yyyyMMdd-HHmmss)"
if (Test-Path $smokeDir) { throw "Smoke-test directory already exists: $smokeDir" }
New-Item -ItemType Directory -Path $smokeDir | Out-Null
python .\runtime.py --journal "$smokeDir\paper_broker.jsonl" --status "$smokeDir\runtime_status.json" --candle-interval-seconds 60 --market-stale-after-seconds 3 --max-reconnect-attempts 1
```

Leave it running for about 90 seconds, then stop with Ctrl+C in the same
attached PowerShell terminal. A redirected or detached Windows subprocess may
not have a console that can deliver Ctrl+C; automated checks should signal the
runtime's stop event instead. In another terminal, start the read-only local
dashboard:

```powershell
python -m streamlit run .\dashboard.py --server.address 127.0.0.1
```

In the dashboard sidebar, set **Broker journal** and **Runtime status** to the
two files in `$smokeDir`. Check that the paper runtime heartbeat is fresh, the
quote feed is connected and healthy with a recent update, the candle interval
is `1m`, the candle connection is healthy, and the completed-candle count and
warm-up remaining are displayed. Entry permission should remain false until
all existing freshness, feed, persistence, risk, and 21-contiguous-candle
requirements pass. Do not create a position or change the strategy to force
readiness; if public access, reseeding, or a closed candle is unavailable,
report the corresponding status and stop. Only Binance public bookTicker,
kline WebSocket, and unauthenticated public kline REST endpoints are used.
No API keys, private account endpoints, order endpoints, or real orders are
part of this smoke check.

Ticker samples must accumulate into completed bars; the default strategies
need at least 21 contiguous completed bars at the selected runtime interval
before an entry can be considered. The runtime defaults to `1m`; for example,
use `--candle-interval-seconds 300` for `5m`. The interval must be one of the
supported values above.
These locally sampled quote bars, simplified fills, fees, slippage, and risk
assumptions are research approximations, not verified Binance candle data or
actual fills. A market-data outage does not produce an exit fill from a stale
quote. A triggered exit without a fresh executable quote remains explicitly
pending and unresolved until fresh market data returns; no maximum loss is
guaranteed during an outage. The runtime is paper-only and does not
demonstrate profitability.

No strategy profitability is claimed. Later stages require separate approval
before implementation.
