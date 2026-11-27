import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from config import Settings
from main import run_super_parse
from web_app import create_app


class TestImgProxyAndSuperParse(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(
            _env_file=None,
            DB_PATH=str(Path(self.tmp_dir.name) / "sp.db"),
        )
        self.app = create_app(output_dir=Path(self.tmp_dir.name), settings=self.settings)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_img_proxy_ssrf_and_content_type_and_caching(self) -> None:
        # Reject bad scheme
        self.assertEqual(self.client.get("/api/img?url=file:///etc/passwd").status_code, 400)

        # Reject private/loopback IP (SSRF)
        with patch("web_app.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]):
            self.assertEqual(self.client.get("/api/img?url=https://evil.test/img.jpg").status_code, 403)

        with patch("web_app.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.10", 0))]):
            self.assertEqual(self.client.get("/spy/api/img?url=https://lan.test/img.jpg").status_code, 403)

        # Reject non-image / SVG content type
        def svg_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "image/svg+xml"}, content=b"<svg/>")

        with (
            patch("web_app.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]),
            patch("web_app.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=httpx.MockTransport(svg_handler))),
        ):
            self.assertEqual(self.client.get("/api/img?url=https://cdn.example.org/bad.svg").status_code, 415)

        # Serve valid JPEG & hit memory cache on second request
        fetch_count = 0

        def jpg_handler(request: httpx.Request) -> httpx.Response:
            nonlocal fetch_count
            fetch_count += 1
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8\xff\xe0TESTJPG")

        with patch("web_app.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            with patch("web_app.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=httpx.MockTransport(jpg_handler))):
                r1 = self.client.get("/spy/api/img?url=https://cdn.example.org/good.jpg")
                self.assertEqual(r1.status_code, 200)
                self.assertEqual(r1.headers["content-type"], "image/jpeg")
                self.assertIn("max-age=3600", r1.headers.get("cache-control", ""))

            # Second request served from cache without calling httpx
            r2 = self.client.get("/api/img?url=https://cdn.example.org/good.jpg")
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r2.content, b"\xff\xd8\xff\xe0TESTJPG")
            self.assertEqual(fetch_count, 1)

        # Serve JPEG sent with binary/octet-stream (Backblaze B2 / Tyver CDN pattern)
        def octet_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "binary/octet-stream"}, content=b"\xff\xd8\xff\xe0B2JPG")

        with (
            patch("web_app.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]),
            patch("web_app.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=httpx.MockTransport(octet_handler))),
        ):
            r3 = self.client.get("/spy/api/img?url=https://tyver-may26-img-1.s3.example.com/ad.jpg")
            self.assertEqual(r3.status_code, 200)
            self.assertEqual(r3.headers["content-type"], "image/jpeg")

    async def test_run_super_parse_and_endpoint_live_wiring(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if "aliexpress" in host:
                return httpx.Response(
                    200,
                    text='{"itemList":{"content":[{"productId":"555","title":{"displayTitle":"EMS Neck Massager Live"},"prices":{"salePrice":{"currencyCode":"USD","minPrice":8.2}},"image":{"imgUrl":"//ae-pic.com/m.jpg"},"trade":{"tradeDesc":"5,000+ sold"},"evaluation":{"starRating":4.8}}]}}',
                )
            if "prom.ua" in host:
                return httpx.Response(
                    200,
                    text='<div data-qaid="product_block"><a href="/ua/p555-massager.html"><img src="https://images.prom.ua/m.jpg"/><span data-qaid="product_name">Масажер EMS Live</span></a><span data-qaid="product_price">1 290 ₴</span></div>',
                )
            if "olx.ua" in host:
                return httpx.Response(
                    200,
                    text='<div data-cy="l-card"><a href="/d/m1.html"><img src="https://olxcdn.com/m.jpg"/><h4>Масажер OLX</h4></a><p data-testid="ad-price">950 грн</p></div>',
                )
            if "olx.pl" in host:
                return httpx.Response(403, text="blocked")
            if "trends.google.com" in host:
                return httpx.Response(200, text='<rss><channel><item><title>Massager Pro</title></item></channel></rss>')
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_http:
            res = await run_super_parse(
                categories=["beauty_wellness"],
                output_dir=self.tmp_dir.name,
                mock=False,
                settings=self.settings,
                client=mock_http,
            )

        self.assertGreaterEqual(len(res["reports"]), 2)
        self.assertTrue(all(r.ad.is_live for r in res["reports"]))
        self.assertIn("skipped", res["source_diagnostics"]["china_1688"])
        self.assertTrue(res["source_diagnostics"]["prom_ua"].startswith("live:"))
        self.assertEqual(res["trending_keywords"][0]["title"], "Massager Pro")
        self.assertGreaterEqual(len(res["external_blocks"]), 1)

        with patch("web_app.run_super_parse", new=AsyncMock(return_value=res)):
            resp = self.client.post("/spy/api/super-parse", json={"categories": ["beauty_wellness"]})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("source_diagnostics", body)
        self.assertIn("external_blocks", body)
        self.assertGreaterEqual(body["summary"]["live_count"], 2)

    async def test_5stage_ad_first_arbitrage_with_tyver_and_honest_wholesale(self) -> None:
        settings_tyv = Settings(
            _env_file=None,
            DB_PATH=str(Path(self.tmp_dir.name) / "adfirst.db"),
            TYVER_API_KEY="tyv_test_5stage",
            MIN_DURATION_DAYS=3,
            MIN_ACTIVE_AD_SETS=5,
            SKIP_CHINA_SOURCING=True,
        )

        # Stage 1 returns 2 products:
        # 1) "Blender USB MiniJuice" (passes 5+ Pass #2 ads + has verified Prom wholesalePrices tier)
        # 2) "Telescope Celestron" (passes 5+ Pass #2 ads + has NO wholesale -> honest "none")
        # 3) "SingleAd LowSets" (only 1 matching ad -> filtered out by Stage 3 min_ad_sets=5!)
        stage1_ads = [
            {
                "provider_id": "ad1",
                "activity_days_amount": 9,
                "bodies": ["Портативний блендер USB MiniJuice"],
                "link_titles": ["Портативний блендер USB MiniJuice"],
                "countries": ["UA"],
                "provider_page_name": "BlendShop UA",
                "target_url": "https://blendshop.com.ua/minijuice",
                "domain": "blendshop.com.ua",
                "cta": "Shop now",
            },
            {
                "provider_id": "ad2",
                "activity_days_amount": 7,
                "bodies": ["Телескоп Celestron Pro"],
                "link_titles": ["Телескоп Celestron Pro"],
                "countries": ["UA"],
                "provider_page_name": "AstroUA",
                "target_url": "https://astroua.com.ua/telescope",
                "domain": "astroua.com.ua",
                "cta": "Order now",
            },
            {
                "provider_id": "ad3",
                "activity_days_amount": 4,
                "bodies": ["Рідкісний товар Одиночка"],
                "link_titles": ["Рідкісний товар Одиночка"],
                "countries": ["UA"],
                "provider_page_name": "SoloShop",
                "target_url": "https://soloshop.com.ua/solo",
                "domain": "soloshop.com.ua",
                "cta": "Shop now",
            },
        ]

        prom_ws = {
            "P1": {
                "id": "55",
                "name": "Портативний блендер USB MiniJuice Опт",
                "wholesalePrices": [{"id": "w1", "price": "310", "minimumOrderQuantity": 3}],
                "company": {"name": "7km Opt Warehouse"},
                "model": {"urlText": "blender-opt", "images400x400": []},
            },
            "P2": {
                "id": "56",
                "name": "Кабель USB для блендера",
                "wholesalePrices": [{"id": "w2", "price": "55", "minimumOrderQuantity": 5}],
                "company": {"name": "CableShop"},
                "model": {"urlText": "cable", "images400x400": []},
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            u = str(request.url)
            host = request.url.host
            if "api.tyver.io" in host:
                if "search=" in u:
                    if "%D0%BE%D0%B4%D0%B8%D0%BD%D0%BE%D1%87%D0%BA%D0%B0" in u.lower():
                        return httpx.Response(200, json={"detail": "Success", "data": {"items": [stage1_ads[2]], "total": 1}})
                    if "celestron" in u.lower():
                        items = [{**stage1_ads[1], "provider_id": f"c{i}", "provider_page_name": f"Astro{i}"} for i in range(6)]
                        return httpx.Response(200, json={"detail": "Success", "data": {"items": items, "total": 6}})
                    items = [{**stage1_ads[0], "provider_id": f"b{i}", "provider_page_name": f"Blend{i}"} for i in range(7)]
                    return httpx.Response(200, json={"detail": "Success", "data": {"items": items, "total": 7}})
                return httpx.Response(200, json={"detail": "Success", "data": {"items": stage1_ads, "total": 3}})
            if "blendshop.com.ua" in host:
                return httpx.Response(200, text="<html><head><meta property='og:title' content='Портативний блендер USB MiniJuice'/><meta property='product:price:amount' content='990'/><meta property='product:price:currency' content='UAH'/></head></html>")
            if "astroua.com.ua" in host:
                return httpx.Response(200, text="<html><head><meta property='og:title' content='Телескоп Celestron Pro'/><meta property='product:price:amount' content='4200'/><meta property='product:price:currency' content='UAH'/></head></html>")
            if "soloshop.com.ua" in host:
                return httpx.Response(200, text="<html><head><meta property='og:title' content='Рідкісний товар Одиночка'/><meta property='product:price:amount' content='650'/><meta property='product:price:currency' content='UAH'/></head></html>")
            if "hotline.ua" in host:
                return httpx.Response(200, text='<div class="list-item"><a href="/ua/blender/">Портативний блендер USB MiniJuice</a><span>890 – 1 190 ₴</span></div>')
            if "prom.ua" in host:
                if "wholesale" in u:
                    return httpx.Response(200, text=f"<script>{__import__('json').dumps(prom_ws)}</script>")
                return httpx.Response(200, text='<div data-qaid="product_block"><a href="/ua/p1.html"><span data-qaid="product_name">Портативний блендер USB MiniJuice</span></a><div data-qaid="product_price" data-qaprice="950">950 ₴</div></div>')
            if "trends.google.com" in host:
                return httpx.Response(200, text="<rss><channel><item><title>Блендер</title></item></channel></rss>")
            return httpx.Response(200, text="")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_http:
            res = await run_super_parse(
                output_dir=self.tmp_dir.name,
                settings=settings_tyv,
                client=mock_http,
            )

        reports = res["reports"]
        # SoloShop (1 ad) was filtered out by Stage 3 min_ad_sets=5; Blender + Celestron survive
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0].landing.core_phrase.lower().count("minijuice"), 1)
        self.assertEqual(reports[0].competitor_advertisers, 7)
        self.assertEqual(reports[0].total_matching_ads, 7)
        self.assertEqual(reports[0].wholesale_match_status, "verified")
        self.assertAlmostEqual(reports[0].best_supplier.wholesale_price_uah or 0.0, 310.0)
        self.assertTrue(reports[0].economics.ua_cod_net_profit_usd > 0)

        # Honest unmatched wholesale for Celestron (zero fake profits!)
        self.assertEqual(reports[1].wholesale_match_status, "none")
        self.assertIsNone(reports[1].best_supplier)
        self.assertIsNone(reports[1].economics.ua_net_profit_uah)
        self.assertEqual(reports[1].economics.recommended_model, "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
