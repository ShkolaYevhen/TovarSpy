import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock, patch

from config import AdRecord, LandingPageData, MarketplaceIntelReport, Settings, SupplierOffer
from main import create_web_server, main, run_pipeline


class TestMainPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_run_pipeline_mock_end_to_end_and_schema(self) -> None:
        schema_path = Path(__file__).resolve().parent.parent / "sample_report_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        required_keys = set(schema["required"])

        with tempfile.TemporaryDirectory() as tmp_dir:
            reports, exported = await run_pipeline(
                query="neck massager",
                countries=["UA", "PL", "US"],
                output_dir=tmp_dir,
                mock=True,
                send_alerts=True,
                settings=Settings(_env_file=None, DB_PATH=str(Path(tmp_dir) / "test.db")),
            )
            self.assertGreaterEqual(len(reports), 2)
            self.assertTrue(any(r.margin.is_winner for r in reports))
            self.assertTrue(any(not r.margin.is_winner for r in reports))

            csv_path = Path(exported["csv"])
            json_path = Path(exported["json"])
            self.assertTrue(csv_path.exists())
            self.assertTrue(json_path.exists())

            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(len(data), len(reports))
            for item in data:
                self.assertTrue(required_keys.issubset(item.keys()))

    async def test_run_pipeline_live_wiring_with_mocks(self) -> None:
        fake_ad = AdRecord(
            ad_id="live-1",
            page_name="LiveShop",
            landing_url="https://liveshop.example/item",
            days_running=10,
            ad_creative_image_url="https://liveshop.example/ad.jpg",
            active_ad_sets=6,
            countries=["DE"],
        )
        fake_landing = LandingPageData(
            url="https://liveshop.example/item",
            final_url="https://liveshop.example/item",
            title="Live Test Gadget",
            price=40.0,
            currency="EUR",
            image_url="https://liveshop.example/prod.jpg",
            platform="Shopify",
        )
        fake_supplier = SupplierOffer(
            supplier_name="Live Factory",
            product_title="Gadget",
            product_url="https://detail.1688.com/offer/1.html",
            wholesale_price_rmb=30.0,
            wholesale_price_usd=4.20,
            rating=4.8,
            years_on_1688=4,
            moq=5,
        )

        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            patch("main.collect_winning_ads", new=AsyncMock(return_value=[fake_ad])),
            patch("main.parse_landings", new=AsyncMock(return_value=[fake_landing])),
            patch("main.match_1688_suppliers", new=AsyncMock(return_value=[fake_supplier])),
            patch(
                "main.find_zero_cost_suppliers",
                new=AsyncMock(
                    return_value={
                        "china_offers": [fake_supplier],
                        "ua_drop_offers": [],
                        "best_china_offer": fake_supplier,
                        "best_ua_drop_offer": None,
                    }
                ),
            ),
            patch(
                "main.analyze_marketplace_intel",
                new=AsyncMock(return_value=MarketplaceIntelReport(query="Live Test Gadget")),
            ),
            patch("main.send_telegram_alert", new=AsyncMock(return_value=True)) as mock_alert,
        ):
            reports, _ = await run_pipeline(
                query="gadget",
                output_dir=tmp_dir,
                mock=False,
                send_alerts=True,
                settings=Settings(_env_file=None, DB_PATH=str(Path(tmp_dir) / "test.db")),
            )

        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0].margin.is_winner)
        mock_alert.assert_awaited_once()

    def test_web_server_and_cli_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            exit_code = main(
                ["--query", "neck massager", "--countries", "UA,PL,US", "--mock", "--output-dir", tmp_dir]
            )
            self.assertEqual(exit_code, 0)

            json_path = Path(tmp_dir) / "spy_report.json"
            server = create_web_server(json_path, host="127.0.0.1", port=0)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
                    self.assertEqual(resp.status, 200)
                    html_body = resp.read().decode("utf-8")
                    self.assertIn("TovarSpy Analyzed Offers", html_body)

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/report") as resp:
                    self.assertEqual(resp.status, 200)
                    api_payload = json.loads(resp.read().decode("utf-8"))
                    self.assertIsInstance(api_payload, list)
                    self.assertGreaterEqual(len(api_payload), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
