import asyncio
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import httpx
from playwright.async_api import async_playwright

from config import DEFAULT_FX_RATES_TO_USD, LandingPageData, make_product_placeholder_svg
from product_matcher import extract_core_phrase, extract_model_tokens

logger = logging.getLogger(__name__)

_SKIP_IMAGE_MARKERS: tuple[str, ...] = (
    "favicon", "logo", "sprite", "pixel", "1x1", "spacer", "badge", "icon", ".svg", "data:image/svg",
)
_INSTALLMENT_STRIP_RE = re.compile(
    r"(?:від|от)?\s*\d[\d\s\u00a0]*(?:[.,]\d{1,2})?\s*(?:₴|грн\.?|UAH)?\s*/\s*міс(?:\.|яц[ьі]?)?",
    re.IGNORECASE,
)

_CURRENCY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "UAH",
        re.compile(
            r"(?:(?P<pre>₴|грн\.?|UAH)\s*(?P<num1>\d[\d\s\u00a0]*(?:[.,]\d{1,2})?))|"
            r"(?:(?P<num2>\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*(?P<post>₴|грн\.?|UAH))",
            re.IGNORECASE,
        ),
    ),
    (
        "PLN",
        re.compile(
            r"(?:(?P<pre>zł|zl|PLN)\s*(?P<num1>\d[\d\s\u00a0]*(?:[.,]\d{1,2})?))|"
            r"(?:(?P<num2>\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*(?P<post>zł|zl|PLN))",
            re.IGNORECASE,
        ),
    ),
    (
        "EUR",
        re.compile(
            r"(?:(?P<pre>€|EUR)\s*(?P<num1>\d[\d\s\u00a0]*(?:[.,]\d{1,2})?))|"
            r"(?:(?P<num2>\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*(?P<post>€|EUR))",
            re.IGNORECASE,
        ),
    ),
    (
        "USD",
        re.compile(
            r"(?:(?P<pre>\$|USD)\s*(?P<num1>\d[\d\s\u00a0]*(?:[.,]\d{1,2})?))|"
            r"(?:(?P<num2>\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*(?P<post>\$|USD))",
            re.IGNORECASE,
        ),
    ),
]


def _parse_number(raw: str) -> Optional[float]:
    """Normalize localized price string (spaces, comma decimals) to float."""
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
        return round(float(cleaned), 2)
    except ValueError:
        return None


def _normalize_currency(raw: Optional[str]) -> Optional[str]:
    """Map currency symbols and aliases to canonical ISO-like codes."""
    if not raw:
        return None
    val = raw.strip().upper()
    mapping = {
        "₴": "UAH",
        "ГРН": "UAH",
        "ГРН.": "UAH",
        "UAH": "UAH",
        "$": "USD",
        "USD": "USD",
        "€": "EUR",
        "EUR": "EUR",
        "ZŁ": "PLN",
        "ZL": "PLN",
        "PLN": "PLN",
    }
    return mapping.get(val, val if val in {"UAH", "USD", "EUR", "PLN"} else None)


def detect_platform(html: str, url: str) -> str:
    """Fingerprint e-commerce platform from HTML markers and URL."""
    lowered_html = html.lower()
    lowered_url = url.lower()

    if "cdn.shopify.com" in lowered_html or "shopify.theme" in lowered_html or "myshopify.com" in lowered_url:
        return "Shopify"
    if "horoshop" in lowered_html or "cartum" in lowered_html:
        return "Cartum/Horoshop"
    if "tildacdn" in lowered_html or "t-records" in lowered_html or "data-tilda-project-id" in lowered_html or "tilda.ws" in lowered_url:
        return "Tilda"
    if ".php" in lowered_url or "phpsessid" in lowered_html or ".php" in lowered_html:
        return "Custom PHP"
    return "Custom PHP"


