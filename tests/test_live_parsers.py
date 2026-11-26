import tempfile
import unittest
from pathlib import Path

import httpx

from config import Settings
from marketplace_intel import (
    fetch_live_marketplace_products,
    parse_olx_items,
    parse_prom_items,
)
from sourcing_engine import (
    fetch_aliexpress_live,
    fetch_google_trends_rss,
    parse_aliexpress_html,
    parse_google_trends_rss,
)


class TestLiveParsers(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(
            _env_file=None,
            DB_PATH=str(Path(self.tmp_dir.name) / "live.db"),
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_parse_aliexpress_init_data_content_shape(self) -> None:
        html = """
        <html><script>
        window._init_data_ = {"data":{"data":{"itemList":{"content":[
          {
            "productId": "1005011584364858",
            "title": {"displayTitle": "Electric Neck Massager Pro"},
            "prices": {"salePrice": {"currencyCode": "USD", "minPrice": 9.50}},
            "image": {"imgUrl": "//ae-pic-a1.aliexpress-media.com/kf/massager.jpg"},
            "trade": {"tradeDesc": "10,000+ sold"},
            "evaluation": {"starRating": 4.9},
            "store": {"storeName": "SmartWellness Official"}
          }
        ]}}}};
        </script></html>
        """
        offers = parse_aliexpress_html(html, "neck massager")
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].product_title, "Electric Neck Massager Pro")
        self.assertAlmostEqual(offers[0].wholesale_price_usd, 9.50)
        self.assertEqual(offers[0].image_url, "https://ae-pic-a1.aliexpress-media.com/kf/massager.jpg")
        self.assertEqual(offers[0].product_url, "https://www.aliexpress.com/item/1005011584364858.html")
        self.assertEqual(offers[0].sales_count_text, "10,000+ sold")
        self.assertAlmostEqual(offers[0].rating or 0.0, 4.9)
        self.assertTrue(offers[0].is_live)

    def test_parse_prom_and_olx_items_and_trends_rss(self) -> None:
        prom_html = """
        <div data-qaid="product_block">
          <a href="/ua/p2766038213-massazher.html">
            <img src="https://images.prom.ua/6827076868_w200_h200_massazher.jpg" />
            <span data-qaid="product_name">Масажер для шиї EMS</span>
          </a>
          <span data-qaid="product_price">від 1 500 ₴</span>
          <span data-qaid="company_name">GadgetUA</span>
        </div>
        """
        prom_items = parse_prom_items(prom_html, "massager")
        self.assertEqual(len(prom_items), 1)
        self.assertEqual(prom_items[0]["title"], "Масажер для шиї EMS")
        self.assertAlmostEqual(prom_items[0]["price"], 1500.0)
        self.assertEqual(prom_items[0]["image_url"], "https://images.prom.ua/6827076868_w200_h200_massazher.jpg")
        self.assertEqual(prom_items[0]["product_url"], "https://prom.ua/ua/p2766038213-massazher.html")

        olx_html = """
        <div data-cy="l-card">
          <a href="/d/uk/obyavlenie/masazher-1.html">
            <img data-src="https://ireland.apollo.olxcdn.com/v1/files/abc/image" />
            <h4>Масажер для спини</h4>
          </a>
          <p data-testid="ad-price">1 200 грн.</p>
        </div>
        """
        olx_items = parse_olx_items(olx_html, "massager", domain="ua")
        self.assertEqual(len(olx_items), 1)
        self.assertAlmostEqual(olx_items[0]["price"], 1200.0)
        self.assertEqual(olx_items[0]["image_url"], "https://ireland.apollo.olxcdn.com/v1/files/abc/image")
        self.assertEqual(olx_items[0]["product_url"], "https://www.olx.ua/d/uk/obyavlenie/masazher-1.html")

        rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0" xmlns:ht="https://trends.google.com/trending/rss">
          <channel>
            <item><title>Sunset Lamp</title><ht:approx_traffic>5000+</ht:approx_traffic></item>
            <item><title>Neck Massager</title><ht:approx_traffic>2000+</ht:approx_traffic></item>
          </channel>
        </rss>
        """
        trends = parse_google_trends_rss(rss_xml, geo="UA")
        self.assertEqual(len(trends), 2)
        self.assertEqual(trends[0]["title"], "Sunset Lamp")
        self.assertEqual(trends[0]["traffic"], "5000+")

    async def test_live_fetchers_and_diagnostics_with_mock_transport(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if "prom.ua" in host:
                return httpx.Response(
                    200,
                    text='<div data-qaid="product_block"><a href="/ua/p1.html"><img src="https://images.prom.ua/1.jpg"/><span data-qaid="product_name">Lamp</span></a><span data-qaid="product_price">890 ₴</span></div>',
                )
            if "olx.ua" in host:
                return httpx.Response(
                    200,
                    text='<div data-cy="l-card"><a href="/d/1.html"><img src="https://olxcdn.com/1.jpg"/><h4>Lamp</h4></a><p data-testid="ad-price">750 грн</p></div>',
                )
            if "olx.pl" in host:
                return httpx.Response(403, text="blocked")
            if "aliexpress" in host:
                return httpx.Response(
                    200,
                    text='{"itemList":{"content":[{"productId":"99","title":{"displayTitle":"Lamp AE"},"prices":{"salePrice":{"currencyCode":"USD","minPrice":4.5}},"image":{"imgUrl":"//ae.com/1.jpg"}}]}}',
                )
            if "trends.google.com" in host:
                return httpx.Response(200, text='<rss><channel><item><title>LED Lamp</title></item></channel></rss>')
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            items, diag = await fetch_live_marketplace_products("lamp", self.settings, client=client)
            ae = await fetch_aliexpress_live("lamp", self.settings, client=client)
            tr = await fetch_google_trends_rss("UA", self.settings, client=client)

        self.assertEqual(len(items), 2)
        self.assertTrue(diag["prom_ua"].startswith("live:"))
        self.assertTrue(diag["olx_ua"].startswith("live:"))
        self.assertTrue(diag["olx_pl"].startswith("blocked:"))
        self.assertEqual(len(ae), 1)
        self.assertEqual(ae[0].product_url, "https://www.aliexpress.com/item/99.html")
        self.assertEqual(tr[0]["title"], "LED Lamp")


if __name__ == "__main__":
    unittest.main()
