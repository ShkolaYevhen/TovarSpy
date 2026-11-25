import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd

from config import (
    AdRecord,
    AnalyzedProductReport,
    LandingPageData,
    MarginResult,
    Settings,
    SupplierOffer,
)
from notifier import export_reports, format_winner_alert, send_telegram_alert


def _make_report(is_winner: bool = True) -> AnalyzedProductReport:
    supplier = SupplierOffer(
        supplier_name="Yiwu BestSource",
        product_title="EMS Neck Massager",
        product_url="https://detail.1688.com/offer/810001.html",
        wholesale_price_rmb=24.0,
        wholesale_price_usd=3.36,
        rating=4.9,
        years_on_1688=5,
        moq=5,
    )
    return AnalyzedProductReport(
        ad=AdRecord(
            ad_id="ad-101",
            page_name="SmartRelax PL",
            landing_url="https://smartrelax-shop.pl/neck-massager",
            days_running=14,
            ad_creative_image_url="https://cdn.example.com/ad.jpg",
            active_ad_sets=9,
            countries=["PL", "DE"],
            source="meta",
        ),
        landing=LandingPageData(
            url="https://smartrelax-shop.pl/neck-massager",
            final_url="https://smartrelax-shop.pl/neck-massager",
            title="EMS Smart Neck Massager Pro",
            price=149.0,
            currency="PLN",
            image_url="https://cdn.example.com/product.jpg",
            platform="Shopify",
        ),
        suppliers=[supplier],
        best_supplier=supplier,
        margin=MarginResult(
            retail_price_usd=37.25,
            china_wholesale_price_usd=3.36,
            shipping_cost_est_usd=2.50,
            est_cpa_usd=6.00,
            net_margin_usd=25.39 if is_winner else 5.00,
            profitability_index=2.14 if is_winner else 0.42,
            is_winner=is_winner,
        ),
    )


class TestNotifier(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="12345:fake-token",
            TELEGRAM_CHAT_ID="-100123456",
        )

    def test_format_winner_alert_contains_required_fields(self) -> None:
        msg = format_winner_alert(_make_report(is_winner=True))
        self.assertIn("EMS Smart Neck Massager Pro", msg)
        self.assertIn("Shopify", msg)
        self.assertIn("PL, DE", msg)
        self.assertIn("149.00 PLN ($37.25)", msg)
        self.assertIn("¥24.00 RMB ($3.36)", msg)
        self.assertIn("$25.39", msg)
        self.assertIn("IS_WINNER: True", msg)
        self.assertIn("https://detail.1688.com/offer/810001.html", msg)
        self.assertIn("https://smartrelax-shop.pl/neck-massager", msg)

    async def test_send_telegram_alert_flows(self) -> None:
        loser = _make_report(is_winner=False)
        winner = _make_report(is_winner=True)

        self.assertFalse(await send_telegram_alert(loser, self.settings, mock=True))
        self.assertTrue(await send_telegram_alert(winner, self.settings, mock=True))

        empty_settings = Settings(_env_file=None)
        self.assertFalse(await send_telegram_alert(winner, empty_settings, mock=False))

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        with patch("notifier.telegram.Bot", return_value=mock_bot) as bot_cls:
            ok = await send_telegram_alert(winner, self.settings, mock=False)
            self.assertTrue(ok)
            bot_cls.assert_called_once_with(token="12345:fake-token")
            mock_bot.send_message.assert_awaited_once()

        failing_bot = MagicMock()
        failing_bot.send_message = AsyncMock(side_effect=RuntimeError("network timeout"))
        with patch("notifier.telegram.Bot", return_value=failing_bot):
            self.assertFalse(await send_telegram_alert(winner, self.settings, mock=False))

    def test_export_reports_csv_and_json(self) -> None:
        report = _make_report(is_winner=True)
        with tempfile.TemporaryDirectory() as tmp_dir:
            paths = export_reports([report], tmp_dir, base_name="test_spy")
            csv_file = Path(paths["csv"])
            json_file = Path(paths["json"])
            self.assertTrue(csv_file.exists())
            self.assertTrue(json_file.exists())

            df = pd.read_csv(csv_file)
            self.assertEqual(len(df), 1)
            self.assertEqual(df.loc[0, "ad_id"], "ad-101")
            self.assertTrue(bool(df.loc[0, "is_winner"]))
            self.assertAlmostEqual(float(df.loc[0, "net_margin_usd"]), 25.39)

            raw_json = json.loads(json_file.read_text(encoding="utf-8"))
            self.assertEqual(len(raw_json), 1)
            validated = AnalyzedProductReport.model_validate(raw_json[0])
            self.assertEqual(validated.ad.ad_id, "ad-101")


if __name__ == "__main__":
    unittest.main()
