import asyncio
import json
import logging
import re
from statistics import median
from typing import Any, Optional
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup

from config import (
    DEFAULT_FX_RATES_TO_USD,
    MarketplaceIntelReport,
    MarketplaceSignal,
    Settings,
    SupplierOffer,
    get_httpx_proxy,
)
from product_matcher import compute_dhash, evaluate_product_match
from storage import get_cache, set_cache

logger = logging.getLogger(__name__)

_INSTALLMENT_RE = re.compile(
    r"(?:від|от)?\s*\d[\d\s\u00a0]*(?:[.,]\d{1,2})?\s*(?:₴|грн|UAH)?\s*/\s*міс(?:\.|яц[ьі]?)?"
    r"|оплата\s+частинами[^₴\d]*\d[\d\s\u00a0]*(?:[.,]\d{1,2})?\s*(?:₴|грн|UAH)?",
    re.I,
)


def _strip_installment_text(text: str) -> str:
    """Remove Prom/Bigl monthly installment snippets like 'від 250 ₴ /міс' before price extraction."""
    return _INSTALLMENT_RE.sub(" ", text or "")

_CAPTCHA_MARKERS = (
    "validatecaptcha",
    "verify you are human",
    "datadome",
    "cf-challenge",
    "just a moment",
    "checking your browser",
    "not a robot",
    "g-recaptcha",
    "access denied",
)


def _is_blocked_html(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in _CAPTCHA_MARKERS)


def _parse_float(raw: str) -> Optional[float]:
    cleaned = raw.replace("\u00a0", "").replace(" ", "").strip()
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        val = float(cleaned)
        return round(val, 2) if val > 0 else None
    except ValueError:
        return None


def _price_stats(prices: list[float], currency: str) -> tuple[float, float, float, float]:
    if not prices:
        return 0.0, 0.0, 0.0, 0.0
    med = round(float(median(prices)), 2)
    fx = DEFAULT_FX_RATES_TO_USD.get(currency.upper(), 1.0)
    return med, round(min(prices), 2), round(max(prices), 2), round(med * fx, 2)


def parse_allegro_html(html: str, query: str) -> MarketplaceSignal:
    """Parse public Allegro.pl search results for PLN pricing and buyer velocity."""
    if not html or _is_blocked_html(html):
        return MarketplaceSignal(
            source="allegro_pl",
            platform="allegro_pl",
            country="PL",
            query=query,
            currency="PLN",
            status="blocked" if html else "empty",
            blocked=bool(html),
            note="Blocked by anti-bot challenge" if html else "Empty response",
        )

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    prices = [
        val
        for raw in re.findall(r"(\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*(?:zł|PLN)", text, re.I)
        if (val := _parse_float(raw)) is not None and 5.0 <= val <= 20000.0
    ]
    sold_matches = re.findall(r"(\d+)\s*(?:os[oó]b|osoby|osoba)?\s*kupi[łl]o", text, re.I)
    total_sold = sum(int(m) for m in sold_matches)

    count_match = re.search(r"(\d[\d\s]*)\s+(?:ofert|wynik[oó]w)", text, re.I)
    listing_count = int(re.sub(r"\s+", "", count_match.group(1))) if count_match else len(prices)

    sample_url = None
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if "/oferta/" in href:
            sample_url = urljoin("https://allegro.pl", href)
            break

    med, pmin, pmax, med_usd = _price_stats(prices, "PLN")
    velocity = min(100.0, round((total_sold / 200.0) * 100.0, 2)) if total_sold else (
        min(45.0, float(len(prices) * 4)) if prices else 0.0
    )
    status = "live" if (prices or listing_count > 0) else "empty"

    return MarketplaceSignal(
        source="allegro_pl",
        platform="allegro_pl",
        country="PL",
        query=query,
        listing_count=listing_count,
        sold_count=total_sold,
        median_price=med,
        min_price=pmin,
        max_price=pmax,
        currency="PLN",
        median_price_usd=med_usd,
        sold_velocity_score=velocity,
        seller_density=listing_count,
        seller_count=listing_count,
        review_velocity=float(total_sold),
        top_sample_url=sample_url,
        status=status,
    )


def parse_olx_html(html: str, query: str, domain: str = "ua") -> MarketplaceSignal:
    """Parse OLX.ua or OLX.pl search results for median prices and listing saturation."""
    is_ua = domain.lower() == "ua"
    platform = "olx_ua" if is_ua else "olx_pl"
    country = "UA" if is_ua else "PL"
    currency = "UAH" if is_ua else "PLN"
    base_url = "https://www.olx.ua" if is_ua else "https://www.olx.pl"

    if not html or _is_blocked_html(html):
        return MarketplaceSignal(
            source=platform,
            platform=platform,
            country=country,
            query=query,
            currency=currency,
            status="blocked" if html else "empty",
            blocked=bool(html),
            note="Blocked by anti-bot challenge" if html else "Empty response",
        )

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    total_el = soup.select_one('[data-testid="total-count"]')
    listing_count = 0
    if total_el:
        digits = re.search(r"(\d[\d\s]*)", total_el.get_text(" ", strip=True))
        if digits:
            listing_count = int(re.sub(r"\s+", "", digits.group(1)))
    if not listing_count:
        cnt_match = re.search(
            r"(?:Знайдено|Znaleźliśmy)\s+(\d[\d\s]*)|(\d[\d\s]*)\s+(?:оголошень|ogłoszeń)",
            text,
            re.I,
        )
        if cnt_match:
            raw_num = cnt_match.group(1) or cnt_match.group(2)
            listing_count = int(re.sub(r"\s+", "", raw_num))

    curr_regex = r"(?:грн|₴|UAH)" if is_ua else r"(?:zł|PLN)"
    cards = soup.select('[data-cy="l-card"], [data-testid="l-card"]')
    price_blobs = [c.get_text(" ", strip=True) for c in cards] if cards else [text]
    prices: list[float] = []
    for blob in price_blobs:
        for raw in re.findall(rf"(\d[\d\s\u00a0]*(?:[.,]\d{{1,2}})?)\s*{curr_regex}", blob, re.I):
            val = _parse_float(raw)
            if val is not None and 5.0 <= val <= 100000.0:
                prices.append(val)
                if cards:
                    break

    if not listing_count:
        listing_count = len(cards) or len(prices)

    sample_url = None
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if "/d/" in href or "/oferta/" in href:
            sample_url = urljoin(base_url, href)
            break

    med, pmin, pmax, med_usd = _price_stats(prices, currency)
    velocity = min(70.0, round((listing_count / 150.0) * 70.0, 2)) if listing_count else 0.0
    status = "live" if (prices or listing_count > 0) else "empty"

    return MarketplaceSignal(
        source=platform,
        platform=platform,
        country=country,
        query=query,
        listing_count=listing_count,
        median_price=med,
        min_price=pmin,
        max_price=pmax,
        currency=currency,
        median_price_usd=med_usd,
        sold_velocity_score=velocity,
        seller_density=listing_count,
        seller_count=listing_count,
        top_sample_url=sample_url,
        status=status,
    )


