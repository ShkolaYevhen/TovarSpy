import tempfile
import unittest
from pathlib import Path

import httpx

from config import Settings
from sourcing_engine import (
    find_zero_cost_suppliers,
    parse_alibaba_html,
    parse_aliexpress_html,
    parse_telegram_channel_html,
    search_telegram_dropshipping,
)


class TestSourcingEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(
            _env_file=None,
            DB_PATH=str(Path(self.tmp_dir.name) / "sourcing.db"),
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_parse_telegram_channel_html_formats_and_filters(self) -> None:
        html = """
        <div class="tgme_widget_message_wrap">
          <div class="tgme_widget_message" data-post="drop_ua/101">
            <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn.t.me/lamp.jpg')"></a>
            <div class="tgme_widget_message_text">
              🔥 Проекційна лампа Sunset LED<br/>
              Ціна дроп: 280 грн<br/>
              РРЦ: 890 грн<br/>
              В наявності · Замовити: @drop_manager
            </div>
          </div>
        </div>
        <div class="tgme_widget_message_wrap">
          <div class="tgme_widget_message" data-post="drop_ua/102">
            <div class="tgme_widget_message_text">
              Зимова куртка<br/>
              Опт: 1200 грн<br/>
              Немає в наявності
            </div>
          </div>
        </div>
        """
        offers = parse_telegram_channel_html(html, channel="drop_ua", query="Sunset")
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].post_url, "https://t.me/drop_ua/101")
        self.assertEqual(offers[0].image_url, "https://cdn.t.me/lamp.jpg")
        self.assertAlmostEqual(offers[0].drop_price_uah, 280.0)
        self.assertAlmostEqual(offers[0].rrc_price_uah, 890.0)
        self.assertTrue(offers[0].in_stock)
        self.assertEqual(offers[0].supplier_contact, "@drop_manager")

        all_offers = parse_telegram_channel_html(html, channel="drop_ua")
        self.assertEqual(len(all_offers), 2)
        self.assertFalse(all_offers[1].in_stock)

    def test_parse_alibaba_and_aliexpress_and_captcha(self) -> None:
        ali_html = """
        <html><body>
          <div class="fy23-search-card">
            <h2 class="search-card-e-title">EMS Neck Massager Wholesale</h2>
            <a href="//www.alibaba.com/product-detail/massager_1.html">Link</a>
            <div class="search-card-e-company">Shenzhen Best Co.</div>
            <span>$3.85</span><span>Min. order: 10 pieces</span>
          </div>
        </body></html>
        """
        ali = parse_alibaba_html(ali_html, "neck massager")
        self.assertEqual(len(ali), 1)
        self.assertEqual(ali[0].source, "alibaba")
        self.assertAlmostEqual(ali[0].wholesale_price_usd, 3.85)
        self.assertEqual(ali[0].moq, 10)
        self.assertEqual(ali[0].product_url, "https://www.alibaba.com/product-detail/massager_1.html")

        ae_html = """
        <html><script>
        var data = {"itemList":[{"productId":"777","title":"Neck Massager AE","price":"6.50","storeName":"AE Official"}]};
        </script></html>
        """
        ae = parse_aliexpress_html(ae_html, "neck massager")
        self.assertEqual(len(ae), 1)
        self.assertEqual(ae[0].source, "aliexpress")
        self.assertAlmostEqual(ae[0].wholesale_price_usd, 6.50)

        self.assertEqual(parse_alibaba_html("<html>baxia captcha punish</html>", "lamp"), [])
        self.assertEqual(parse_aliexpress_html("<html>verify you are human</html>", "lamp"), [])

    async def test_search_tg_and_find_zero_cost_suppliers_with_cache(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            host = request.url.host
            if "t.me" in host:
                return httpx.Response(
                    200,
                    text="""
                    <div class="tgme_widget_message_wrap">
                      <div class="tgme_widget_message" data-post="drop_ua/55">
                        <div class="tgme_widget_message_text">Neck Massager Pro\nДроп: 310 грн\nРРЦ: 950 грн</div>
                      </div>
                    </div>
                    """,
                )
            if "alibaba.com" in host:
                return httpx.Response(
                    200,
                    text='<div class="fy23-search-card"><a href="/product-detail/m.html">Massager</a><span>$3.40</span><span>MOQ: 5</span></div>',
                )
            return httpx.Response(403, text="blocked")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tg = await search_telegram_dropshipping("Massager", self.settings, channels=["drop_ua"], client=client)
            self.assertEqual(len(tg), 1)
            self.assertAlmostEqual(tg[0].drop_price_uah, 310.0)

            res1 = await find_zero_cost_suppliers("Neck Massager", None, self.settings, client=client)
            calls_after_first = calls
            res2 = await find_zero_cost_suppliers("Neck Massager", None, self.settings, client=client)

        self.assertIsNotNone(res1["best_china_offer"])
        self.assertIsNotNone(res1["best_ua_drop_offer"])
        self.assertEqual(res1["best_china_offer"].source, "alibaba")
        self.assertAlmostEqual(res1["best_china_offer"].wholesale_price_usd, 3.40)
        # Second call served entirely from SQLite cache
        self.assertEqual(calls, calls_after_first)
        self.assertEqual(len(res2["china_offers"]), len(res1["china_offers"]))


if __name__ == "__main__":
    unittest.main()
