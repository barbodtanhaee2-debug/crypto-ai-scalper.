"""Configuration for the paper-only Binance Spot BTC scalper."""

PAPER_TRADING_ONLY = True
LIVE_EXECUTION_ENABLED = False

# Starting simulation units are USDT; no EUR conversion is applied.
STARTING_BALANCE_AMOUNT = 100.0
STARTING_BALANCE_CURRENCY = "USDT"

LOG_LEVEL = "INFO"