def parse_prom_or_rozetka_html(
    html: str,
    query: str,
    platform: str = "prom_ua",
) -> MarketplaceSignal:
    """Parse Prom.ua or Rozetka.com.ua search HTML for UAH retail pricing & seller density."""
    base_url = "https://prom.ua" if platform == "prom_ua" else "https://rozetka.com.ua"
    if not html or _is_blocked_html(html):
        return MarketplaceSignal(
            source=platform,
            platform=platform,
            country="UA",
            query=query,
            currency="UAH",
            status="blocked" if html else "empty",
            blocked=bool(html),
            note="Blocked by anti-bot challenge" if html else "Empty response",
        )

    soup = BeautifulSoup(html, "html.parser")
    text = _strip_installment_text(soup.get_text(" ", strip=True))
    prices = [
        val
        for raw in re.findall(r"(\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*(?:₴|грн|UAH)", text, re.I)
        if (val := _parse_float(raw)) is not None and 20.0 <= val <= 100000.0
    ]

    seller_nodes = soup.select('[data-qaid="company_name"], .goods-tile__seller, .seller-name')
    sellers = {n.get_text(" ", strip=True) for n in seller_nodes if n.get_text(" ", strip=True)}
    seller_density = len(sellers) if sellers else len(prices)

    review_hits = re.findall(r"(\d+)\s*(?:відгук|отзыв|замовлен|продаж)", text, re.I)
    total_reviews = sum(int(r) for r in review_hits)

    sample_url = None
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if "/p" in href or "goods" in href or "/ua/" in href:
            sample_url = urljoin(base_url, href)
            break

    med, pmin, pmax, med_usd = _price_stats(prices, "UAH")
    velocity = min(100.0, round((total_reviews / 150.0) * 100.0, 2)) if total_reviews else (
        min(50.0, float(len(prices) * 5)) if prices else 0.0
    )
    status = "live" if (prices or seller_density > 0) else "empty"

    return MarketplaceSignal(
        source=platform,
        platform=platform,
        country="UA",
        query=query,
        listing_count=len(prices),
        sold_count=total_reviews,
        median_price=med,
        min_price=pmin,
        max_price=pmax,
        currency="UAH",
        median_price_usd=med_usd,
        sold_velocity_score=velocity,
        seller_density=seller_density,
        seller_count=seller_density,
        review_velocity=float(total_reviews),
        top_sample_url=sample_url,
        status=status,
    )


def parse_amazon_search_html(html: str, query: str) -> MarketplaceSignal:
    """Parse Amazon search HTML for USD retail prices, review counts, and monthly bought badges."""
    if not html or _is_blocked_html(html):
        return MarketplaceSignal(
            source="amazon_us",
            platform="amazon_us",
            country="US",
            query=query,
            currency="USD",
            status="blocked" if html else "empty",
            blocked=bool(html),
            note="Blocked by Amazon CAPTCHA" if html else "Empty response",
        )

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('[data-component-type="s-search-result"]')
    prices: list[float] = []
    bought_total = 0
    reviews_total = 0
    sample_url = None

    for card in cards:
        price_el = card.select_one(".a-price .a-offscreen")
        if price_el:
            val = _parse_float(price_el.get_text(" ", strip=True).lstrip("$"))
            if val is not None:
                prices.append(val)

        card_text = card.get_text(" ", strip=True)
        bought_match = re.search(r"(\d+(?:\.\d+)?)\s*(K)?\+?\s*bought in past month", card_text, re.I)
        if bought_match:
            num = float(bought_match.group(1))
            if bought_match.group(2):
                num *= 1000
            bought_total += int(num)

        rev_el = card.select_one("span.a-size-base.s-underline-text")
        if rev_el:
            rev_digits = re.sub(r"[^\d]", "", rev_el.get_text(strip=True))
            if rev_digits:
                reviews_total += int(rev_digits)

        if sample_url is None:
            for a in card.find_all("a", href=True):
                if "/dp/" in str(a["href"]):
                    sample_url = urljoin("https://www.amazon.com", str(a["href"]))
                    break

    med, pmin, pmax, med_usd = _price_stats(prices, "USD")
    velocity = min(100.0, round((bought_total / 1000.0) * 100.0, 2)) if bought_total else (
        min(60.0, round((reviews_total / 1000.0) * 60.0, 2)) if reviews_total else 0.0
    )
    status = "live" if (cards or prices) else "empty"

    return MarketplaceSignal(
        source="amazon_us",
        platform="amazon_us",
        country="US",
        query=query,
        listing_count=len(cards),
        sold_count=bought_total,
        median_price=med,
        min_price=pmin,
        max_price=pmax,
        currency="USD",
        median_price_usd=med_usd,
        sold_velocity_score=velocity,
        seller_density=len(cards),
        seller_count=len(cards),
        review_velocity=float(reviews_total),
        top_sample_url=sample_url,
        status=status,
    )