def _extract_from_json_ld(soup: BeautifulSoup) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """Extract price, currency, and image URL from JSON-LD product schema if present."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            offers = item.get("offers")
            if isinstance(offers, list) and offers:
                offers = offers[0]
            price = None
            currency = None
            if isinstance(offers, dict):
                raw_price = offers.get("price")
                if raw_price is not None:
                    price = _parse_number(str(raw_price))
                currency = _normalize_currency(str(offers.get("priceCurrency", "")))
            img = item.get("image")
            if isinstance(img, list) and img:
                img = img[0]
            img_url = str(img) if isinstance(img, str) else None
            if price is not None or img_url:
                return price, currency, img_url
    return None, None, None


def _extract_price_and_currency(soup: BeautifulSoup) -> tuple[Optional[float], Optional[str]]:
    """Extract offer price and currency from meta tags, JSON-LD, or DOM text."""
    # 1. OpenGraph / product meta tags
    meta_price = soup.find("meta", attrs={"property": re.compile(r"^(og|product):price:amount$", re.I)})
    meta_curr = soup.find("meta", attrs={"property": re.compile(r"^(og|product):price:currency$", re.I)})
    if meta_price and meta_price.get("content"):
        parsed_price = _parse_number(str(meta_price["content"]))
        parsed_curr = _normalize_currency(str(meta_curr["content"])) if meta_curr and meta_curr.get("content") else None
        if parsed_price is not None:
            return parsed_price, parsed_curr

    # 2. JSON-LD offers
    ld_price, ld_curr, _ = _extract_from_json_ld(soup)
    if ld_price is not None:
        return ld_price, ld_curr

    # 3. Targeted active price containers (skipping <del>/<s>/old-price nodes and /міс installments)
    candidates: list[str] = []
    for selector in (
        "[itemprop='price']",
        ".price-new",
        ".current-price",
        ".product-price",
        ".offer-price",
        ".t-store__card__price",
        ".price",
    ):
        for node in soup.select(selector):
            text = node.get("content") or node.get_text(" ", strip=True)
            if text:
                candidates.append(_INSTALLMENT_STRIP_RE.sub(" ", str(text)))

    body_clone = BeautifulSoup(str(soup), "html.parser")
    for old_tag in body_clone.select("del, s, strike, .old-price, .price-old, [class*='old-price'], [class*='compare']"):
        old_tag.decompose()
    candidates.append(_INSTALLMENT_STRIP_RE.sub(" ", body_clone.get_text(" ", strip=True)))

    for text in candidates:
        for currency_code, pattern in _CURRENCY_PATTERNS:
            match = pattern.search(text)
            if match:
                num_str = match.group("num1") or match.group("num2")
                parsed = _parse_number(num_str)
                if parsed is not None:
                    return parsed, currency_code
    return None, None


def _extract_title(soup: BeautifulSoup) -> Optional[str]:
    """Extract product title preferring og:title, then h1, then <title>."""
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = str(og_title["content"]).strip()
        if title:
            return title
    h1 = soup.find("h1")
    if h1:
        text = h1.get_text(" ", strip=True)
        if text:
            return text
    if soup.title:
        text = soup.title.get_text(" ", strip=True)
        if text:
            return text
    return None


def _is_usable_hero_img(url: str) -> bool:
    low = (url or "").strip().lower()
    if not low or any(m in low for m in _SKIP_IMAGE_MARKERS):
        return False
    return True


def _extract_image_url(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    """Extract primary hero product image URL skipping icons/logos/tracking pixels."""
    og_img = soup.find("meta", attrs={"property": "og:image"})
    if og_img and og_img.get("content"):
        resolved = urljoin(base_url, str(og_img["content"]).strip())
        if _is_usable_hero_img(resolved):
            return resolved

    _, _, ld_img = _extract_from_json_ld(soup)
    if ld_img:
        resolved = urljoin(base_url, ld_img.strip())
        if _is_usable_hero_img(resolved):
            return resolved

    for selector in ("img.product-image", ".product-slider img", ".gallery img", "main img", "img"):
        for img in soup.select(selector):
            raw_src = img.get("src") or img.get("data-src") or img.get("data-original")
            if not raw_src:
                continue
            resolved = urljoin(base_url, str(raw_src).strip())
            if _is_usable_hero_img(resolved):
                return resolved
    return None


def parse_landing_html(html: str, url: str) -> LandingPageData:
    """Parse HTML string into structured LandingPageData with core_phrase & model_code."""
    soup = BeautifulSoup(html, "html.parser")
    raw_title = _extract_title(soup)
    price, currency = _extract_price_and_currency(soup)
    image_url = _extract_image_url(soup, url)
    platform = detect_platform(html, url)

    core_phrase = extract_core_phrase(raw_title or "", max_words=4)
    models = sorted(extract_model_tokens(raw_title or ""))
    model_code = models[0] if models else ""

    return LandingPageData(
        url=url,
        final_url=url,
        title=raw_title,
        price=price,
        currency=currency,
        image_url=image_url,
        platform=platform,
        core_phrase=core_phrase,
        model_code=model_code,
    )


async def fetch_landing_page_fast(
    url: str,
    client: Optional[httpx.AsyncClient] = None,
    timeout_s: float = 10.0,
    browser: Any = None,
) -> LandingPageData:
    """Try fast HTTPX fetch first, falling back to Playwright only when HTML lacks title/price."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.7",
    }

    async def _get_html(http_client: httpx.AsyncClient) -> Optional[LandingPageData]:
        try:
            resp = await http_client.get(url, headers=headers)
            if resp.status_code == 200 and resp.text:
                parsed = parse_landing_html(resp.text, str(resp.url))
                parsed.url = url
                parsed.final_url = str(resp.url)
                if parsed.title and parsed.price:
                    return parsed
                return parsed
        except httpx.HTTPError:
            pass
        return None

    parsed_fast: Optional[LandingPageData] = None
    if client is not None:
        parsed_fast = await _get_html(client)
    else:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as owned:
            parsed_fast = await _get_html(owned)

    if parsed_fast is not None and parsed_fast.title and (parsed_fast.price or parsed_fast.image_url):
        return parsed_fast

    try:
        return await fetch_landing_page(url, timeout_ms=int(timeout_s * 1000), browser=browser)
    except Exception:
        return parsed_fast or LandingPageData(url=url, final_url=url, platform="Custom PHP")


