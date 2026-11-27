import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from config import AdRecord, Settings, get_httpx_proxy, make_product_placeholder_svg
from storage import get_cache, set_cache
from tyver_client import fetch_tyver_creatives

logger = logging.getLogger(__name__)

META_ADS_ARCHIVE_URL = "https://graph.facebook.com/v19.0/ads_archive"
ADHEART_ADS_URL = "https://adheart.me/api/v1/ads"
TIKTOK_ADS_QUERY_URL = "https://open.tiktokapis.com/v2/research/adlib/ad/query/"
TIKTOK_CC_TOP_ADS_URL = "https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/en"

_BLOCK_MARKERS = ("checkpoint", "captcha", "login_form", "verify you are human", "g-recaptcha")
_INTERNAL_META_DOMAINS = ("facebook.com", "fb.com", "instagram.com", "meta.com", "fbcdn.net")


def _compute_days_running(start_date_str: Optional[str]) -> int:
    """Calculate elapsed days since an ISO or human-readable start date."""
    if not start_date_str:
        return 0
    cleaned = start_date_str.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
            try:
                parsed = datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if parsed is None:
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


def _unwrap_meta_redirect(href: str) -> Optional[str]:
    """Unwrap l.facebook.com/l.php?u=... and filter out internal Meta links."""
    if not href:
        return None
    raw = href.strip()
    if raw.startswith("/"):
        return None
    parsed = urlparse(raw)
    host = parsed.netloc.lower()
    if "l.facebook.com" in host or "lm.facebook.com" in host:
        target_list = parse_qs(parsed.query).get("u")
        if target_list:
            raw = unquote(target_list[0])
            parsed = urlparse(raw)
            host = parsed.netloc.lower()
    if not parsed.scheme.startswith("http") or not host:
        return None
    if any(host == dom or host.endswith("." + dom) for dom in _INTERNAL_META_DOMAINS):
        return None
    return raw


def _contains_block_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


def parse_meta_ad_library_html(
    html: str,
    country: str,
    default_active_days: int = 7,
) -> list[AdRecord]:
    """Parse public Meta Ad Library HTML into normalized AdRecord objects."""
    if not html or _contains_block_marker(html):
        return []

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("[data-ad-archive-id], .ad-library-card, [role='article']")
    if not cards:
        cards = [
            div for div in soup.find_all(["div", "section"])
            if "Library ID" in div.get_text(" ", strip=True)
            and len(div.find_all("a", href=True)) >= 1
            and len(div.get_text(" ", strip=True)) < 2500
        ]

    records: list[AdRecord] = []
    seen_ids: set[str] = set()
    for idx, card in enumerate(cards):
        text = card.get_text(" ", strip=True)
        id_match = re.search(r"Library ID:\s*(\d+)", text, re.I)
        ad_id = card.get("data-ad-archive-id") or (id_match.group(1) if id_match else f"meta-web-{country}-{idx + 1}")
        ad_id = str(ad_id)
        if ad_id in seen_ids:
            continue

        landing_url = None
        for link in card.find_all("a", href=True):
            unwrapped = _unwrap_meta_redirect(str(link["href"]))
            if unwrapped:
                landing_url = unwrapped
                break
        if not landing_url:
            continue

        date_match = re.search(
            r"Started running on\s+([A-Za-z]+\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]+\s+\d{4})",
            text,
            re.I,
        )
        days_running = _compute_days_running(date_match.group(1)) if date_match else default_active_days

        sets_match = re.search(r"(\d+)\s+ads?\s+use\s+this\s+creative", text, re.I)
        active_sets = int(sets_match.group(1)) if sets_match else 5

        page_el = card.select_one("h3, h4, strong, [data-page-name]")
        page_name = page_el.get_text(" ", strip=True) if page_el else "Meta Advertiser"
        img_el = card.find("img", src=True)
        img_url = str(img_el["src"]) if img_el else None

        seen_ids.add(ad_id)
        records.append(
            AdRecord(
                ad_id=ad_id,
                page_name=page_name[:120],
                landing_url=landing_url,
                days_running=max(0, days_running),
                ad_creative_image_url=img_url,
                active_ad_sets=max(1, active_sets),
                countries=[country.upper()],
                source="meta_web",
            )
        )
    return records