_PLATFORM_URLS: dict[str, tuple[str, Any]] = {
    "allegro_pl": ("https://allegro.pl/listing?string={q}", parse_allegro_html),
    "olx_ua": ("https://www.olx.ua/uk/list/q-{q}/", lambda h, q: parse_olx_html(h, q, domain="ua")),
    "olx_pl": ("https://www.olx.pl/oferty/q-{q}/", lambda h, q: parse_olx_html(h, q, domain="pl")),
    "prom_ua": ("https://prom.ua/ua/search?search_term={q}", lambda h, q: parse_prom_or_rozetka_html(h, q, "prom_ua")),
    "rozetka_ua": ("https://rozetka.com.ua/ua/search/?text={q}", lambda h, q: parse_prom_or_rozetka_html(h, q, "rozetka_ua")),
    "amazon_us": ("https://www.amazon.com/s?k={q}", parse_amazon_search_html),
}


def _mock_signals_for_query(query: str, platforms: list[str]) -> list[MarketplaceSignal]:
    """Return realistic deterministic marketplace benchmarks for mock/dry-run mode."""
    samples: dict[str, MarketplaceSignal] = {
        "olx_ua": MarketplaceSignal(
            source="olx_ua", platform="olx_ua", country="UA", query=query,
            listing_count=42, median_price=890.0, min_price=650.0, max_price=1250.0,
            currency="UAH", median_price_usd=21.36, sold_velocity_score=48.0,
            seller_density=42, seller_count=42, status="live",
        ),
        "prom_ua": MarketplaceSignal(
            source="prom_ua", platform="prom_ua", country="UA", query=query,
            listing_count=28, sold_count=95, median_price=1050.0, min_price=790.0, max_price=1390.0,
            currency="UAH", median_price_usd=25.20, sold_velocity_score=63.3,
            seller_density=19, seller_count=19, review_velocity=95.0, status="live",
        ),
        "allegro_pl": MarketplaceSignal(
            source="allegro_pl", platform="allegro_pl", country="PL", query=query,
            listing_count=65, sold_count=180, median_price=139.0, min_price=99.0, max_price=189.0,
            currency="PLN", median_price_usd=34.75, sold_velocity_score=90.0,
            seller_density=65, seller_count=65, status="live",
        ),
        "olx_pl": MarketplaceSignal(
            source="olx_pl", platform="olx_pl", country="PL", query=query,
            listing_count=31, median_price=119.0, min_price=85.0, max_price=159.0,
            currency="PLN", median_price_usd=29.75, sold_velocity_score=38.0,
            seller_density=31, seller_count=31, status="live",
        ),
        "amazon_us": MarketplaceSignal(
            source="amazon_us", platform="amazon_us", country="US", query=query,
            listing_count=24, sold_count=850, median_price=34.99, min_price=24.99, max_price=49.99,
            currency="USD", median_price_usd=34.99, sold_velocity_score=85.0,
            seller_density=24, seller_count=24, review_velocity=1420.0, status="live",
        ),
    }
    return [samples[p] for p in platforms if p in samples]


async def _fetch_single_platform(
    platform: str,
    query: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> MarketplaceSignal:
    """Fetch and parse one marketplace platform with SQLite cache support."""
    if platform not in _PLATFORM_URLS:
        return MarketplaceSignal(source=platform, platform=platform, query=query, status="empty")

    cache_key = f"mkt:{platform}:{query.strip().lower()}"
    cached = get_cache(settings.DB_PATH, cache_key, settings.CACHE_TTL_SECONDS)
    if isinstance(cached, dict):
        sig = MarketplaceSignal.model_validate(cached)
        sig.status = "cached"
        return sig

    template, parser = _PLATFORM_URLS[platform]
    url = template.format(q=quote_plus(query or "gadget"))
    try:
        resp = await http_client.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "uk-UA,uk;q=0.9,pl;q=0.8,en;q=0.7",
            },
        )
        if resp.status_code in (403, 429, 503):
            return MarketplaceSignal(
                source=platform,
                platform=platform,
                query=query,
                status="blocked",
                blocked=True,
                note=f"HTTP {resp.status_code}",
            )
        if resp.status_code != 200:
            return MarketplaceSignal(
                source=platform,
                platform=platform,
                query=query,
                status="empty",
                note=f"HTTP {resp.status_code}",
            )
        signal = parser(resp.text, query)
        if signal.status == "live":
            set_cache(settings.DB_PATH, cache_key, signal.model_dump(mode="json"))
        return signal
    except httpx.HTTPError as exc:
        logger.warning("Marketplace fetch failed (%s): %s", platform, exc)
        return MarketplaceSignal(
            source=platform,
            platform=platform,
            query=query,
            status="empty",
            note=str(exc),
        )


def _aggregate_intel_report(query: str, signals: list[MarketplaceSignal]) -> MarketplaceIntelReport:
    """Compute velocity, saturation, and regional median prices across signals."""
    vel_scores = [s.sold_velocity_score for s in signals if s.sold_velocity_score > 0]
    demand_velocity = round(sum(vel_scores) / len(vel_scores), 2) if vel_scores else 0.0

    densities = [s.seller_density for s in signals if s.seller_density > 0]
    avg_density = (sum(densities) / len(densities)) if densities else 0.0
    saturation = min(100.0, round((avg_density / 200.0) * 100.0, 2)) if avg_density else 0.0

    ua_prices = [
        s.median_price for s in signals
        if s.platform in ("olx_ua", "prom_ua", "rozetka_ua") and s.median_price > 0
    ]
    pl_prices = [
        s.median_price for s in signals
        if s.platform in ("allegro_pl", "olx_pl") and s.median_price > 0
    ]
    us_prices = [
        s.median_price for s in signals
        if s.platform == "amazon_us" and s.median_price > 0
    ]

    return MarketplaceIntelReport(
        query=query,
        signals=signals,
        demand_velocity_score=demand_velocity,
        saturation_score=saturation,
        ua_median_price_uah=round(float(median(ua_prices)), 2) if ua_prices else None,
        pl_median_price_pln=round(float(median(pl_prices)), 2) if pl_prices else None,
        us_median_price_usd=round(float(median(us_prices)), 2) if us_prices else None,
    )


