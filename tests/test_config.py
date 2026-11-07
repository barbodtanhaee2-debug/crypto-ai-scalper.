import unittest

from config import (
    LIVE_EXECUTION_ENABLED,
    PAPER_TRADING_ONLY,
    STARTING_BALANCE_AMOUNT,
    STARTING_BALANCE_CURRENCY,
)


class ConfigurationSafetyTests(unittest.TestCase):
    def test_paper_trading_is_the_default_mode(self):
        self.assertIs(PAPER_TRADING_ONLY, True)

    def test_starting_balance_is_100_usdt_without_fx_conversion(self):
        self.assertEqual(STARTING_BALANCE_AMOUNT, 100.0)
        self.assertEqual(STARTING_BALANCE_CURRENCY, "USDT")

    def test_live_execution_is_disabled(self):
        self.assertIs(LIVE_EXECUTION_ENABLED, False)


if __name__ == "__main__":
    unittest.main()
