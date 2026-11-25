import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from config import AdRecord, Settings

logger = logging.getLogger(__name__)

META_ADS_ARCHIVE_URL = "https://graph.facebook.com/v19.0/ads_archive"
ADHEART_ADS_URL = "https://adheart.me/api/v1/ads"
TIKTOK_ADS_QUERY_URL = "https://open.tiktokapis.com/v2/research/adlib/ad/query/"


def _compute_days_running(start_date_str: Optional[str]) -> int:
    """Calculate elapsed days since an ISO-formatted start timestamp."""
    if not start_date_str:
        return 0
    cleaned = start_date_str.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        try:
            parsed = datetime.strptime(cleaned[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - parsed
    return max(0, delta.days)


def _normalize_url(url: str) -> str:
    """Normalize a landing URL for offer deduplication."""
    parsed = urlparse(url.strip())
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{netloc}{path}" if netloc else url.strip().lower().rstrip("/")


async def fetch_meta_ads(
    client: httpx.AsyncClient,
    settings: Settings,
    query: str,
    countries: list[str],
) -> list[AdRecord]:
    """Fetch active ads from the Meta Ad Library Graph API."""
    if not settings.META_ACCESS_TOKEN:
        return []

    params = {
        "access_token": settings.META_ACCESS_TOKEN,
        "search_terms": query or "offer",
        "ad_reached_countries": json.dumps(countries),
        "ad_active_status": "ACTIVE",
        "fields": (
            "id,page_name,ad_delivery_start_time,ad_snapshot_url,"
            "ad_creative_link_captions,collation_count,publisher_platforms"
        ),
    }
    try:
        resp = await client.get(META_ADS_ARCHIVE_URL, params=params)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Meta Ad Library request failed: %s", exc)
        return []

    records: list[AdRecord] = []
    for item in payload.get("data", []):
        captions = item.get("ad_creative_link_captions") or []
        landing = item.get("landing_url") or (captions[0] if captions else "") or item.get("ad_snapshot_url", "")
        if not landing:
            continue
        if not landing.startswith(("http://", "https://")):
            landing = f"https://{landing}"

        days = item.get("days_running")
        if days is None:
            days = _compute_days_running(item.get("ad_delivery_start_time"))

        item_countries = item.get("countries") or countries
        records.append(
            AdRecord(
                ad_id=str(item.get("id", "")),
                page_name=str(item.get("page_name") or "Unknown Page"),
                landing_url=landing,
                days_running=int(days),
                ad_creative_image_url=item.get("ad_creative_image_url") or item.get("ad_snapshot_url"),
                active_ad_sets=int(item.get("collation_count") or item.get("active_ad_sets") or 1),
                countries=[str(c).upper() for c in item_countries],
                source="meta",
            )
        )
    return records


async def fetch_adheart_ads(
    client: httpx.AsyncClient,
    settings: Settings,
    query: str,
    countries: list[str],
) -> list[AdRecord]:
    """Fetch active ads from the AdHeart REST API."""
    if not settings.ADHEART_API_KEY:
        return []

    headers = {"Authorization": f"Bearer {settings.ADHEART_API_KEY}"}
    params = {"q": query or "", "countries": ",".join(countries)}
    try:
        resp = await client.get(ADHEART_ADS_URL, headers=headers, params=params)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("AdHeart API request failed: %s", exc)
        return []

    raw_items: list[dict[str, Any]] = payload.get("items") or payload.get("data") or []
    records: list[AdRecord] = []
    for item in raw_items:
        landing = item.get("landing_url") or item.get("target_url") or ""
        if not landing:
            continue
        days = item.get("days_running")
        if days is None:
            days = _compute_days_running(item.get("started_at"))

        records.append(
            AdRecord(
                ad_id=str(item.get("ad_id") or item.get("id") or ""),
                page_name=str(item.get("page_name") or "Unknown Advertiser"),
                landing_url=landing,
                days_running=int(days),
                ad_creative_image_url=item.get("creative_image_url") or item.get("ad_creative_image_url"),
                active_ad_sets=int(item.get("active_ad_sets") or item.get("duplicates") or 1),
                countries=[str(c).upper() for c in (item.get("countries") or countries)],
                source="adheart",
            )
        )
    return records


async def fetch_tiktok_ads(
    client: httpx.AsyncClient,
    settings: Settings,
    query: str,
    countries: list[str],
) -> list[AdRecord]:
    """Fetch active ads from the TikTok Commercial Content API."""
    if not settings.TIKTOK_ACCESS_TOKEN:
        return []

    headers = {"Authorization": f"Bearer {settings.TIKTOK_ACCESS_TOKEN}"}
    body = {
        "search_term": query or "trending",
        "country_code": countries,
        "max_count": 20,
    }
    try:
        resp = await client.post(TIKTOK_ADS_QUERY_URL, headers=headers, json=body)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("TikTok Commercial Content API request failed: %s", exc)
        return []

    data_block = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    raw_ads: list[dict[str, Any]] = data_block.get("ads") or payload.get("ads") or []
    records: list[AdRecord] = []
    for item in raw_ads:
        landing = item.get("landing_page_url") or item.get("landing_url") or ""
        if not landing:
            continue
        days = item.get("days_running")
        if days is None:
            days = _compute_days_running(item.get("first_shown_date"))

        reach_countries = item.get("reach_countries") or item.get("countries") or countries
        records.append(
            AdRecord(
                ad_id=str(item.get("id") or item.get("ad_id") or ""),
                page_name=str(item.get("advertiser_name") or item.get("page_name") or "TikTok Advertiser"),
                landing_url=landing,
                days_running=int(days),
                ad_creative_image_url=item.get("image_url") or item.get("ad_creative_image_url"),
                active_ad_sets=int(item.get("ad_group_count") or item.get("active_ad_sets") or 1),
                countries=[str(c).upper() for c in reach_countries],
                source="tiktok",
            )
        )
    return records


def generate_mock_ads(
    query: str = "",
    countries: Optional[list[str]] = None,
) -> list[AdRecord]:
    """Return deterministic sample ads (winners and non-winners) for dry-run mode."""
    _ = query
    _ = countries
    return [
        AdRecord(
            ad_id="meta-1001",
            page_name="SmartRelax PL",
            landing_url="https://smartrelax-shop.pl/products/ems-neck-massager",
            days_running=14,
            ad_creative_image_url="https://cdn.example.com/ads/neck-massager.jpg",
            active_ad_sets=9,
            countries=["PL", "DE"],
            source="meta",
        ),
        AdRecord(
            ad_id="adheart-1002",
            page_name="HomeGlow UA",
            landing_url="https://homeglow.com.ua/sunset-projection-lamp",
            days_running=11,
            ad_creative_image_url="https://cdn.example.com/ads/sunset-lamp.jpg",
            active_ad_sets=6,
            countries=["UA", "RO"],
            source="adheart",
        ),
        AdRecord(
            ad_id="tiktok-1003",
            page_name="AutoClean Pro",
            landing_url="https://autocleanpro.com/mini-car-vacuum",
            days_running=19,
            ad_creative_image_url="https://cdn.example.com/ads/car-vacuum.jpg",
            active_ad_sets=12,
            countries=["US", "DE", "CZ"],
            source="tiktok",
        ),
        AdRecord(
            ad_id="meta-1004",
            page_name="PostureFit EU",
            landing_url="https://posturefit.tilda.ws/corrector",
            days_running=8,
            ad_creative_image_url="https://cdn.example.com/ads/posture-corrector.jpg",
            active_ad_sets=5,
            countries=["CZ", "PL"],
            source="meta",
        ),
        # Non-winners below to verify filtering in dry-run pipelines
        AdRecord(
            ad_id="meta-1005-short",
            page_name="FlashDrop",
            landing_url="https://flashdrop.example/short-run",
            days_running=4,
            ad_creative_image_url="https://cdn.example.com/ads/short.jpg",
            active_ad_sets=8,
            countries=["US"],
            source="meta",
        ),
        AdRecord(
            ad_id="adheart-1006-lowscale",
            page_name="SmallTest",
            landing_url="https://smalltest.example/low-scale",
            days_running=15,
            ad_creative_image_url="https://cdn.example.com/ads/low.jpg",
            active_ad_sets=3,
            countries=["UA"],
            source="adheart",
        ),
    ]


def filter_winning_ads(
    ads: list[AdRecord],
    min_duration_days: int = 7,
    min_active_ad_sets: int = 5,
    target_countries: Optional[list[str]] = None,
) -> list[AdRecord]:
    """Filter ads by duration, active ad set scale, and target countries, deduplicating offers."""
    allowed_countries = {
        c.upper() for c in (target_countries or ["UA", "PL", "RO", "CZ", "DE", "US"])
    }
    best_by_offer: dict[str, AdRecord] = {}

    for ad in ads:
        if ad.days_running < min_duration_days:
            continue
        if ad.active_ad_sets < min_active_ad_sets:
            continue
        ad_countries = {c.upper() for c in ad.countries}
        if not allowed_countries.intersection(ad_countries):
            continue

        offer_key = _normalize_url(ad.landing_url)
        existing = best_by_offer.get(offer_key)
        if existing is None or (ad.active_ad_sets, ad.days_running) > (
            existing.active_ad_sets,
            existing.days_running,
        ):
            best_by_offer[offer_key] = ad

    return list(best_by_offer.values())


async def collect_winning_ads(
    settings: Settings,
    query: str = "",
    countries: Optional[list[str]] = None,
    sources: Optional[list[str]] = None,
    mock: bool = False,
    client: Optional[httpx.AsyncClient] = None,
) -> list[AdRecord]:
    """Collect ads from configured sources (or mock dataset) and return filtered winners."""
    target_countries = [c.upper() for c in (countries or settings.TARGET_COUNTRIES)]

    if mock:
        raw_ads = generate_mock_ads(query=query, countries=target_countries)
    else:
        selected = [s.lower() for s in (sources or ["meta", "adheart", "tiktok"])]
        fetcher_map = {
            "meta": fetch_meta_ads,
            "adheart": fetch_adheart_ads,
            "tiktok": fetch_tiktok_ads,
        }

        async def _run_with_client(http_client: httpx.AsyncClient) -> list[AdRecord]:
            tasks = [
                fetcher_map[src](http_client, settings, query, target_countries)
                for src in selected
                if src in fetcher_map
            ]
            if not tasks:
                return []
            results = await asyncio.gather(*tasks)
            combined: list[AdRecord] = []
            for batch in results:
                combined.extend(batch)
            return combined

        if client is not None:
            raw_ads = await _run_with_client(client)
        else:
            async with httpx.AsyncClient(timeout=20.0) as owned_client:
                raw_ads = await _run_with_client(owned_client)

    return filter_winning_ads(
        raw_ads,
        min_duration_days=settings.MIN_DURATION_DAYS,
        min_active_ad_sets=settings.MIN_ACTIVE_AD_SETS,
        target_countries=target_countries,
    )