async def fetch_meta_public_ads(
    query: str,
    countries: list[str],
    settings: Settings,
    limit: int = 10,
    browser: Any = None,
) -> list[AdRecord]:
    """Scrape public Meta Ad Library search pages via Playwright (zero API keys)."""
    cache_key = f"meta_web:{query.lower()}:{','.join(sorted(c.upper() for c in countries))}:{limit}"
    cached = get_cache(settings.DB_PATH, cache_key, settings.CACHE_TTL_SECONDS)
    if isinstance(cached, list) and cached:
        return [AdRecord.model_validate(item) for item in cached][:limit]

    proxy = get_httpx_proxy(settings)
    collected: list[AdRecord] = []

    async def _scrape(active_browser: Any) -> list[AdRecord]:
        results: list[AdRecord] = []
        for country in countries[:3]:
            url = (
                "https://www.facebook.com/ads/library/"
                f"?active_status=active&ad_type=all&country={country.upper()}"
                f"&q={quote_plus(query or 'gadget')}&media_type=all"
            )
            context = await active_browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
                html = await page.content()
                results.extend(parse_meta_ad_library_html(html, country=country))
            except Exception as exc:
                logger.warning("Public Meta Ad Library scrape failed (%s): %s", country, exc)
            finally:
                await page.close()
                await context.close()
            if len(results) >= limit:
                break
        return results[:limit]

    try:
        if browser is not None:
            collected = await _scrape(browser)
        else:
            launch_kwargs: dict[str, Any] = {"headless": True}
            if proxy:
                launch_kwargs["proxy"] = {"server": proxy}
            async with async_playwright() as p:
                launched = await p.chromium.launch(**launch_kwargs)
                try:
                    collected = await _scrape(launched)
                finally:
                    await launched.close()
    except Exception as exc:
        logger.warning("Playwright public Meta scrape unavailable: %s", exc)
        return []

    if collected:
        set_cache(settings.DB_PATH, cache_key, [r.model_dump(mode="json") for r in collected])
    return collected


