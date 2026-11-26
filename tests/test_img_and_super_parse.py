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
        self.assertTrue(res["source_diagnostics"]["aliexpress"].startswith("live:"))
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


if __name__ == "__main__":
    unittest.main()
