import tempfile
import unittest
from pathlib import Path

import httpx

from config import Settings
from main import run_super_parse
from margin_calculator import calculate_cofounder_economics
from marketplace_intel import (
    fetch_ua_wholesale_offers,
    parse_bigl_items,
    parse_prom_items,
)
from sourcing_engine import find_zero_cost_suppliers


class TestUaWholesaleAndMargins(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(
            _env_file=None,
            DB_PATH=str(Path(self.tmp_dir.name) / "ua.db"),
            SKIP_CHINA_SOURCING=True,
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_prom_installment_exclusion_and_data_qaprice(self) -> None:
        html = """
        <div data-qaid="product_block">
          <a href="/ua/p100-organaizer.html"><span data-qaid="product_name">Органайзер Опт</span></a>
          <div data-qaid="product_price" data-qaprice="240">від 25 ₴ /міс 240 ₴</div>
          <img src="https://images.prom.ua/org.jpg" />
        </div>
        <div data-qaid="product_block">
          <a href="/ua/p101-massager.html"><span data-qaid="product_name">Масажер Pro</span></a>
          <div data-qaid="product_price">від 73 ₴ /міс 480 ₴</div>
          <img src="https://images.prom.ua/mas.jpg" />
        </div>
        """
        items = parse_prom_items(html, "органайзер")
        self.assertEqual(len(items), 2)
        self.assertAlmostEqual(items[0]["price"], 240.0)
        self.assertAlmostEqual(items[1]["price"], 480.0)

    def test_bigl_parser_extracts_uah_items(self) -> None:
        html = """
        <div>
          <a href="/ua/p2494246-organajzer">Органайзер опт набір</a>
          <span>від 50 ₴ /міс</span>
          <span>390 ₴</span>
          <img src="https://images.prom.ua/bigl_org.jpg" />
        </div>
        """
        items = parse_bigl_items(html, "органайзер")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "bigl_ua")
        self.assertAlmostEqual(items[0]["price"], 390.0)
        self.assertEqual(items[0]["image_url"], "https://images.prom.ua/bigl_org.jpg")

    async def test_skip_china_bypasses_1688_aliexpress_alibaba_and_parses_ua_wholesale(self) -> None:
        visited_hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            visited_hosts.append(request.url.host)
            host = request.url.host
            if "prom.ua" in host:
                is_ws = "wholesale" in str(request.url)
                price = "320" if is_ws else "980"
                return httpx.Response(
                    200,
                    text=f'<div data-qaid="product_block"><a href="/ua/p11.html"><img src="https://images.prom.ua/p11.jpg"/><span data-qaid="product_name">Кухонний набір</span></a><div data-qaid="product_price" data-qaprice="{price}">{price} ₴</div></div>',
                )
            if "olx.ua" in host:
                is_opt = "опт" in str(request.url) or "%D0%BE%D0%BF%D1%82" in str(request.url)
                price = "350 грн" if is_opt else "890 грн"
                return httpx.Response(
                    200,
                    text=f'<div data-cy="l-card"><a href="/d/test.html"><img src="https://olxcdn.com/t.jpg"/><h4>Кухонний набір OLX</h4></a><p data-testid="ad-price">{price}</p></div>',
                )
            if "bigl.ua" in host:
                return httpx.Response(
                    200,
                    text='<div><a href="/ua/p777-item">Набір для кухні Bigl</a><span>410 ₴</span><img src="https://images.prom.ua/b.jpg"/></div>',
                )
            if "trends.google.com" in host:
                return httpx.Response(200, text="<rss><channel><item><title>Органайзер</title></item></channel></rss>")
            return httpx.Response(500, text="Unexpected external host")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ws_offers, ws_diag = await fetch_ua_wholesale_offers("кухонний набір", self.settings, client=client)
            z_res = await find_zero_cost_suppliers("кухонний набір", None, self.settings, client=client)
            sp_res = await run_super_parse(
                categories=["home_kitchen"],
                output_dir=self.tmp_dir.name,
                settings=self.settings,
                client=client,
            )

        # Verify ZERO China domains visited
        self.assertFalse(any("aliexpress" in h or "alibaba" in h or "1688" in h for h in visited_hosts))
        self.assertGreaterEqual(len(ws_offers), 2)
        self.assertTrue(ws_diag["prom_wholesale"].startswith("live:"))
        self.assertEqual(z_res["china_offers"], [])
        self.assertGreaterEqual(len(z_res["ua_wholesale_offers"]), 2)
        self.assertIn("skipped", sp_res["source_diagnostics"]["china_1688"])
        self.assertTrue(sp_res["reports"][0].margin.shipping_cost_est_usd == 0.0)

    def test_ua_local_arbitrage_margin_without_china_cargo(self) -> None:
        # UA Wholesale = 300 UAH ($7.20), UA Retail = 1100 UAH ($26.40), zero China cargo
        econ = calculate_cofounder_economics(
            retail_price_usd=26.40,
            china_wholesale_usd=0.0,
            settings=self.settings,
            ua_wholesale_uah=300.0,
            ua_retail_uah=1100.0,
            skip_china=True,
        )
        # (1 - 0.22)*(26.40 - 7.20) - (0.22*2.60) - 3.50 = 14.976 - 0.572 - 3.50 = 10.90
        self.assertAlmostEqual(econ.ua_cod_net_profit_usd, 10.90, delta=0.05)
        self.assertAlmostEqual(econ.ua_wholesale_price_uah, 300.0)
        self.assertAlmostEqual(econ.ua_retail_price_uah, 1100.0)
        self.assertGreater(econ.ua_net_profit_uah, 400.0)
        self.assertEqual(econ.cross_border_net_profit_usd, 0.0)
        self.assertEqual(econ.recommended_model, "UA_COD")


if __name__ == "__main__":
    unittest.main()
