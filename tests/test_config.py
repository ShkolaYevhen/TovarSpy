import json
import unittest
from pathlib import Path

from config import (
    AdRecord,
    AnalyzedProductReport,
    DEFAULT_FX_RATES_TO_USD,
    LandingPageData,
    MarginResult,
    Settings,
    SupplierOffer,
    get_settings,
)


class TestConfigAndModels(unittest.TestCase):
    def test_settings_defaults(self) -> None:
        settings = Settings(_env_file=None)
        self.assertEqual(settings.TARGET_COUNTRIES, ["UA", "PL", "RO", "CZ", "DE", "US"])
        self.assertEqual(settings.MIN_DURATION_DAYS, 3)
        self.assertEqual(settings.MIN_ACTIVE_AD_SETS, 5)
        self.assertAlmostEqual(settings.SHIPPING_COST_EST_USD, 2.50)
        self.assertAlmostEqual(settings.EST_CPA_USD, 6.00)
        self.assertAlmostEqual(settings.WINNER_NET_MARGIN_USD, 10.00)
        self.assertEqual(settings.FX_RATES_TO_USD, DEFAULT_FX_RATES_TO_USD)
        self.assertIsInstance(get_settings(), Settings)

    def test_fx_rates_present(self) -> None:
        for currency in ("USD", "EUR", "PLN", "UAH", "RMB", "CNY"):
            self.assertIn(currency, DEFAULT_FX_RATES_TO_USD)
            self.assertGreater(DEFAULT_FX_RATES_TO_USD[currency], 0)

    def test_sample_report_schema_matches_model(self) -> None:
        schema_path = Path(__file__).resolve().parent.parent / "sample_report_schema.json"
        self.assertTrue(schema_path.exists(), "sample_report_schema.json must exist")
        on_disk = json.loads(schema_path.read_text(encoding="utf-8"))
        expected_schema = AnalyzedProductReport.model_json_schema()
        self.assertEqual(on_disk, expected_schema)

    def test_model_roundtrip(self) -> None:
        report = AnalyzedProductReport(
            ad=AdRecord(
                ad_id="123456",
                page_name="GadgetShop",
                landing_url="https://example.com/product",
                days_running=12,
                ad_creative_image_url="https://example.com/ad.jpg",
                active_ad_sets=7,
                countries=["PL", "DE"],
            ),
            landing=LandingPageData(
                url="https://example.com/product",
                title="Smart Neck Massager",
                price=149.0,
                currency="PLN",
                image_url="https://example.com/product.jpg",
                platform="Shopify",
            ),
            suppliers=[
                SupplierOffer(
                    supplier_name="Shenzhen Tech Co.",
                    product_title="Neck Massager EMS",
                    product_url="https://detail.1688.com/offer/1.html",
                    wholesale_price_rmb=35.0,
                    wholesale_price_usd=4.90,
                    rating=4.8,
                    years_on_1688=4,
                    moq=10,
                )
            ],
            margin=MarginResult(
                retail_price_usd=37.25,
                china_wholesale_price_usd=4.90,
                net_margin_usd=23.85,
                profitability_index=1.78,
                is_winner=True,
            ),
        )
        dumped = report.model_dump(mode="json")
        restored = AnalyzedProductReport.model_validate(dumped)
        self.assertEqual(restored.ad.ad_id, "123456")
        self.assertTrue(restored.margin.is_winner)


if __name__ == "__main__":
    unittest.main()
