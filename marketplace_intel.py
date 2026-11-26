import asyncio
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
    get_httpx_proxy,
)
from storage import get_cache, set_cache

logger = logging.getLogger(__name__)

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
    text = soup.get_text(" ", strip=True)
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
