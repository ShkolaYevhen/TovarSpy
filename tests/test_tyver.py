import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from ad_collector import collect_winning_ads
from config import Settings
from tyver_client import (
    discover_winning_ads,
    fetch_tyver_creatives,
    fetch_tyver_quota,
    validate_cross_advertiser_demand,
)
from web_app import create_app


class TestTyverClient(unittest.IsolatedAsyncioTestCase):
    async def test_missing_api_key_returns_clear_diagnostic(self) -> None:
        quota = await fetch_tyver_quota("")
        self.assertFalse(quota["ok"])
        self.assertFalse(quota["configured"])
        self.assertEqual(quota["status"], "missing_key")

        creatives = await fetch_tyver_creatives("", mode="igaming")
        self.assertFalse(creatives["ok"])
        self.assertEqual(creatives["items"], [])
        self.assertIn("TYVER_API_KEY", creatives["error"])

    async def test_fetch_tyver_quota_parses_info_me_and_errors(self) -> None:
        captured_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            if request.url.path.endswith("/info/me"):
                return httpx.Response(
                    200,
                    json={"detail": "Success", "data": {"tariff": "PRO", "remainder_views": 53950}},
                )
            return httpx.Response(404, json={"detail": "Not found"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            res = await fetch_tyver_quota("tyv_test_secret", client=client)

        self.assertTrue(res["ok"])
        self.assertEqual(res["tariff"], "PRO")
        self.assertEqual(res["remainder_views"], 53950)
        self.assertEqual(captured_headers.get("x-tyver-authorization"), "tyv_test_secret")

        def unauth_handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "Invalid token"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(unauth_handler)) as client:
            err = await fetch_tyver_quota("tyv_bad", client=client)
        self.assertFalse(err["ok"])
        self.assertEqual(err["status"], "http_401")
        self.assertIn("Invalid token", err["error"])

    async def test_igaming_and_ecom_ua_params_and_media_normalization(self) -> None:
        seen_queries: list[dict[str, list[str]]] = []

        sample_items = [
            {
                "provider_id": "1001",
                "activity_days_amount": 25,
                "bodies": ["Bonus 500% UA"],
                "link_titles": ["Play Aviator Crash"],
                "countries": ["UA"],
                "provider_page_name": "LuckyClub",
                "cta": "Play Game",
                "target_url": "https://casino-offer.example/go",
                "domain": "casino-offer.example",
                "media_type": "VIDEO",
                "media": [
                    {
                        "content_type": "video/mp4",
                        "url": "https://cdn.tyver.example/vid.mp4",
                        "compressed_video_image_preview_url": "https://cdn.tyver.example/thumb.jpg",
                    }
                ],
                "verticals": ["GAMBLING"],
                "cms": None,
            },
            {
                "provider_id": "2002",
                "activity_days_amount": 12,
                "bodies": ["Органайзер для кухні зі знижкою 699 грн"],
                "link_titles": ["Kitchen Pro UA"],
                "countries": ["UA"],
                "provider_page_name": "HomeUA Shop",
                "cta": "Order Now",
                "target_url": "https://homeua.shop/organizer",
                "domain": "homeua.shop",
                "media_type": "IMAGE",
                "media": [
                    {
                        "content_type": "image/jpeg",
                        "url": "https://cdn.tyver.example/full.jpg",
                        "compressed_image_url": "https://cdn.tyver.example/compressed.jpg",
                    }
                ],
                "verticals": None,
                "cms": "SHOPIFY",
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            qs = parse_qs(urlparse(str(request.url)).query)
            seen_queries.append(qs)
            return httpx.Response(
                200,
                json={"detail": "Success", "data": {"items": sample_items, "cursor": "cur_2", "total": 2}},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            igaming = await fetch_tyver_creatives(
                "tyv_abc",
                mode="igaming",
                countries=["UA", "PL"],
                ai_angle="Big Win",
                ai_gambling_game_type="Crash",
                activity_days_amount_gte=10,
                client=client,
            )
            ecom = await fetch_tyver_creatives(
                "tyv_abc",
                mode="ecom_ua",
                query="органайзер",
                cta="Order Now",
                cms="SHOPIFY",
                client=client,
            )

        self.assertTrue(igaming["ok"])
        self.assertEqual(igaming["cursor"], "cur_2")
        self.assertEqual(igaming["items"][0]["video_url"], "https://cdn.tyver.example/vid.mp4")
        self.assertEqual(igaming["items"][0]["thumbnail_url"], "https://cdn.tyver.example/thumb.jpg")
        self.assertEqual(seen_queries[0]["ai_category"], ["GAMBLING"])
        self.assertEqual(seen_queries[0]["ai_angle"], ["Big Win"])
        self.assertEqual(seen_queries[0]["ai_gambling_game_type"], ["Crash"])
        self.assertEqual(seen_queries[0]["countries"], ["UA", "PL"])
        self.assertEqual(seen_queries[0]["activity_days_amount_gte"], ["10"])

        self.assertTrue(ecom["ok"])
        self.assertEqual(ecom["items"][1]["image_url"], "https://cdn.tyver.example/compressed.jpg")
        self.assertEqual(ecom["items"][1]["raw_source"], "tyver")
        self.assertNotIn("vertical", seen_queries[1])
        self.assertEqual(seen_queries[1]["countries"], ["UA"])
        self.assertEqual(seen_queries[1]["search"], ["органайзер"])
        self.assertEqual(seen_queries[1]["cta"], ["Order Now"])
        self.assertEqual(seen_queries[1]["cms"], ["SHOPIFY"])

    async def test_ad_collector_collect_winning_ads_with_tyver_source(self) -> None:
        payload = {
            "detail": "Success",
            "data": {
                "items": [
                    {
                        "provider_id": "9911",
                        "activity_days_amount": 15,
                        "bodies": ["Масажер UA"],
                        "link_titles": ["Neck Relax UA"],
                        "countries": ["UA"],
                        "provider_page_name": "RelaxUA",
                        "target_url": "https://relaxua.prom.ua/p123-massager",
                        "media": [{"content_type": "image/jpeg", "compressed_image_url": "https://img.test/m.jpg"}],
                    }
                ],
                "total": 1,
            },
        }

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=json.dumps(payload), headers={"Content-Type": "application/json"})

        settings = Settings(TYVER_API_KEY="tyv_live_key", MIN_DURATION_DAYS=7, MIN_ACTIVE_AD_SETS=5)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ads = await collect_winning_ads(
                settings,
                query="масажер",
                countries=["UA"],
                sources=["tyver"],
                mock=False,
                client=client,
                use_public_fallback=False,
            )

        self.assertEqual(len(ads), 1)
        self.assertEqual(ads[0].ad_id, "tyver-9911")
        self.assertEqual(ads[0].source, "tyver")
        self.assertEqual(ads[0].days_running, 15)
        self.assertEqual(ads[0].landing_url, "https://relaxua.prom.ua/p123-massager")
        self.assertTrue(ads[0].is_live)

    async def test_stage1_discover_winning_ads_filters_junk_and_dedups(self) -> None:
        raw_items = [
            {
                "provider_id": "1",
                "activity_days_amount": 8,
                "bodies": ["Бездротовий блендер"],
                "link_titles": ["Портативний блендер"],
                "countries": ["UA"],
                "provider_page_name": "ShopA",
                "target_url": "https://shop-a.com.ua/blender?fbclid=111",
                "domain": "shop-a.com.ua",
                "cta": "Shop now",
                "media": [{"content_type": "image/jpeg", "compressed_image_url": "https://img/b.jpg"}],
            },
            {
                "provider_id": "2",
                "activity_days_amount": 5,
                "bodies": ["Бездротовий блендер"],
                "link_titles": ["Портативний блендер"],
                "countries": ["UA"],
                "provider_page_name": "ShopA",
                "target_url": "https://shop-a.com.ua/blender?utm_source=fb",
                "domain": "shop-a.com.ua",
                "cta": "Order now",
            },
            {
                "provider_id": "3",
                "activity_days_amount": 90,
                "bodies": ["Amazon product"],
                "countries": ["UA"],
                "provider_page_name": "Amz",
                "target_url": "https://www.amazon.com/dp/B001",
                "domain": "amazon.com",
            },
            {
                "provider_id": "4",
                "activity_days_amount": 20,
                "bodies": ["PinUp Casino"],
                "countries": ["UA"],
                "provider_page_name": "Slots",
                "target_url": "https://spin-club.site/",
                "verticals": ["GAMBLING"],
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            qs = parse_qs(urlparse(str(request.url)).query)
            self.assertIn("creation_date_gte", qs)
            self.assertEqual(qs["activity_days_amount_gte"], ["3"])
            self.assertEqual(qs["countries"], ["UA"])
            return httpx.Response(200, json={"detail": "Success", "data": {"items": raw_items, "total": 4}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            res = await discover_winning_ads("tyv_abc", country="UA", min_days=3, client=client)

        self.assertTrue(res["ok"])
        self.assertEqual(len(res["items"]), 1)
        self.assertEqual(res["items"][0]["duplicate_count"], 2)
        self.assertEqual(res["items"][0]["activity_days"], 8)

    async def test_stage3_validate_cross_advertiser_demand(self) -> None:
        ads = [
            {
                "provider_id": f"ad-{i}",
                "activity_days_amount": 5 + i,
                "bodies": ["Портативний блендер USB 450мл"],
                "link_titles": ["Портативний блендер USB"],
                "countries": ["UA"],
                "provider_page_name": f"Store {i % 3}",
                "target_url": f"https://store{i}.com.ua/blender",
                "domain": f"store{i}.com.ua",
                "impressions": 1000,
            }
            for i in range(6)
        ] + [
            {
                "provider_id": "unrelated-1",
                "activity_days_amount": 10,
                "bodies": ["Зимова куртка пуховик"],
                "link_titles": ["Куртка"],
                "countries": ["UA"],
                "provider_page_name": "Jacket",
                "target_url": "https://jacket.com.ua/",
            }
        ]

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"detail": "Success", "data": {"items": ads, "total": 7}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            val = await validate_cross_advertiser_demand(
                "tyv_abc", "портативний блендер usb", country="UA", min_ad_sets=5, min_days=3, client=client
            )

        self.assertTrue(val["ok"])
        self.assertEqual(val["competitor_advertisers"], 3)
        self.assertEqual(val["total_matching_ads"], 6)
        self.assertTrue(val["meets_ad_sets_threshold"])
        self.assertEqual(val["cumulative_active_days"], sum(5 + i for i in range(6)))
        self.assertEqual(val["total_impressions"], 6000)


class TestTyverWebEndpoints(unittest.TestCase):
    def test_tyver_quota_and_search_routes_and_no_secret_leak(self) -> None:
        secret = "tyv_super_secret_should_never_leak"
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = Settings(
                _env_file=None,
                DB_PATH=str(Path(tmp_dir) / "tyver_web.db"),
                TYVER_API_KEY=secret,
            )
            app = create_app(output_dir=Path(tmp_dir), settings=settings)
            client = TestClient(app)

            with patch(
                "web_app.fetch_tyver_quota",
                new=AsyncMock(return_value={"ok": True, "configured": True, "status": "active", "tariff": "PRO", "remainder_views": 50000, "error": None}),
            ):
                q_res = client.get("/spy/api/tyver/quota")
                self.assertEqual(q_res.status_code, 200)
                self.assertEqual(q_res.json()["tariff"], "PRO")
                self.assertNotIn(secret, q_res.text)

            with (
                patch(
                    "web_app.fetch_tyver_creatives",
                    new=AsyncMock(return_value={"ok": True, "mode": "igaming", "total": 1, "cursor": None, "items": [{"id": "a1", "title": "Crash Game", "raw_source": "tyver"}], "error": None}),
                ),
                patch(
                    "web_app.fetch_tyver_quota",
                    new=AsyncMock(return_value={"ok": True, "configured": True, "status": "active", "tariff": "PRO", "remainder_views": 49999, "error": None}),
                ),
            ):
                s_res = client.post(
                    "/spy/api/tyver/search",
                    json={"mode": "igaming", "countries": ["UA"], "ai_gambling_game_type": "Crash"},
                )
                self.assertEqual(s_res.status_code, 200)
                body = s_res.json()
                self.assertTrue(body["ok"])
                self.assertEqual(body["items"][0]["id"], "a1")
                self.assertEqual(body["quota"]["remainder_views"], 49999)
                self.assertNotIn(secret, s_res.text)

            cfg = client.get("/api/config")
            self.assertEqual(cfg.status_code, 200)
            self.assertTrue(cfg.json()["tyver_configured"])
            self.assertTrue(cfg.json()["skip_china_sourcing"])
            self.assertNotIn(secret, cfg.text)

    def test_tyver_routes_missing_key_graceful(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = Settings(
                _env_file=None,
                DB_PATH=str(Path(tmp_dir) / "tyver_nokey.db"),
                TYVER_API_KEY=None,
            )
            client = TestClient(create_app(output_dir=Path(tmp_dir), settings=settings))
            q_res = client.get("/api/tyver/quota")
            self.assertEqual(q_res.status_code, 200)
            self.assertFalse(q_res.json()["ok"])
            self.assertFalse(q_res.json()["configured"])

            g_res = client.get("/spy/api/tyver/search?mode=ecom_ua&query=lamp")
            self.assertEqual(g_res.status_code, 200)
            self.assertFalse(g_res.json()["ok"])
            self.assertEqual(g_res.json()["items"], [])


if __name__ == "__main__":
    unittest.main()
