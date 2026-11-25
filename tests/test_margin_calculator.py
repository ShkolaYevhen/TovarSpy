import unittest

from config import Settings
from margin_calculator import calculate_profitability, convert_to_usd


class TestMarginCalculator(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(_env_file=None)

    def test_convert_to_usd_supported_currencies(self) -> None:
        self.assertAlmostEqual(convert_to_usd(100.0, "USD", self.settings), 100.0)
        self.assertAlmostEqual(convert_to_usd(100.0, "EUR", self.settings), 108.0)
        self.assertAlmostEqual(convert_to_usd(100.0, "PLN", self.settings), 25.0)
        self.assertAlmostEqual(convert_to_usd(1000.0, "UAH", self.settings), 24.0)
        self.assertAlmostEqual(convert_to_usd(100.0, "RMB", self.settings), 14.0)
        self.assertAlmostEqual(convert_to_usd(100.0, "CNY", self.settings), 14.0)

    def test_convert_to_usd_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            convert_to_usd(10.0, "JPY", self.settings)
        with self.assertRaises(ValueError):
            convert_to_usd(-5.0, "USD", self.settings)

    def test_calculate_profitability_winner_boundaries(self) -> None:
        # 22.00 - (3.50 + 2.50 + 6.00) = 10.00 -> winner
        exact = calculate_profitability(22.00, "USD", 3.50, self.settings)
        self.assertAlmostEqual(exact.net_margin_usd, 10.00)
        self.assertTrue(exact.is_winner)
        self.assertGreater(exact.profitability_index, 0)

        # 21.99 - (3.50 + 2.50 + 6.00) = 9.99 -> not winner
        just_below = calculate_profitability(21.99, "USD", 3.50, self.settings)
        self.assertAlmostEqual(just_below.net_margin_usd, 9.99)
        self.assertFalse(just_below.is_winner)

        # Negative margin
        loss = calculate_profitability(10.00, "USD", 5.00, self.settings)
        self.assertAlmostEqual(loss.net_margin_usd, -3.50)
        self.assertFalse(loss.is_winner)
        self.assertLess(loss.profitability_index, 0)


if __name__ == "__main__":
    unittest.main()
