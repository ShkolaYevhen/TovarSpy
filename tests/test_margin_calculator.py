import unittest

from config import MarketplaceSignal, Settings, TelegramDropOffer
from margin_calculator import (
    calculate_cofounder_economics,
    calculate_profitability,
    convert_to_usd,
)


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

    def test_cofounder_economics_models_and_recommendations(self) -> None:
        tg_offer = TelegramDropOffer(
            channel="drop_ua",
            post_url="https://t.me/drop_ua/1",
            title="Smart Massager",
            drop_price_uah=300.0,  # $7.20
            rrc_price_uah=1100.0,  # $26.40
        )
        signals = [
            MarketplaceSignal(
                source="allegro_pl",
                platform="allegro_pl",
                country="PL",
                query="massager",
                sold_velocity_score=80.0,
                seller_density=30,
                median_price_usd=35.0,
            )
        ]
        econ = calculate_cofounder_economics(
            retail_price_usd=35.0,
            china_wholesale_usd=4.0,
            settings=self.settings,
            ad_days_running=14,
            ad_active_sets=9,
            marketplace_signals=signals,
            best_tg_offer=tg_offer,
        )
        # UA return drag = 0.22 * 2.60 = 0.572 -> 0.57
        self.assertAlmostEqual(econ.ua_return_drag_usd, 0.57)
        self.assertGreater(econ.ua_cod_net_profit_usd, 10.0)
        self.assertGreater(econ.cross_border_net_profit_usd, 10.0)
        self.assertTrue(0.0 <= econ.demand_velocity_score <= 100.0)
        self.assertTrue(0.0 <= econ.competition_score <= 100.0)
        self.assertTrue(0.0 <= econ.opportunity_score <= 100.0)
        self.assertIn(econ.recommended_model, ("UA_COD", "CROSS_BORDER"))

        # High return-rate override lowers UA profit; low retail recommends PASS
        bad = calculate_cofounder_economics(
            retail_price_usd=9.0,
            china_wholesale_usd=5.0,
            settings=self.settings,
            ua_return_rate=0.50,
            cross_border_cpa_usd=8.0,
        )
        self.assertEqual(bad.recommended_model, "PASS")
        self.assertLess(bad.cross_border_net_profit_usd, 0.0)
        self.assertGreater(bad.ua_return_drag_usd, 1.0)


if __name__ == "__main__":
    unittest.main()