async def analyze_marketplace_intel(
    query: str,
    settings: Settings,
    platforms: Optional[list[str]] = None,
    client: Optional[httpx.AsyncClient] = None,
    mock: bool = False,
) -> MarketplaceIntelReport:
    """Concurrently analyze demand, saturation, and prices across public marketplaces."""
    selected = platforms or ["olx_ua", "prom_ua", "allegro_pl", "olx_pl", "amazon_us"]
    if mock:
        return _aggregate_intel_report(query, _mock_signals_for_query(query, selected))

    if client is not None:
        signals = list(
            await asyncio.gather(*(_fetch_single_platform(p, query, settings, client) for p in selected))
        )
    else:
        proxy = get_httpx_proxy(settings)
        async with httpx.AsyncClient(timeout=15.0, proxy=proxy, follow_redirects=True) as owned:
            signals = list(
                await asyncio.gather(*(_fetch_single_platform(p, query, settings, owned) for p in selected))
            )

    return _aggregate_intel_report(query, signals)


def parse_prom_items(
    html: str,
    query: str,
    limit: int = 10,
    source: str = "prom_ua",
    supplier_type: str = "ua_retail",
) -> list[dict[str, Any]]:
    """Extract individual Prom.ua product cards (retail or wholesale) ignoring '/міс' installments."""
    if not html or _is_blocked_html(html):
        return []
    soup = BeautifulSoup(html, "html.parser")
    uah_rate = DEFAULT_FX_RATES_TO_USD.get("UAH", 0.024)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in soup.select('[data-qaid="product_block"]'):
        title_el = card.select_one('[data-qaid="product_name"]')
        price_el = card.select_one('[data-qaid="product_price"]')
        link_el = card.select_one('a[href*="/p"]') or card.select_one("a[href]")
        if not title_el or not link_el:
            continue
        title = title_el.get_text(" ", strip=True)
        href = urljoin("https://prom.ua", str(link_el["href"]))
        if not title or href in seen:
            continue

        price_uah: Optional[float] = None
        if price_el and price_el.get("data-qaprice"):
            price_uah = _parse_float(str(price_el["data-qaprice"]))
        if price_uah is None:
            raw_price_text = price_el.get_text(" ", strip=True) if price_el else card.get_text(" ", strip=True)
            cleaned_price_text = _strip_installment_text(raw_price_text)
            m_price = re.search(r"(\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*(?:₴|грн|UAH)?", cleaned_price_text, re.I)
            price_uah = _parse_float(m_price.group(1)) if m_price else None
        if price_uah is None or price_uah <= 0:
            continue

        img_el = card.find("img")
        img_url = ""
        if img_el:
            img_url = str(img_el.get("src") or img_el.get("data-src") or "").strip()
            if img_url.startswith("//"):
                img_url = "https:" + img_url
        seller_el = card.select_one('[data-qaid="company_name"]')
        seller = seller_el.get_text(" ", strip=True) if seller_el else "Prom.ua Seller"
        seen.add(href)
        items.append({
            "source": source,
            "supplier_type": supplier_type,
            "country": "UA",
            "query": query,
            "title": title[:160],
            "price": price_uah,
            "currency": "UAH",
            "price_usd": round(price_uah * uah_rate, 2),
            "image_url": img_url,
            "product_url": href,
            "seller_name": seller[:100],
            "is_live": True,
        })
        if len(items) >= limit:
            break
    return items


def parse_bigl_items(
    html: str,
    query: str,
    limit: int = 10,
    supplier_type: str = "ua_wholesale",
) -> list[dict[str, Any]]:
    """Extract Bigl.ua product cards with UAH prices, photos, and product links."""
    if not html or _is_blocked_html(html):
        return []
    soup = BeautifulSoup(html, "html.parser")
    uah_rate = DEFAULT_FX_RATES_TO_USD.get("UAH", 0.024)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for link in soup.select('a[href*="/p"]'):
        raw_href = str(link.get("href") or "").strip()
        if not raw_href:
            continue
        href = urljoin("https://bigl.ua", raw_href).split("?")[0]
        if href in seen:
            continue

        card = link
        for _ in range(4):
            if card.parent is not None and len(card.parent.get_text(" ", strip=True)) < 550:
                card = card.parent
            else:
                break

        raw_text = card.get_text(" ", strip=True)
        clean_text = _strip_installment_text(raw_text)
        prices = [
            val
            for m in re.findall(r"(\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*(?:₴|грн)", clean_text, re.I)
            if (val := _parse_float(m)) is not None and 20.0 <= val <= 100000.0
        ]
        if not prices:
            continue
        price_uah = prices[0]

        title = link.get_text(" ", strip=True)
        if not title or len(title) < 6:
            title_node = card.select_one('[data-qaid="product_name"], h2, h3, .bgl-product__title')
            title = title_node.get_text(" ", strip=True) if title_node else clean_text[:120]
        if not title:
            continue

        img_el = card.find("img")
        img_url = ""
        if img_el:
            img_url = str(img_el.get("src") or img_el.get("data-src") or "").strip()
            if img_url.startswith("//"):
                img_url = "https:" + img_url

        seen.add(href)
        items.append({
            "source": "bigl_ua",
            "supplier_type": supplier_type,
            "country": "UA",
            "query": query,
            "title": title[:160],
            "price": price_uah,
            "currency": "UAH",
            "price_usd": round(price_uah * uah_rate, 2),
            "image_url": img_url,
            "product_url": href,
            "seller_name": "Bigl.ua Supplier",
            "is_live": True,
        })
        if len(items) >= limit:
            break
    return items