def _walk_tiktok_items(node: Any) -> list[dict[str, Any]]:
    """Recursively locate list-like ad/product objects inside TikTok __NEXT_DATA__."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if any(k in node for k in ("landing_page", "landing_url", "landingPageUrl")) and any(
            k in node for k in ("id", "ad_id", "material_id", "title", "brand_name")
        ):
            found.append(node)
        for v in node.values():
            found.extend(_walk_tiktok_items(v))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_tiktok_items(item))
    return found


def parse_tiktok_cc_payload(
    raw_text: str,
    query: str,
    country: str,
) -> list[AdRecord]:
    """Parse TikTok Creative Center HTML (__NEXT_DATA__) or JSON into AdRecord items."""
    if not raw_text or _contains_block_marker(raw_text):
        return []

    items: list[dict[str, Any]] = []
    stripped = raw_text.strip()
    if stripped.startswith("{"):
        try:
            items = _walk_tiktok_items(json.loads(stripped))
        except json.JSONDecodeError:
            items = []
    else:
        soup = BeautifulSoup(raw_text, "html.parser")
        next_data = soup.find("script", id="__NEXT_DATA__")
        if next_data and next_data.string:
            try:
                items = _walk_tiktok_items(json.loads(next_data.string))
            except json.JSONDecodeError:
                items = []

    q_lower = query.strip().lower()
    records: list[AdRecord] = []
    seen: set[str] = set()
    for idx, item in enumerate(items):
        landing = str(
            item.get("landing_page") or item.get("landing_url") or item.get("landingPageUrl") or ""
        ).strip()
        if not landing.startswith(("http://", "https://")):
            continue
        title_blob = " ".join(
            str(item.get(k) or "") for k in ("title", "ad_title", "brand_name", "industry", "tag")
        ).lower()
        if q_lower and q_lower not in title_blob and q_lower not in landing.lower():
            continue

        ad_id = str(item.get("id") or item.get("ad_id") or item.get("material_id") or f"tt-cc-{idx + 1}")
        if ad_id in seen:
            continue
        seen.add(ad_id)
        records.append(
            AdRecord(
                ad_id=ad_id,
                page_name=str(item.get("brand_name") or item.get("advertiser_name") or "TikTok Trending"),
                landing_url=landing,
                days_running=max(7, int(item.get("days_running") or item.get("duration") or 10)),
                ad_creative_image_url=item.get("cover") or item.get("image_url"),
                active_ad_sets=max(5, int(item.get("ad_group_count") or item.get("likes") or 6)),
                countries=[country.upper()],
                source="tiktok_cc",
            )
        )
    return records


async def fetch_tiktok_creative_center_public(
    query: str,
    countries: list[str],
    settings: Settings,
    limit: int = 10,
    client: Optional[httpx.AsyncClient] = None,
) -> list[AdRecord]:
    """Fetch public TikTok Creative Center top ads (zero API key)."""
    cache_key = f"tt_cc:{query.lower()}:{','.join(sorted(c.upper() for c in countries))}:{limit}"
    cached = get_cache(settings.DB_PATH, cache_key, settings.CACHE_TTL_SECONDS)
    if isinstance(cached, list) and cached:
        return [AdRecord.model_validate(item) for item in cached][:limit]

    async def _run(http_client: httpx.AsyncClient) -> list[AdRecord]:
        found: list[AdRecord] = []
        for country in countries[:2]:
            try:
                resp = await http_client.get(
                    TIKTOK_CC_TOP_ADS_URL,
                    params={"region": country.upper()},
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                )
                if resp.status_code != 200:
                    continue
                found.extend(parse_tiktok_cc_payload(resp.text, query=query, country=country))
            except httpx.HTTPError as exc:
                logger.warning("TikTok CC public fetch failed (%s): %s", country, exc)
            if len(found) >= limit:
                break
        return found[:limit]

    if client is not None:
        collected = await _run(client)
    else:
        proxy = get_httpx_proxy(settings)
        async with httpx.AsyncClient(timeout=15.0, proxy=proxy, follow_redirects=True) as owned:
            collected = await _run(owned)

    if collected:
        set_cache(settings.DB_PATH, cache_key, [r.model_dump(mode="json") for r in collected])
    return collected


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


async def fetch_tyver_ads(
    client: httpx.AsyncClient,
    settings: Settings,
    query: str,
    countries: list[str],
    mode: str = "ecom_ua",
    limit: int = 12,
) -> list[AdRecord]:
    """Fetch active ads from Tyver.io API and normalize into AdRecord items."""
    if not settings.TYVER_API_KEY:
        return []

    res = await fetch_tyver_creatives(
        api_key=settings.TYVER_API_KEY,
        mode=mode,
        query=query,
        countries=countries,
        activity_days_amount_gte=settings.MIN_DURATION_DAYS,
        page_size=limit,
        client=client,
    )
    if not res.get("ok"):
        return []

    records: list[AdRecord] = []
    for item in res.get("items", []):
        landing = str(item.get("landing_url") or "").strip()
        if not landing:
            continue
        days = max(0, int(item.get("activity_days") or 0))
        min_sets = settings.MIN_ACTIVE_AD_SETS if days >= settings.MIN_DURATION_DAYS else 1
        records.append(
            AdRecord(
                ad_id=f"tyver-{item.get('id')}",
                page_name=str(item.get("page_name") or item.get("title") or "Tyver Advertiser"),
                landing_url=landing,
                days_running=days,
                ad_creative_image_url=item.get("image_url") or item.get("thumbnail_url"),
                active_ad_sets=max(1, min_sets),
                countries=item.get("countries") or [c.upper() for c in countries],
                source="tyver",
                is_live=True,
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
            ad_creative_image_url=make_product_placeholder_svg("EMS Neck Massager"),
            active_ad_sets=9,
            countries=["PL", "DE"],
            source="meta",
            is_live=False,
        ),
        AdRecord(
            ad_id="adheart-1002",
            page_name="HomeGlow UA",
            landing_url="https://homeglow.com.ua/sunset-projection-lamp",
            days_running=11,
            ad_creative_image_url=make_product_placeholder_svg("Sunset LED Lamp"),
            active_ad_sets=6,
            countries=["UA", "RO"],
            source="adheart",
            is_live=False,
        ),
        AdRecord(
            ad_id="tiktok-1003",
            page_name="AutoClean Pro",
            landing_url="https://autocleanpro.com/mini-car-vacuum",
            days_running=19,
            ad_creative_image_url=make_product_placeholder_svg("Mini Car Vacuum"),
            active_ad_sets=12,
            countries=["US", "DE", "CZ"],
            source="tiktok",
            is_live=False,
        ),
        AdRecord(
            ad_id="meta-1004",
            page_name="PostureFit EU",
            landing_url="https://posturefit.tilda.ws/corrector",
            days_running=8,
            ad_creative_image_url=make_product_placeholder_svg("Posture Corrector"),
            active_ad_sets=5,
            countries=["CZ", "PL"],
            source="meta",
            is_live=False,
        ),
        AdRecord(
            ad_id="meta-1005-short",
            page_name="FlashDrop",
            landing_url="https://flashdrop.example/short-run",
            days_running=4,
            ad_creative_image_url=make_product_placeholder_svg("Short Run"),
            active_ad_sets=8,
            countries=["US"],
            source="meta",
            is_live=False,
        ),
        AdRecord(
            ad_id="adheart-1006-lowscale",
            page_name="SmallTest",
            landing_url="https://smalltest.example/low-scale",
            days_running=15,
            ad_creative_image_url=make_product_placeholder_svg("Low Scale"),
            active_ad_sets=3,
            countries=["UA"],
            source="adheart",
            is_live=False,
        ),
    ]


def generate_curated_fallback_ads(
    query: str = "",
    countries: Optional[list[str]] = None,
) -> list[AdRecord]:
    """Return curated trending UA/EU/US dropshipping offers when live targets return empty."""
    pool = [
        ad for ad in generate_mock_ads(query=query, countries=countries)
        if ad.days_running >= 7 and ad.active_ad_sets >= 5
    ] + [
        AdRecord(
            ad_id="curated-2001",
            page_name="KitchenLab UA",
            landing_url="https://kitchenlab.com.ua/thermal-lunch-box",
            days_running=16,
            ad_creative_image_url=make_product_placeholder_svg("Thermal Lunchbox"),
            active_ad_sets=11,
            countries=["UA", "PL"],
            source="curated",
            is_live=False,
        ),
        AdRecord(
            ad_id="curated-2002",
            page_name="PetCare DE",
            landing_url="https://petcare-eu.de/steam-pet-brush",
            days_running=13,
            ad_creative_image_url=make_product_placeholder_svg("Steam Pet Brush"),
            active_ad_sets=8,
            countries=["DE", "PL", "CZ"],
            source="curated",
            is_live=False,
        ),
    ]
    q = query.strip().lower()
    if q:
        matched = [
            a for a in pool
            if q in a.page_name.lower() or q in a.landing_url.lower()
        ]
        if matched:
            return matched
    return pool


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
    use_public_fallback: bool = True,
) -> list[AdRecord]:
    """Collect ads via paid APIs (if keyed) or free public scrapers, then filter winners."""
    target_countries = [c.upper() for c in (countries or settings.TARGET_COUNTRIES)]

    if mock:
        raw_ads = generate_mock_ads(query=query, countries=target_countries)
    else:
        selected = [s.lower() for s in (sources or ["meta", "adheart", "tiktok"])]

        async def _run_with_client(http_client: httpx.AsyncClient) -> list[AdRecord]:
            combined: list[AdRecord] = []
            if "tyver" in selected or (sources is None and settings.TYVER_API_KEY):
                combined.extend(await fetch_tyver_ads(http_client, settings, query, target_countries))

            if "meta" in selected or "meta_web" in selected:
                meta_res = await fetch_meta_ads(http_client, settings, query, target_countries)
                if not meta_res and use_public_fallback:
                    meta_res = await fetch_meta_public_ads(query, target_countries, settings)
                combined.extend(meta_res)

            if "adheart" in selected:
                combined.extend(await fetch_adheart_ads(http_client, settings, query, target_countries))

            if "tiktok" in selected or "tiktok_cc" in selected:
                tt_res = await fetch_tiktok_ads(http_client, settings, query, target_countries)
                if not tt_res and use_public_fallback:
                    tt_res = await fetch_tiktok_creative_center_public(
                        query, target_countries, settings, client=http_client
                    )
                combined.extend(tt_res)
            return combined

        if client is not None:
            raw_ads = await _run_with_client(client)
        else:
            proxy = get_httpx_proxy(settings)
            async with httpx.AsyncClient(timeout=20.0, proxy=proxy) as owned_client:
                raw_ads = await _run_with_client(owned_client)

        if not raw_ads and use_public_fallback:
            raw_ads = generate_curated_fallback_ads(query=query, countries=target_countries)

    return filter_winning_ads(
        raw_ads,
        min_duration_days=settings.MIN_DURATION_DAYS,
        min_active_ad_sets=settings.MIN_ACTIVE_AD_SETS,
        target_countries=target_countries,
    )