def to_uah(price: Optional[float], currency: Optional[str], usd_to_uah: float = 41.67) -> Optional[float]:
    """Convert any parsed landing price to UAH using DEFAULT_FX_RATES_TO_USD."""
    if price is None or price <= 0:
        return None
    code = (currency or "UAH").strip().upper()
    if code == "UAH":
        return round(price, 2)
    fx = DEFAULT_FX_RATES_TO_USD.get(code)
    if fx is None:
        return None
    return round(price * fx * usd_to_uah, 2)


async def fetch_landing_page(
    url: str,
    timeout_ms: int = 15000,
    browser: Any = None,
) -> LandingPageData:
    """Load a landing page with headless Playwright Chromium and extract metadata."""
    async def _scrape_with_browser(active_browser: Any) -> LandingPageData:
        context = await active_browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(4000, timeout_ms))
            except Exception:
                pass
            final_url = page.url or url
            html = await page.content()
            parsed = parse_landing_html(html, final_url)
            parsed.url = url
            parsed.final_url = final_url
            return parsed
        finally:
            await page.close()
            await context.close()

    if browser is not None:
        return await _scrape_with_browser(browser)

    async with async_playwright() as p:
        launched = await p.chromium.launch(headless=True)
        try:
            return await _scrape_with_browser(launched)
        finally:
            await launched.close()


_MOCK_LANDINGS: dict[str, dict[str, Any]] = {
    "smartrelax-shop.pl": {
        "title": "EMS Smart Neck Massager Pro",
        "price": 149.0,
        "currency": "PLN",
        "image_url": make_product_placeholder_svg("EMS Neck Massager"),
        "platform": "Shopify",
    },
    "homeglow.com.ua": {
        "title": "Sunset Projection LED Lamp 16 Colors",
        "price": 1190.0,
        "currency": "UAH",
        "image_url": make_product_placeholder_svg("Sunset LED Lamp"),
        "platform": "Cartum/Horoshop",
    },
    "autocleanpro.com": {
        "title": "Wireless Mini Car Vacuum 12000Pa",
        "price": 34.99,
        "currency": "USD",
        "image_url": make_product_placeholder_svg("Mini Car Vacuum"),
        "platform": "Shopify",
    },
    "posturefit.tilda.ws": {
        "title": "Magnetic Smart Posture Corrector",
        "price": 89.0,
        "currency": "PLN",
        "image_url": make_product_placeholder_svg("Posture Corrector"),
        "platform": "Tilda",
    },
}


def _mock_landing_for_url(url: str) -> LandingPageData:
    """Return deterministic LandingPageData for offline dry-run execution."""
    for domain, data in _MOCK_LANDINGS.items():
        if domain in url:
            return LandingPageData(url=url, final_url=url, **data)
    return LandingPageData(
        url=url,
        final_url=url,
        title="Trending E-Commerce Gadget",
        price=29.99,
        currency="USD",
        image_url=make_product_placeholder_svg("Trending Gadget"),
        platform="Custom PHP",
    )


async def parse_landings(
    urls: list[str],
    mock: bool = False,
    timeout_ms: int = 15000,
    browser: Any = None,
) -> list[LandingPageData]:
    """Batch-parse landing page URLs using Playwright or deterministic mock fixtures."""
    if not urls:
        return []
    if mock:
        return [_mock_landing_for_url(u) for u in urls]

    if browser is not None:
        return list(
            await asyncio.gather(*(fetch_landing_page(u, timeout_ms=timeout_ms, browser=browser) for u in urls))
        )

    async with async_playwright() as p:
        launched = await p.chromium.launch(headless=True)
        try:
            results: list[LandingPageData] = []
            for url in urls:
                try:
                    results.append(await fetch_landing_page(url, timeout_ms=timeout_ms, browser=launched))
                except Exception as exc:
                    logger.warning("Failed to parse landing %s: %s", url, exc)
                    results.append(LandingPageData(url=url, final_url=url, platform="Custom PHP"))
            return results
        finally:
            await launched.close()