async def fetch_ua_wholesale_offers(
    query: str,
    settings: Settings,
    limit_per_source: int = 8,
    client: Optional[httpx.AsyncClient] = None,
) -> tuple[list[SupplierOffer], dict[str, str]]:
    """Fetch live Ukrainian local wholesale offers from Prom.ua wholesale, OLX.ua opt, and Bigl.ua."""
    clean_q = (query or "органайзер").strip()
    cache_key = f"ua_wholesale:{clean_q.lower()}:{limit_per_source}"
    cached = get_cache(settings.DB_PATH, cache_key, settings.CACHE_TTL_SECONDS)
    if isinstance(cached, dict) and isinstance(cached.get("offers"), list):
        offers = [SupplierOffer.model_validate(o) for o in cached["offers"]]
        diag = dict(cached.get("diagnostics") or {})
        return offers, {k: v.replace("live:", "cached:") for k, v in diag.items()}

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.7",
    }
    q_enc = quote_plus(clean_q)
    q_slug = quote_plus(f"{clean_q} опт").replace("+", "-")

    sources_cfg = [
        (
            "prom_wholesale",
            f"https://prom.ua/ua/search?search_term={q_enc}&sale_type=wholesale",
            lambda h: parse_prom_items(h, clean_q, limit=limit_per_source, source="prom_wholesale", supplier_type="ua_wholesale"),
        ),
        (
            "olx_opt",
            f"https://www.olx.ua/uk/list/q-{q_slug}/",
            lambda h: parse_olx_items(h, clean_q, domain="ua", limit=limit_per_source),
        ),
        (
            "bigl_ua",
            f"https://bigl.ua/ua/search?search_term={q_enc}",
            lambda h: parse_bigl_items(h, clean_q, limit=limit_per_source, supplier_type="ua_wholesale"),
        ),
    ]

    async def _fetch_src(http_client: httpx.AsyncClient, name: str, url: str, parser: Any) -> tuple[str, list[SupplierOffer], str]:
        try:
            resp = await http_client.get(url, headers=headers)
            if resp.status_code in (403, 429, 503):
                return name, [], f"blocked: HTTP {resp.status_code}"
            if resp.status_code != 200:
                return name, [], f"empty: HTTP {resp.status_code}"
            if _is_blocked_html(resp.text):
                return name, [], "blocked: CAPTCHA/anti-bot"
            raw_items = parser(resp.text)
            converted: list[SupplierOffer] = []
            for it in raw_items:
                p_uah = float(it.get("price") or 0.0)
                if p_uah <= 0:
                    continue
                src_tag = "olx_opt" if name == "olx_opt" else it.get("source", name)
                converted.append(
                    SupplierOffer(
                        supplier_name=str(it.get("seller_name") or name),
                        product_title=str(it.get("title") or clean_q),
                        product_url=str(it.get("product_url") or ""),
                        wholesale_price_rmb=0.0,
                        wholesale_price_usd=float(it.get("price_usd") or round(p_uah * DEFAULT_FX_RATES_TO_USD["UAH"], 2)),
                        wholesale_price_uah=round(p_uah, 2),
                        moq=1,
                        image_url=it.get("image_url") or None,
                        source=src_tag,
                        supplier_type="ua_wholesale",
                        sales_count_text="UA Wholesale",
                        is_live=True,
                    )
                )
            if converted:
                return name, converted, f"live: {len(converted)} items"
            return name, [], "empty: 0 items"
        except httpx.HTTPError as exc:
            return name, [], f"error: {exc}"

    if client is not None:
        batches = await asyncio.gather(*(_fetch_src(client, n, u, p) for n, u, p in sources_cfg))
    else:
        proxy = get_httpx_proxy(settings)
        async with httpx.AsyncClient(timeout=15.0, proxy=proxy, follow_redirects=True) as owned:
            batches = await asyncio.gather(*(_fetch_src(owned, n, u, p) for n, u, p in sources_cfg))

    all_offers: list[SupplierOffer] = []
    diagnostics: dict[str, str] = {}
    for name, items, status_msg in batches:
        all_offers.extend(items)
        diagnostics[name] = status_msg

    all_offers.sort(key=lambda o: (o.wholesale_price_uah or 999999.0, o.wholesale_price_usd))
    if all_offers:
        set_cache(
            settings.DB_PATH,
            cache_key,
            {"offers": [o.model_dump(mode="json") for o in all_offers], "diagnostics": diagnostics},
        )
    return all_offers, diagnostics


