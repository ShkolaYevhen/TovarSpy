import json
import unittest
from datetime import datetime, timedelta, timezone

import httpx

from ad_collector import (
    ADHEART_ADS_URL,
    META_ADS_ARCHIVE_URL,
    TIKTOK_ADS_QUERY_URL,
    collect_winning_ads,
    fetch_adheart_ads,
    fetch_meta_ads,
    fetch_tiktok_ads,
    filter_winning_ads,
)
from config import AdRecord, Settings


class TestAdCollector(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            META_ACCESS_TOKEN="meta-token",
            ADHEART_API_KEY="adheart-key",
            TIKTOK_ACCESS_TOKEN="tiktok-token",
        )

    async def test_fetch_meta_ads_parses_records(self) -> None:
        start_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(str(request.url).startswith(META_ADS_ARCHIVE_URL))
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "m1",
                            "page_name": "Meta Store",
                            "ad_delivery_start_time": start_time,
                            "ad_creative_link_captions": ["shop.example.com/item"],
                            "ad_snapshot_url": "https://fb.com/snap/m1",
                            "collation_count": 7,
                        }
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ads = await fetch_meta_ads(client, self.settings, "item", ["PL", "DE"])

        self.assertEqual(len(ads), 1)
        self.assertEqual(ads[0].ad_id, "m1")
        self.assertEqual(ads[0].landing_url, "https://shop.example.com/item")
        self.assertGreaterEqual(ads[0].days_running, 9)
        self.assertEqual(ads[0].active_ad_sets, 7)
        self.assertEqual(ads[0].source, "meta")

    async def test_fetch_adheart_and_tiktok_parse_and_handle_errors(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.startswith(ADHEART_ADS_URL):
                self.assertEqual(request.headers.get("Authorization"), "Bearer adheart-key")
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "ad_id": "ah1",
                                "page_name": "AH Shop",
                                "landing_url": "https://ah.example/p1",
                                "days_running": 9,
                                "duplicates": 6,
                                "countries": ["UA"],
                                "creative_image_url": "https://ah.example/img.jpg",
                            }
                        ]
                    },
                )
            if url.startswith(TIKTOK_ADS_QUERY_URL):
                payload = json.loads(request.content.decode("utf-8"))
                self.assertEqual(payload["search_term"], "lamp")
                return httpx.Response(500, json={"error": "server error"})
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ah_ads = await fetch_adheart_ads(client, self.settings, "lamp", ["UA"])
            tt_ads = await fetch_tiktok_ads(client, self.settings, "lamp", ["UA"])

        self.assertEqual(len(ah_ads), 1)
        self.assertEqual(ah_ads[0].ad_id, "ah1")
        self.assertEqual(ah_ads[0].active_ad_sets, 6)
        self.assertEqual(tt_ads, [])

    async def test_fetch_tiktok_ads_success_and_missing_keys(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "ads": [
                            {
                                "id": "tt1",
                                "advertiser_name": "TT Brand",
                                "landing_page_url": "https://tt.example/offer",
                                "days_running": 12,
                                "ad_group_count": 8,
                                "reach_countries": ["RO", "PL"],
                                "image_url": "https://tt.example/img.png",
                            }
                        ]
                    }
                },
            )

        empty_settings = Settings(_env_file=None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(await fetch_meta_ads(client, empty_settings, "q", ["US"]), [])
            self.assertEqual(await fetch_adheart_ads(client, empty_settings, "q", ["US"]), [])
            self.assertEqual(await fetch_tiktok_ads(client, empty_settings, "q", ["US"]), [])
            tt_ads = await fetch_tiktok_ads(client, self.settings, "q", ["RO"])

        self.assertEqual(len(tt_ads), 1)
        self.assertEqual(tt_ads[0].source, "tiktok")
        self.assertEqual(tt_ads[0].active_ad_sets, 8)

    def test_filter_winning_ads_boundaries_and_dedup(self) -> None:
        candidates = [
            AdRecord(
                ad_id="pass-exact",
                page_name="P1",
                landing_url="https://shop.com/a/",
                days_running=7,
                active_ad_sets=5,
                countries=["UA"],
            ),
            AdRecord(
                ad_id="pass-better-dup",
                page_name="P1",
                landing_url="https://shop.com/a",
                days_running=10,
                active_ad_sets=8,
                countries=["PL"],
            ),
            AdRecord(
                ad_id="fail-days",
                page_name="P2",
                landing_url="https://shop.com/b",
                days_running=6,
                active_ad_sets=9,
                countries=["DE"],
            ),
            AdRecord(
                ad_id="fail-sets",
                page_name="P3",
                landing_url="https://shop.com/c",
                days_running=12,
                active_ad_sets=4,
                countries=["US"],
            ),
            AdRecord(
                ad_id="fail-country",
                page_name="P4",
                landing_url="https://shop.com/d",
                days_running=12,
                active_ad_sets=10,
                countries=["FR", "ES"],
            ),
        ]
        winners = filter_winning_ads(
            candidates,
            min_duration_days=7,
            min_active_ad_sets=5,
            target_countries=["UA", "PL", "RO", "CZ", "DE", "US"],
        )
        self.assertEqual([w.ad_id for w in winners], ["pass-better-dup"])

    async def test_collect_winning_ads_mock_mode(self) -> None:
        winners = await collect_winning_ads(self.settings, mock=True)
        self.assertEqual(len(winners), 4)
        for ad in winners:
            self.assertGreaterEqual(ad.days_running, 7)
            self.assertGreaterEqual(ad.active_ad_sets, 5)


if __name__ == "__main__":
    unittest.main()
