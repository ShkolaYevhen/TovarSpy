import tempfile
import time
import unittest
from pathlib import Path

from config import (
    CoFounderEconomics,
    MarketplaceSignal,
    Settings,
    TelegramDropOffer,
    get_httpx_proxy,
)
from storage import (
    delete_tg_channel,
    get_cache,
    get_scan,
    list_scans,
    list_tg_channels,
    list_watchlist,
    normalize_tg_handle,
    remove_watchlist,
    save_scan,
    seed_default_channels,
    set_cache,
    upsert_tg_channel,
    upsert_watchlist,
)


class TestStorageAndConfigV2(unittest.TestCase):
    def test_config_proxy_and_new_models(self) -> None:
        settings = Settings(_env_file=None, PROXY_URL="  http://user:pass@1.2.3.4:8080  ")
        self.assertEqual(get_httpx_proxy(settings), "http://user:pass@1.2.3.4:8080")
        empty_settings = Settings(_env_file=None)
        self.assertIsNone(get_httpx_proxy(empty_settings))

        sig = MarketplaceSignal(source="allegro", country="PL", query="massager", sold_count=120, median_price=149.0)
        tg = TelegramDropOffer(channel="drop_ua", post_url="https://t.me/drop_ua/10", title="Massager", drop_price_uah=350.0)
        econ = CoFounderEconomics(opportunity_score=84.5, recommended_model="ua_cod")
        self.assertEqual(sig.sold_count, 120)
        self.assertEqual(tg.drop_price_uah, 350.0)
        self.assertEqual(econ.recommended_model, "ua_cod")

    def test_cache_ttl_and_storage_crud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db = Path(tmp_dir) / "nested" / "spy.db"

            # Cache hit & TTL expiry
            set_cache(db, "k1", {"ok": True, "count": 3})
            self.assertEqual(get_cache(db, "k1", ttl_seconds=60), {"ok": True, "count": 3})
            time.sleep(0.02)
            self.assertIsNone(get_cache(db, "k1", ttl_seconds=0))

            # Scan history
            scan_id = save_scan(
                db,
                query="neck massager",
                countries=["UA", "PL"],
                sources=["meta_web", "allegro"],
                total_count=4,
                winners_count=2,
                reports=[{"ad": {"ad_id": "1"}}],
            )
            scans = list_scans(db)
            self.assertEqual(len(scans), 1)
            self.assertEqual(scans[0]["id"], scan_id)
            loaded = get_scan(db, scan_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["reports"][0]["ad"]["ad_id"], "1")
            self.assertIsNone(get_scan(db, 9999))

            # Watchlist CRUD
            upsert_watchlist(db, "offer-1", "Neck Massager", "UA", 88.5, {"ad_id": "1"})
            upsert_watchlist(db, "offer-2", "Sunset Lamp", "PL", 91.0, {"ad_id": "2"})
            wl = list_watchlist(db)
            self.assertEqual([w["product_key"] for w in wl], ["offer-2", "offer-1"])
            self.assertTrue(remove_watchlist(db, "offer-1"))
            self.assertFalse(remove_watchlist(db, "offer-1"))

            # TG channels normalization & CRUD
            self.assertEqual(normalize_tg_handle("https://t.me/s/Drop_UA/123"), "drop_ua")
            self.assertEqual(normalize_tg_handle("@Opt_Drop_UA"), "opt_drop_ua")
            seed_default_channels(db, ["drop_ua", "opt_drop_ua"])
            channels = list_tg_channels(db)
            self.assertEqual(len(channels), 2)
            upsert_tg_channel(db, "https://t.me/s/tovarka_ua", label="Tovarka UA", enabled=False)
            enabled = list_tg_channels(db, enabled_only=True)
            self.assertEqual(len(enabled), 2)
            self.assertTrue(delete_tg_channel(db, "@tovarka_ua"))
            self.assertFalse(delete_tg_channel(db, "missing_chan"))


if __name__ == "__main__":
    unittest.main()