def parse_olx_items(html: str, query: str, domain: str = "ua", limit: int = 10) -> list[dict[str, Any]]:
    """Extract individual OLX.ua or OLX.pl listing cards with real photos, prices, and links."""
    if not html or _is_blocked_html(html):
        return []
    is_ua = domain.lower() == "ua"
    source = "olx_ua" if is_ua else "olx_pl"
    country = "UA" if is_ua else "PL"
    currency = "UAH" if is_ua else "PLN"
    base_url = "https://www.olx.ua" if is_ua else "https://www.olx.pl"
    fx = DEFAULT_FX_RATES_TO_USD.get(currency, 0.024)

    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in soup.select('[data-cy="l-card"], [data-testid="l-card"]'):
        title_el = card.select_one('h4, h6, [data-cy="ad-card-title"]')
        price_el = card.select_one('[data-testid="ad-price"]')
        link_el = card.select_one("a[href]")
        if not title_el or not link_el:
            continue
        title = title_el.get_text(" ", strip=True)
        href = urljoin(base_url, str(link_el["href"]))
        if not title or href in seen:
            continue
        price_text = price_el.get_text(" ", strip=True) if price_el else card.get_text(" ", strip=True)
        m_price = re.search(r"(\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)", price_text)
        price_val = _parse_float(m_price.group(1)) if m_price else None
        if price_val is None:
            continue
        img_el = card.find("img")
        img_url = ""
        if img_el:
            img_url = str(img_el.get("src") or img_el.get("data-src") or "").strip()
            if img_url.startswith("//"):
                img_url = "https:" + img_url
        seen.add(href)
        items.append({
            "source": source,
            "country": country,
            "query": query,
            "title": title[:160],
            "price": price_val,
            "currency": currency,
            "price_usd": round(price_val * fx, 2),
            "image_url": img_url,
            "product_url": href,
            "seller_name": f"OLX.{domain.upper()} Seller",
            "is_live": True,
        })
        if len(items) >= limit:
            break
    return items


