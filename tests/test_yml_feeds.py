import tempfile
import unittest
from pathlib import Path

import httpx

from config import Settings
from storage import delete_yml_feed, list_yml_feeds, upsert_yml_feed
from yml_feed_manager import parse_yml_catalog, search_yml_offers

SAMPLE_YML = """<?xml version="1.0" encoding="UTF-8"?>
<yml_catalog date="2026-11-27 12:00">
  <shop>
    <name>UA Drop Warehouse</name>
    <offers>
      <offer id="101" available="true">
        <name>Портативний блендер USB 450мл MiniJuice</name>
        <vendor>SmartHome Drop</vendor>
        <vendorCode>MJ-450</vendorCode>
        <price>310</price>
        <oldprice>890</oldprice>
        <currencyId>UAH</currencyId>
        <picture>https://cdn.ua/blender.jpg</picture>
        <url>https://drop-ua.shop/blender</url>
      </offer>
      <offer id="102" available="false">
        <name>Портативний блендер USB (Немає в наявності)</name>
        <price>280</price>
        <currencyId>UAH</currencyId>
      </offer>
      <offer id="103" available="true">
        <name>Чохол для блендера силіконовий</name>
        <price>65</price>
        <currencyId>UAH</currencyId>
      </offer>
    </offers>
  </shop>
</yml_catalog>
"""


class TestYmlFeeds(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(_env_file=None, DB_PATH=str(Path(self.tmp.name) / "yml.db"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_parse_yml_catalog_skips_unavailable_and_extracts_rrc(self) -> None:
        items = parse_yml_catalog(SAMPLE_YML, source_label="SmartDrop")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["offer_id"], "101")
        self.assertAlmostEqual(items[0]["price_uah"], 310.0)
        self.assertAlmostEqual(items[0]["rrc_uah"], 890.0)
        self.assertEqual(items[0]["vendor"], "SmartHome Drop")

    async def test_yml_db_crud_and_exact_match_search(self) -> None:
        saved = upsert_yml_feed(self.settings.DB_PATH, "https://drop-ua.shop/feed.xml", label="UA 7km Drop")
        self.assertEqual(saved["label"], "UA 7km Drop")
        self.assertEqual(len(list_yml_feeds(self.settings.DB_PATH)), 1)

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=SAMPLE_YML)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            offers, diag = await search_yml_offers(
                "портативний блендер usb",
                "Портативний блендер USB MiniJuice",
                890.0,
                self.settings,
                client=client,
            )

        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].source, "yml_feed")
        self.assertAlmostEqual(offers[0].wholesale_price_uah or 0.0, 310.0)
        self.assertGreaterEqual(offers[0].match_score, 50.0)
        self.assertIn("verified", diag)
        self.assertTrue(delete_yml_feed(self.settings.DB_PATH, "https://drop-ua.shop/feed.xml"))
        self.assertEqual(list_yml_feeds(self.settings.DB_PATH), [])


if __name__ == "__main__":
    unittest.main()
