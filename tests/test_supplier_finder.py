import json
import tempfile
import unittest
from pathlib import Path

import httpx

from config import Settings
from marketplace_intel import fetch_ua_retail_benchmark, parse_hotline_items, parse_prom_wholesale_tiers
from sourcing_engine import find_verified_ua_wholesale


class TestStage5SupplierFinder(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            _env_file=None,
            DB_PATH=str(Path(self.tmp.name) / "s5.db"),
            DEFAULT_TG_CHANNELS=["ua_drop_real"],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_prom_wholesale_tiers_rejects_empty_wholesale_prices(self) -> None:
        apollo = {
            "Product:1": {
                "id": "1",
                "name": "Масажер для шиї Smart EMS (Звичайний роздріб)",
                "wholesalePrices": [],
                "company": {"name": "RetailShop"},
                "model": {"urlText": "massager-retail", "images400x400": ["https://img/r.jpg"]},
            },
            "Product:2": {
                "id": "2",
                "name": "Масажер для шиї Smart EMS опт",
                "wholesalePrices": [{"id": "w1", "price": "310", "minimumOrderQuantity": 5}],
                "company": {"name": "Odesa Opt Direct"},
                "model": {"urlText": "massager-opt", "images400x400": ["https://img/w.jpg"]},
            },
        }
        html = f"<html><script>window.__APOLLO_STATE__ = {json.dumps(apollo)};</script></html>"
        items = parse_prom_wholesale_tiers(html, "масажер для шиї")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["seller_name"], "Odesa Opt Direct")
        self.assertAlmostEqual(items[0]["price"], 310.0)
        self.assertEqual(items[0]["moq"], 5)

    def test_parse_hotline_items(self) -> None:
        html = """
        <div class="list-item list-item--column">
          <a href="/ua/bt-elektromassazhery/medisana-nm-870/">Medisana NM 870</a>
          <span>849 – 1 490 ₴</span>
          <span>Всі пропозиції (8)</span>
          <img src="/img/tx/590/nm870.jpg" alt="Medisana NM 870" />
        </div>
        """
        items = parse_hotline_items(html, "medisana nm 870")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "hotline_ua")
        self.assertAlmostEqual(items[0]["price"], 849.0)
        self.assertEqual(items[0]["offer_count"], 8)

    async def test_retail_benchmark_and_verified_wholesale_and_honest_empty(self) -> None:
        prom_ws_json = {
            "P1": {
                "id": "10",
                "name": "Кронштейн для масажера металевий",
                "wholesalePrices": [{"id": "w9", "price": "190", "minimumOrderQuantity": 3}],
                "company": {"name": "Parts"},
                "model": {"urlText": "parts", "images400x400": []},
            },
            "P2": {
                "id": "20",
                "name": "Масажер для шиї Smart EMS",
                "wholesalePrices": [{"id": "w1", "price": "320", "minimumOrderQuantity": 3}],
                "company": {"name": "TrueOpt UA"},
                "model": {"urlText": "ems-opt", "images400x400": []},
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if "hotline.ua" in host:
                return httpx.Response(
                    200,
                    text='<div class="list-item"><a href="/ua/ems-massager/">Масажер для шиї Smart EMS</a><span>890 ₴</span><span>5 пропозицій</span></div>',
                )
            if "prom.ua" in host:
                if "wholesale" in str(request.url):
                    return httpx.Response(200, text=f"<script>{json.dumps(prom_ws_json)}</script>")
                return httpx.Response(
                    200,
                    text='<div data-qaid="product_block"><a href="/ua/p1-ems.html"><span data-qaid="product_name">Масажер для шиї Smart EMS</span></a><div data-qaid="product_price" data-qaprice="950">950 ₴</div></div>',
                )
            if "olx.ua" in host:
                return httpx.Response(
                    200,
                    text='<div data-cy="l-card"><a href="/d/1.html"><h4>Чохол для телефона</h4></a><p data-testid="ad-price">150 грн</p></div>',
                )
            if "t.me" in host:
                return httpx.Response(
                    200,
                    text='<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="ua_drop_real/9"><div class="tgme_widget_message_text">Масажер для шиї Smart EMS\nДроп: 295 грн\nРРЦ: 950 грн</div></div></div>',
                )
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            retail = await fetch_ua_retail_benchmark(
                "масажер шиї smart ems", "Масажер для шиї Smart EMS", 990.0, self.settings, client=client
            )
            ws = await find_verified_ua_wholesale(
                "масажер шиї smart ems", "Масажер для шиї Smart EMS", 990.0, self.settings, client=client
            )
            # Test honest empty wholesale state for unrelated product
            ws_none = await find_verified_ua_wholesale(
                "ексклюзивний телескоп Celestron", "Ексклюзивний телескоп Celestron", 4500.0, self.settings, client=client
            )

        # OLX phone case was rejected by match guard; Hotline + Prom matched cleanly
        self.assertEqual(len(retail["items"]), 2)
        self.assertAlmostEqual(retail["retail_min_uah"], 890.0)

        # Prom accessory bracket was rejected; TrueOpt + TG Drop matched cleanly
        self.assertEqual(ws["wholesale_match_status"], "verified")
        self.assertEqual(len(ws["offers"]), 2)
        self.assertAlmostEqual(ws["offers"][0].wholesale_price_uah or 0.0, 295.0)
        self.assertTrue(all(o.match_score >= 48.0 for o in ws["offers"]))

        # Unrelated product gets honest "none" status (never fabricated!)
        self.assertEqual(ws_none["wholesale_match_status"], "none")
        self.assertEqual(ws_none["offers"], [])
        self.assertIn("No verified wholesale", ws_none["wholesale_match_note"])


if __name__ == "__main__":
    unittest.main()