async def fetch_live_marketplace_products(
    query: str,
    settings: Settings,
    platforms: Optional[list[str]] = None,
    limit_per_platform: int = 8,
    client: Optional[httpx.AsyncClient] = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Fetch live marketplace product cards (Prom.ua, OLX.ua, OLX.pl) plus source diagnostics."""
    selected = platforms or ["prom_ua", "olx_ua", "olx_pl"]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept-Language": "uk-UA,uk;q=0.9,pl;q=0.8,en;q=0.7",
    }
    q_enc = quote_plus(query or "масажер")
    q_slug = quote_plus(query or "masazer").replace("+", "-")

    async def _fetch_one(http_client: httpx.AsyncClient, plat: str) -> tuple[str, list[dict[str, Any]], str]:
        cache_key = f"mkt_items:{plat}:{(query or '').strip().lower()}"
        cached = get_cache(settings.DB_PATH, cache_key, settings.CACHE_TTL_SECONDS)
        if isinstance(cached, list) and cached:
            return plat, cached[:limit_per_platform], f"cached: {len(cached)} items"

        if plat == "prom_ua":
            url = f"https://prom.ua/ua/search?search_term={q_enc}&sort=popularity"
            parser = lambda h: parse_prom_items(h, query, limit=limit_per_platform)
        elif plat == "olx_ua":
            url = f"https://www.olx.ua/uk/list/q-{q_slug}/"
            parser = lambda h: parse_olx_items(h, query, domain="ua", limit=limit_per_platform)
        elif plat == "olx_pl":
            url = f"https://www.olx.pl/oferty/q-{q_slug}/"
            parser = lambda h: parse_olx_items(h, query, domain="pl", limit=limit_per_platform)
        elif plat == "bigl_ua":
            url = f"https://bigl.ua/ua/search?search_term={q_enc}"
            parser = lambda h: parse_bigl_items(h, query, limit=limit_per_platform, supplier_type="ua_retail")
        else:
            return plat, [], "unsupported"

        try:
            resp = await http_client.get(url, headers=headers)
            if resp.status_code in (403, 429, 503):
                return plat, [], f"blocked: HTTP {resp.status_code}"
            if resp.status_code != 200:
                return plat, [], f"empty: HTTP {resp.status_code}"
            if _is_blocked_html(resp.text):
                return plat, [], "blocked: CAPTCHA/anti-bot"
            parsed = parser(resp.text)
            if parsed:
                set_cache(settings.DB_PATH, cache_key, parsed)
                return plat, parsed, f"live: {len(parsed)} items"
            return plat, [], "empty: 0 items"
        except httpx.HTTPError as exc:
            return plat, [], f"error: {exc}"

    if client is not None:
        results = await asyncio.gather(*(_fetch_one(client, p) for p in selected))
    else:
        proxy = get_httpx_proxy(settings)
        async with httpx.AsyncClient(timeout=15.0, proxy=proxy, follow_redirects=True) as owned:
            results = await asyncio.gather(*(_fetch_one(owned, p) for p in selected))

    all_items: list[dict[str, Any]] = []
    diagnostics: dict[str, str] = {}
    for plat, items, status_msg in results:
        all_items.extend(items)
        diagnostics[plat] = status_msg
    return all_items, diagnostics


def parse_hotline_items(html: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Extract Hotline.ua comparison cards with UAH price range, offer count, photo, and link."""
    if not html or _is_blocked_html(html):
        return []
    soup = BeautifulSoup(html, "html.parser")
    uah_rate = DEFAULT_FX_RATES_TO_USD.get("UAH", 0.024)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for card in soup.select(".list-item, [class*='list-item--']"):
        title_link = None
        title = ""
        for a in card.select('a[href*="/ua/"]'):
            txt = a.get_text(" ", strip=True)
            if txt and "акц" not in txt.lower() and "пропозиц" not in txt.lower() and len(txt) >= 4:
                title_link = a
                title = txt
                break
        if not title_link:
            img_alt = card.select_one("img[alt]")
            a_img = card.select_one('a[href*="/ua/"]')
            if img_alt and a_img:
                title = str(img_alt.get("alt") or "").strip()
                title_link = a_img
        if not title or not title_link:
            continue

        href = urljoin("https://hotline.ua", str(title_link.get("href") or "")).split("?")[0]
        if not href or href in seen:
            continue

        clean_text = _strip_installment_text(card.get_text(" ", strip=True)).replace(title, " ")
        m_range = re.search(
            r"\b(\d{1,3}(?:[\s\u00a0]\d{3})+|\d+(?:[.,]\d{1,2})?)\s*(?:[–-]\s*(\d{1,3}(?:[\s\u00a0]\d{3})+|\d+(?:[.,]\d{1,2})?))?\s*₴",
            clean_text,
        )
        if not m_range:
            continue
        p_min = _parse_float(m_range.group(1))
        p_max = _parse_float(m_range.group(2)) if m_range.group(2) else p_min
        if p_min is None or p_min < 20.0:
            continue

        offers_m = re.search(r"(?:(\d+)\s*пропозиц|пропозиц[іїй]+\s*\(?(\d+)\)?)", clean_text, re.I)
        offers_cnt = int(offers_m.group(1) or offers_m.group(2)) if offers_m else 1
        img_el = card.find("img")
        img_url = ""
        if img_el:
            raw_src = str(img_el.get("src") or img_el.get("data-src") or "").strip()
            if raw_src:
                img_url = urljoin("https://hotline.ua", raw_src)

        seen.add(href)
        items.append({
            "source": "hotline_ua",
            "country": "UA",
            "query": query,
            "title": title[:160],
            "price": p_min,
            "price_max": p_max or p_min,
            "currency": "UAH",
            "price_usd": round(p_min * uah_rate, 2),
            "offer_count": offers_cnt,
            "image_url": img_url,
            "product_url": href,
            "seller_name": f"Hotline ({offers_cnt} shops)",
            "is_live": True,
        })
        if len(items) >= limit:
            break
    return items


def _walk_prom_wholesale_nodes(node: Any) -> list[dict[str, Any]]:
    """Find Prom.ua embedded Product JSON objects carrying a non-empty wholesalePrices array."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        ws = node.get("wholesalePrices")
        if isinstance(ws, list) and len(ws) > 0 and (node.get("name") or node.get("model")):
            found.append(node)
        for val in node.values():
            if isinstance(val, (dict, list)):
                found.extend(_walk_prom_wholesale_nodes(val))
    elif isinstance(node, list):
        for elem in node:
            if isinstance(elem, (dict, list)):
                found.extend(_walk_prom_wholesale_nodes(elem))
    return found


def parse_prom_wholesale_tiers(html: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Extract ONLY real Prom.ua listings with populated wholesalePrices tiers (never wholesalePrices=[])."""
    if not html or _is_blocked_html(html):
        return []

    uah_rate = DEFAULT_FX_RATES_TO_USD.get("UAH", 0.024)
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    # 1. Inspect embedded Apollo/Redux JSON scripts for populated wholesalePrices arrays
    for script in soup.find_all("script"):
        raw = script.string or ""
        if '"wholesalePrices"' not in raw:
            continue
        start_idx = raw.find("{")
        while start_idx != -1:
            try:
                obj, end_offset = json.JSONDecoder().raw_decode(raw[start_idx:])
                for pnode in _walk_prom_wholesale_nodes(obj):
                    ws_entries = [w for w in (pnode.get("wholesalePrices") or []) if isinstance(w, dict)]
                    tier_prices: list[float] = []
                    tier_moqs: list[int] = []
                    for w in ws_entries:
                        pv = _parse_float(str(w.get("price") or ""))
                        if pv is not None and pv > 0:
                            tier_prices.append(pv)
                            q_val = w.get("minimumOrderQuantity") or w.get("minQuantity") or w.get("quantity") or 2
                            try:
                                tier_moqs.append(max(2, int(float(str(q_val)))))
                            except ValueError:
                                tier_moqs.append(2)
                    if not tier_prices:
                        continue

                    min_ws = min(tier_prices)
                    min_moq = tier_moqs[tier_prices.index(min_ws)] if tier_moqs else 2
                    model_obj = pnode.get("model") if isinstance(pnode.get("model"), dict) else {}
                    title = str(pnode.get("name") or model_obj.get("name") or "").strip()
                    if not title:
                        continue

                    pid = str(pnode.get("id") or "").strip()
                    url_text = str(pnode.get("urlText") or model_obj.get("urlText") or "product").strip()
                    raw_url = str(pnode.get("urlForProductCatalog") or pnode.get("url") or "").strip()
                    href = urljoin("https://prom.ua", raw_url) if raw_url else (
                        f"https://prom.ua/ua/p{pid}-{url_text}.html" if pid else "https://prom.ua"
                    )
                    if href in seen_urls:
                        continue
                    seen_urls.add(href)

                    imgs = model_obj.get("images400x400") or model_obj.get("images") or pnode.get("images") or []
                    img_url = str(imgs[0]) if isinstance(imgs, list) and imgs else str(pnode.get("mainImage") or "")
                    comp = pnode.get("company") if isinstance(pnode.get("company"), dict) else {}
                    seller = str(comp.get("name") or "Prom Wholesale Supplier")

                    results.append({
                        "source": "prom_wholesale",
                        "supplier_type": "ua_wholesale",
                        "country": "UA",
                        "query": query,
                        "title": title[:180],
                        "price": round(min_ws, 2),
                        "currency": "UAH",
                        "price_usd": round(min_ws * uah_rate, 2),
                        "moq": min_moq,
                        "image_url": img_url,
                        "product_url": href,
                        "seller_name": seller[:100],
                        "tier_note": f"Опт від {min_moq} шт",
                        "is_live": True,
                    })
                start_idx = raw.find("{", start_idx + max(1, end_offset))
            except Exception:
                break
        if len(results) >= limit:
            return results[:limit]

    # 2. Fallback DOM cards ONLY if explicit wholesale qty tier text ("від N шт") appears on card
    for card in soup.select('[data-qaid="product_block"]'):
        card_text = card.get_text(" ", strip=True)
        tier_m = re.search(r"(\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*(?:₴|грн)[^\n]{0,24}від\s*(\d+)\s*шт", card_text, re.I)
        if not tier_m:
            continue
        title_el = card.select_one('[data-qaid="product_name"]')
        link_el = card.select_one('a[href*="/p"]') or card.select_one("a[href]")
        if not title_el or not link_el:
            continue
        href = urljoin("https://prom.ua", str(link_el["href"]))
        if href in seen_urls:
            continue
        price_val = _parse_float(tier_m.group(1))
        moq = max(2, int(tier_m.group(2)))
        if not price_val:
            continue
        img_el = card.find("img")
        img_url = str(img_el.get("src") or img_el.get("data-src") or "").strip() if img_el else ""
        seller_el = card.select_one('[data-qaid="company_name"]')
        seller = seller_el.get_text(" ", strip=True) if seller_el else "Prom Wholesale"
        seen_urls.add(href)
        results.append({
            "source": "prom_wholesale",
            "supplier_type": "ua_wholesale",
            "country": "UA",
            "query": query,
            "title": title_el.get_text(" ", strip=True)[:180],
            "price": price_val,
            "currency": "UAH",
            "price_usd": round(price_val * uah_rate, 2),
            "moq": moq,
            "image_url": img_url,
            "product_url": href,
            "seller_name": seller[:100],
            "tier_note": f"Опт від {moq} шт",
            "is_live": True,
        })
        if len(results) >= limit:
            break

    return results[:limit]


async def _fetch_image_dhash(url: Optional[str], http_client: httpx.AsyncClient) -> Optional[int]:
    """Download thumbnail bytes safely to compute Pillow dHash (returns None on error)."""
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        resp = await http_client.get(url, timeout=6.0)
        if resp.status_code == 200 and len(resp.content) <= 2_500_000:
            return compute_dhash(resp.content)
    except Exception:
        pass
    return None


async def fetch_ua_retail_benchmark(
    core_phrase: str,
    target_title: str,
    target_price_uah: Optional[float],
    settings: Settings,
    ref_image_url: Optional[str] = None,
    ref_dhash: Optional[int] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """Stage 5 Retail Benchmark: query Hotline.ua, Prom.ua, and OLX.ua gated by product_matcher."""
    clean_q = (core_phrase or target_title or "").strip()
    if not clean_q:
        return {"items": [], "signals": [], "retail_min_uah": None, "retail_median_uah": None, "retail_max_uah": None, "diagnostics": {}}

    q_enc = quote_plus(clean_q)
    q_slug = quote_plus(clean_q).replace("+", "-")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.7",
    }

    async def _run(http_client: httpx.AsyncClient) -> tuple[Optional[int], list[tuple[str, list[dict[str, Any]], str]]]:
        active_dhash = ref_dhash if ref_dhash is not None else await _fetch_image_dhash(ref_image_url, http_client)

        async def _get(plat: str, url: str, parser: Any) -> tuple[str, list[dict[str, Any]], str]:
            try:
                r = await http_client.get(url, headers=headers)
                if r.status_code != 200:
                    return plat, [], f"HTTP {r.status_code}"
                parsed = parser(r.text)
                return plat, parsed, f"raw: {len(parsed)}"
            except httpx.HTTPError as exc:
                return plat, [], f"error: {type(exc).__name__}"

        batches = await asyncio.gather(
            _get("hotline_ua", f"https://hotline.ua/ua/sr/?q={q_enc}", lambda h: parse_hotline_items(h, clean_q)),
            _get("prom_ua", f"https://prom.ua/ua/search?search_term={q_enc}", lambda h: parse_prom_items(h, clean_q)),
            _get("olx_ua", f"https://www.olx.ua/uk/list/q-{q_slug}/", lambda h: parse_olx_items(h, clean_q, domain="ua")),
        )
        return active_dhash, list(batches)

    if client is not None:
        resolved_dhash, batches = await _run(client)
    else:
        proxy = get_httpx_proxy(settings)
        async with httpx.AsyncClient(timeout=15.0, proxy=proxy, follow_redirects=True) as owned:
            resolved_dhash, batches = await _run(owned)

    ref_name = (target_title or clean_q).strip()
    verified: list[dict[str, Any]] = []
    signals: list[MarketplaceSignal] = []
    diagnostics: dict[str, str] = {}

    for plat, raw_items, status_msg in batches:
        plat_matched: list[dict[str, Any]] = []
        for it in raw_items:
            verdict = evaluate_product_match(
                ref_name,
                it["title"],
                ref_price_uah=target_price_uah,
                cand_price_uah=float(it["price"]),
                candidate_role="retail",
            )
            if not verdict.is_match:
                continue
            it["match_score"] = verdict.score
            it["match_label"] = verdict.label
            plat_matched.append(it)
            verified.append(it)

        diagnostics[plat] = f"matched: {len(plat_matched)}/{len(raw_items)}" if raw_items else status_msg
        if plat_matched:
            prices = sorted(float(x["price"]) for x in plat_matched)
            med = prices[len(prices) // 2]
            uah_rate = settings.FX_RATES_TO_USD.get("UAH", 0.024)
            signals.append(
                MarketplaceSignal(
                    source=plat,
                    platform=plat,
                    country="UA",
                    query=clean_q,
                    listing_count=len(plat_matched),
                    median_price=med,
                    min_price=prices[0],
                    max_price=prices[-1],
                    currency="UAH",
                    median_price_usd=round(med * uah_rate, 2),
                    sold_velocity_score=min(90.0, float(sum(int(x.get("offer_count") or 1) for x in plat_matched) * 8)),
                    seller_density=len(plat_matched),
                    seller_count=len(plat_matched),
                    top_sample_url=plat_matched[0].get("product_url"),
                    status="live",
                )
            )

    all_prices = sorted(float(x["price"]) for x in verified)
    return {
        "ref_dhash": resolved_dhash,
        "items": sorted(verified, key=lambda x: -float(x.get("match_score") or 0)),
        "signals": signals,
        "retail_min_uah": all_prices[0] if all_prices else None,
        "retail_median_uah": all_prices[len(all_prices) // 2] if all_prices else None,
        "retail_max_uah": all_prices[-1] if all_prices else None,
        "diagnostics": diagnostics,
    }
