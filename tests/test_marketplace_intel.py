import tempfile
import unittest
from pathlib import Path

import httpx

from config import Settings
from marketplace_intel import (
    analyze_marketplace_intel,
    parse_allegro_html,
    parse_amazon_search_html,
    parse_olx_html,
    parse_prom_or_rozetka_html,
)


class TestMarketplaceIntel(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(
            _env_file=None,
            DB_PATH=str(Path(self.tmp_dir.name) / "mkt.db"),
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_parse_allegro_and_blocked(self) -> None:
        html = """
        <html><body>
          <div>150 ofert</div>
          <article>
            <a href="/oferta/masazer-karku-123">Masażer karku</a>
            <span>119,00 zł</span><span>45 osób kupiło</span>
          </article>
          <article>
            <span>149,00 zł</span><span>85 osób kupiło</span>
          </article>
          <article>
            <span>179,00 zł</span>
          </article>
        </body></html>
        """
        sig = parse_allegro_html(html, "massager")
        self.assertEqual(sig.status, "live")
        self.assertEqual(sig.currency, "PLN")
        self.assertEqual(sig.listing_count, 150)
        self.assertEqual(sig.sold_count, 130)
        self.assertAlmostEqual(sig.median_price, 149.0)
        self.assertAlmostEqual(sig.min_price, 119.0)
        self.assertAlmostEqual(sig.max_price, 179.0)
        self.assertEqual(sig.top_sample_url, "https://allegro.pl/oferta/masazer-karku-123")

        blocked = parse_allegro_html("<html><body>datadome captcha</body></html>", "massager")
        self.assertEqual(blocked.status, "blocked")
        self.assertTrue(blocked.blocked)

    def test_parse_olx_ua_and_pl(self) -> None:
        ua_html = """
        <html><body>
          <div data-testid="total-count">Знайдено 64 оголошень</div>
          <div data-cy="l-card"><a href="/d/uk/obyavlenie/lampa-1.html">Lampa</a><span>750 грн</span></div>
          <div data-cy="l-card"><span>950 грн</span></div>
          <div data-cy="l-card"><span>1 150 грн</span></div>
        </body></html>
        """
        ua_sig = parse_olx_html(ua_html, "lamp", domain="ua")
        self.assertEqual(ua_sig.platform, "olx_ua")
        self.assertEqual(ua_sig.listing_count, 64)
        self.assertAlmostEqual(ua_sig.median_price, 950.0)
        self.assertEqual(ua_sig.top_sample_url, "https://www.olx.ua/d/uk/obyavlenie/lampa-1.html")

        pl_html = """
        <html><body>
          <div data-testid="total-count">Znaleźliśmy 22 ogłoszeń</div>
          <div data-cy="l-card"><span>100 zł</span></div>
          <div data-cy="l-card"><span>140 zł</span></div>
        </body></html>
        """
        pl_sig = parse_olx_html(pl_html, "lamp", domain="pl")
        self.assertEqual(pl_sig.platform, "olx_pl")
        self.assertEqual(pl_sig.currency, "PLN")
        self.assertAlmostEqual(pl_sig.median_price, 120.0)

    def test_parse_prom_rozetka_and_amazon(self) -> None:
        prom_html = """
        <html><body>
          <div><span data-qaid="company_name">Shop A</span><span>800 ₴</span><span>30 відгуків</span></div>
          <div><span data-qaid="company_name">Shop B</span><span>1 000 грн</span><span>45 замовлень</span></div>
        </body></html>
        """
        prom_sig = parse_prom_or_rozetka_html(prom_html, "lamp", platform="prom_ua")
        self.assertEqual(prom_sig.status, "live")
        self.assertEqual(prom_sig.seller_density, 2)
        self.assertAlmostEqual(prom_sig.median_price, 900.0)
        self.assertEqual(prom_sig.sold_count, 75)

        amz_html = """
        <html><body>
          <div data-component-type="s-search-result">
            <a href="/dp/B0012345">Item</a>
            <span class="a-price"><span class="a-offscreen">$29.99</span></span>
            <span>500+ bought in past month</span>
            <span class="a-size-base s-underline-text">1,240</span>
          </div>
          <div data-component-type="s-search-result">
            <span class="a-price"><span class="a-offscreen">$39.99</span></span>
            <span>1K+ bought in past month</span>
          </div>
        </body></html>
        """
        amz_sig = parse_amazon_search_html(amz_html, "lamp")
        self.assertEqual(amz_sig.status, "live")
        self.assertEqual(amz_sig.sold_count, 1500)
        self.assertAlmostEqual(amz_sig.median_price, 34.99)
        self.assertEqual(amz_sig.top_sample_url, "https://www.amazon.com/dp/B0012345")

        amz_blocked = parse_amazon_search_html("<html>validateCaptcha</html>", "lamp")
        self.assertEqual(amz_blocked.status, "blocked")

    async def test_analyze_marketplace_intel_live_and_cached(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            host = request.url.host
            if "allegro" in host:
                return httpx.Response(200, text="<div>20 ofert</div><span>120 zł</span><span>50 osób kupiło</span>")
            if "olx.ua" in host:
                return httpx.Response(200, text='<div data-cy="l-card"><span>800 грн</span></div>')
            if "amazon" in host:
                return httpx.Response(403, text="blocked")
            return httpx.Response(200, text="<span>900 грн</span>")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await analyze_marketplace_intel(
                "massager",
                self.settings,
                platforms=["allegro_pl", "olx_ua", "amazon_us"],
                client=client,
            )
            second = await analyze_marketplace_intel(
                "massager",
                self.settings,
                platforms=["allegro_pl", "olx_ua", "amazon_us"],
                client=client,
            )

        self.assertAlmostEqual(first.pl_median_price_pln or 0.0, 120.0)
        self.assertAlmostEqual(first.ua_median_price_uah or 0.0, 800.0)
        self.assertGreater(first.demand_velocity_score, 0.0)
        amz = next(s for s in first.signals if s.platform == "amazon_us")
        self.assertEqual(amz.status, "blocked")

        # allegro_pl and olx_ua cached on second call; only uncached amazon_us re-fetched
        allegro_second = next(s for s in second.signals if s.platform == "allegro_pl")
        self.assertEqual(allegro_second.status, "cached")
        self.assertEqual(calls, 4)  # 3 on first call + 1 uncached blocked on second


if __name__ == "__main__":
    unittest.main()
