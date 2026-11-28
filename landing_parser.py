import asyncio
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import httpx
from playwright.async_api import async_playwright

from config import (
    DEFAULT_FX_RATES_TO_USD,
    DROPSHIP_DOMAIN_TLDS,
    LandingPageData,
    make_product_placeholder_svg,
)
from product_matcher import extract_core_phrase, extract_model_tokens, is_valid_core_phrase

logger = logging.getLogger(__name__)

_SKIP_IMAGE_MARKERS: tuple[str, ...] = (
    "favicon",
    "logo",
    "sprite",
    "pixel",
    "1x1",
    "spacer",
    "badge",
    "icon",
    ".svg",
    "data:image/",
    "facebook.com/tr",
    "/tr?",
    "ev=pageview",
    "google-analytics",
    "googletagmanager",
    "mc.yandex",
    "doubleclick",
    "gtag",
    "tiktok.com",
    "analytics",
    "placeholder",
)
_INSTALLMENT_STRIP_RE = re.compile(
    r"(?:від|от)?\s*\d[\d\s\u00a0]*(?:[.,]\d{1,2})?\s*(?:₴|грн\.?|UAH)?\s*/\s*міс(?:\.|яц[ьі]?)?",
    re.IGNORECASE,
)
_DELIVERY_OR_PERCENT_STRIP_RE = re.compile(
    r"(?:доставка|передоплата|предоплата|економія|экономия|вигода)\s*(?:від|от)?\s*\d[\d\s\u00a0]*(?:[.,]\d{1,2})?\s*(?:₴|грн\.?|UAH|%)?",
    re.IGNORECASE,
)
_GENERIC_CATALOG_TITLES: frozenset[str] = frozenset({
    "бестселери",
    "бестселлеры",
    "каталог",
    "каталог товарів",
    "каталог товаров",
    "акційні товари",
    "акционные товары",
    "розпродаж",
    "распродажа",
    "новинки",
    "магазин",
    "головна",
    "главная",
    "всі товари",
    "все товары",
    "bestsellers",
    "catalog",
    "catalogue",
    "sale",
    "shop",
    "all products",
})
_CATALOG_PATH_RE = re.compile(
    r"/(?:collections?|catalog(?:ue)?|categor(?:y|ies)|filters?|sale|novinki|bestsellers?|courses?|webinar|afisha)(?:/|$)",
    re.I,
)
_FILTER_PATH_RE = re.compile(r"/(?:filters?)(?:/|$)", re.I)
_SINGLE_PRODUCT_PATH_RE = re.compile(r"/(?:p|product|item|offer)/\d+", re.I)
_SOFT_404_TITLE_RE = re.compile(
    r"(?:сторінк[ау]\s+не\s+знайдено|страница\s+не\s+найдена|товар\s+не\s+знайдено|page\s+not\s+found|\b404\b)",
    re.I,
)
_SOFT_404_BODY_RE = re.compile(
    r"(?:сторінк[ау]\s+не\s+знайдено|страница\s+не\s+найдена|товар\s+не\s+знайдено|page\s+not\s+found|\b(?:помилка|ошибка|error)\s*404\b)",
    re.I,
)
_COURSE_INFOPRODUCT_RE = re.compile(
    r"(?:\bкурс[иів]*\b|\bвебінар|\bнавчання\b|з\s+нуля\s+за|\bмодул[ьяі]\b|\bурок[іів]*\b|\bінтенсив|\bтариф[иі]?\b|\bспікер|\bментор|\bквитки\b|\bафіша\b|онлайн-курс)",
    re.I,
)
_UA_COD_MARKERS: tuple[str, ...] = (
    "нова пошта",
    "оплата при отриманні",
    "оплата після отримання",
    "оплата при получении",
    "післяплата",
    "накладений платіж",
    "відправка сьогодні",
    "відправка в день замовлення",
    "без передоплати",
    "швидке замовлення",
    "замовити зі знижкою",
    "до кінця акції",
)
_COD_SUBMIT_CUES: tuple[str, ...] = (
    "замовити",
    "оформити замовлення",
    "купити зараз",
    "залишити заявку",
    "отримати зі знижкою",
    "заказать",
)
_TITLE_PRICE_SUFFIX_RE = re.compile(
    r"\s*(?:\||—|–|-)\s*(?:\d[\d\s\u00a0]*(?:[.,]\d{1,2})?\s*(?:₴|грн\.?|UAH|\$|€|zł)|купити.*|купить.*|офіційний сайт.*)\s*$",
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


def _is_valid_dom_price(price: Optional[float], currency: Optional[str]) -> bool:
    """Reject 0/micro-prices such as shipping fees (e.g. 60-70 UAH) or discount percentages."""
    if price is None or price <= 0:
        return False
    code = (currency or "UAH").strip().upper()
    if code == "UAH":
        return price >= 100.0
    if code == "PLN":
        return price >= 15.0
    return price >= 3.0


def _extract_price_and_currency(soup: BeautifulSoup) -> tuple[Optional[float], Optional[str]]:
    """Extract offer price and currency from meta tags, JSON-LD, or DOM text."""
    # 1. OpenGraph / product meta tags
    meta_price = soup.find("meta", attrs={"property": re.compile(r"^(og|product):price:amount$", re.I)})
    meta_curr = soup.find("meta", attrs={"property": re.compile(r"^(og|product):price:currency$", re.I)})
    if meta_price and meta_price.get("content"):
        parsed_price = _parse_number(str(meta_price["content"]))
        parsed_curr = _normalize_currency(str(meta_curr["content"])) if meta_curr and meta_curr.get("content") else None
        if parsed_price is not None and _is_valid_dom_price(parsed_price, parsed_curr or "USD"):
            return parsed_price, parsed_curr

    # 2. JSON-LD offers
    ld_price, ld_curr, _ = _extract_from_json_ld(soup)
    if ld_price is not None and _is_valid_dom_price(ld_price, ld_curr or "USD"):
        return ld_price, ld_curr

    # 3. Targeted active price containers (skipping <del>/<s>/old-price nodes, /міс installments, and micro-prices)
    def _clean_price_text(raw: str) -> str:
        cleaned = _INSTALLMENT_STRIP_RE.sub(" ", raw)
        return _DELIVERY_OR_PERCENT_STRIP_RE.sub(" ", cleaned)

    candidates: list[str] = []
    for selector in (
        "[itemprop='price']",
        ".price-new",
        ".current-price",
        ".promo-price",
        ".new-price",
        ".product-price",
        ".offer-price",
        ".t-store__card__price",
        ".price",
    ):
        for node in soup.select(selector):
            if node.find_parent(["del", "s", "strike"]):
                continue
            cls_str = " ".join(node.get("class") or []).lower()
            if "old" in cls_str or "compare" in cls_str:
                continue
            text = node.get("content") or node.get_text(" ", strip=True)
            if text:
                candidates.append(_clean_price_text(str(text)))

    body_clone = BeautifulSoup(str(soup), "html.parser")
    for old_tag in body_clone.select("del, s, strike, .old-price, .price-old, [class*='old-price'], [class*='price-old'], [class*='compare']"):
        old_tag.decompose()
    candidates.append(_clean_price_text(body_clone.get_text(" ", strip=True)))

    for text in candidates:
        for currency_code, pattern in _CURRENCY_PATTERNS:
            for match in pattern.finditer(text):
                num_str = match.group("num1") or match.group("num2")
                parsed = _parse_number(num_str)
                if _is_valid_dom_price(parsed, currency_code):
                    return parsed, currency_code
    return None, None


def _clean_raw_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    cleaned = re.sub(r"\s+", " ", title).strip()
    cleaned = _TITLE_PRICE_SUFFIX_RE.sub("", cleaned).strip(" -|—–")
    return cleaned or None


def _is_generic_catalog_title(title: Optional[str]) -> bool:
    if not title:
        return True
    norm = re.sub(r"[^\w\sа-яіїєґ]", " ", title.lower()).strip()
    norm = re.sub(r"\s+", " ", norm)
    return norm in _GENERIC_CATALOG_TITLES


def _extract_title(soup: BeautifulSoup) -> Optional[str]:
    """Extract real product title preferring og:title, then h1, then <title>, skipping generic headers."""
    candidates: list[str] = []
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        t = _clean_raw_title(str(og_title["content"]))
        if t:
            candidates.append(t)
    for h1 in soup.find_all("h1"):
        t = _clean_raw_title(h1.get_text(" ", strip=True))
        if t:
            candidates.append(t)
    if soup.title:
        t = _clean_raw_title(soup.title.get_text(" ", strip=True))
        if t:
            candidates.append(t)

    for cand in candidates:
        if not _is_generic_catalog_title(cand) and is_valid_core_phrase(extract_core_phrase(cand, max_words=4), min_tokens=2):
            return cand
    for cand in candidates:
        if not _is_generic_catalog_title(cand):
            return cand
    return candidates[0] if candidates else None


def _is_usable_hero_img(url: str, img_tag: Any = None) -> bool:
    low = (url or "").strip().lower()
    if not low or any(m in low for m in _SKIP_IMAGE_MARKERS):
        return False
    if img_tag is not None:
        for attr in ("width", "height"):
            val = str(img_tag.get(attr) or "").strip()
            if val.isdigit() and int(val) <= 5:
                return False
    return True


def _extract_image_url(
    soup: BeautifulSoup,
    base_url: str,
    fallback_image_url: Optional[str] = None,
) -> Optional[str]:
    """Extract primary hero product image URL skipping pixels/icons/SVGs, with Tyver CDN fallback."""
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
            raw_src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-original")
                or img.get("data-lazy")
                or img.get("data-bg")
            )
            if not raw_src and img.get("srcset"):
                first_entry = str(img["srcset"]).split(",")[0].strip().split()
                raw_src = first_entry[0] if first_entry else None
            if not raw_src:
                continue
            resolved = urljoin(base_url, str(raw_src).strip())
            if _is_usable_hero_img(resolved, img_tag=img):
                return resolved

    if fallback_image_url and _is_usable_hero_img(fallback_image_url):
        return fallback_image_url
    return None


def detect_cod_lander_dna(
    html: str,
    url: str,
    soup: Optional[BeautifulSoup] = None,
    title: Optional[str] = None,
) -> dict[str, Any]:
    """Score single-product UA COD dropship DNA and flag generic multi-product catalogs."""
    active_soup = soup or BeautifulSoup(html or "", "html.parser")
    signals: list[str] = []
    score = 0

    # 1. COD <form> containing phone/tel input paired with either a name input OR a COD submit CTA button
    has_cod_form = False
    for form in active_soup.find_all("form"):
        has_phone = False
        has_name = False
        has_cod_submit = False
        for inp in form.find_all(["input", "textarea"]):
            inp_type = str(inp.get("type") or "").lower()
            if inp_type in ("submit", "button"):
                btn_val = str(inp.get("value") or "").lower()
                if any(cue in btn_val for cue in _COD_SUBMIT_CUES):
                    has_cod_submit = True
                continue
            if inp_type in ("hidden", "checkbox", "radio"):
                continue
            blob = " ".join(
                str(inp.get(k) or "") for k in ("type", "name", "id", "placeholder", "aria-label")
            ).lower()
            if inp_type == "tel" or any(k in blob for k in ("phone", "tel", "телефон", "моб")):
                has_phone = True
            if any(k in blob for k in ("name", "ім", "имя", "фіо", "фио", "pib", "client", "customer")):
                has_name = True
        if not has_cod_submit:
            for btn in form.find_all(["button", "a"]):
                if any(cue in btn.get_text(" ", strip=True).lower() for cue in _COD_SUBMIT_CUES):
                    has_cod_submit = True
                    break
        if has_phone and (has_name or has_cod_submit):
            has_cod_form = True
            break
    if has_cod_form:
        signals.append("cod_order_form")
        score += 40

    # 2. Strike-out old price + promo pricing
    has_old_price = bool(
        active_soup.select("del, s, strike, .old-price, .price-old, [class*='old-price'], [class*='price-old'], [class*='compare']")
    )
    if has_old_price:
        signals.append("strike_promo_price")
        score += 20

    # 3. Ukrainian COD / Nova Poshta markers
    body_text_lower = active_soup.get_text(" ", strip=True).lower()
    if any(marker in body_text_lower for marker in _UA_COD_MARKERS):
        signals.append("ua_cod_markers")
        score += 25

    # 4. Dropship URL / subdomain structure (e.g. massgun.domely.com.ua, testishop.shop/p/0179, legs.zario.space)
    parsed = urlparse((url or "").strip())
    host = parsed.netloc.lower().split(":")[0].removeprefix("www.")
    path = parsed.path or ""
    query_str = (parsed.query or "").lower()
    labels = [p for p in host.split(".") if p]
    is_subdomain_farm = (host.endswith(".com.ua") and len(labels) >= 4) or (not host.endswith(".com.ua") and len(labels) >= 3)
    has_dropship_tld = any(host.endswith(tld) for tld in DROPSHIP_DOMAIN_TLDS if tld != ".com.ua")
    is_single_url = bool(_SINGLE_PRODUCT_PATH_RE.search(path) or is_subdomain_farm or has_dropship_tld)
    if is_single_url:
        signals.append("dropship_url_dna")
        score += 15

    # Generic storefront/catalog, filter, soft-404, or course/infoproduct rejection
    extracted_title = title if title is not None else _extract_title(active_soup)
    h1_text = " ".join(h.get_text(" ", strip=True) for h in active_soup.find_all("h1"))
    title_blob = f"{extracted_title or ''} {h1_text}".strip()
    is_soft_404 = bool(_SOFT_404_TITLE_RE.search(title_blob) or _SOFT_404_BODY_RE.search(body_text_lower[:1200]))
    course_hits = _COURSE_INFOPRODUCT_RE.findall(f"{title_blob} {body_text_lower[:1800]}")
    is_course = bool(_COURSE_INFOPRODUCT_RE.search(title_blob) or len(set(h.lower() for h in course_hits)) >= 2)

    catalog_card_count = len(active_soup.select(".product-card, .catalog-item, .product-grid-item, .products-grid > *"))
    buy_buttons = [
        b.get_text(" ", strip=True).lower()
        for b in active_soup.find_all(["button", "a"])
        if any(k in b.get_text(" ", strip=True).lower() for k in ("купити", "в кошик", "add to cart"))
    ]
    is_filter_url = bool(_FILTER_PATH_RE.search(path) or ("page=" in query_str and not _SINGLE_PRODUCT_PATH_RE.search(path)))
    is_catalog_url = bool(_CATALOG_PATH_RE.search(path))
    is_generic_title = _is_generic_catalog_title(extracted_title)
    is_multi_product_grid = catalog_card_count >= 6 or len(buy_buttons) >= 6

    is_generic_catalog = bool(
        is_soft_404
        or is_course
        or is_filter_url
        or is_generic_title
        or (is_catalog_url and not (has_cod_form and is_single_url))
        or (is_multi_product_grid and not has_cod_form)
    )

    is_cod_lander = bool((has_cod_form or ("ua_cod_markers" in signals and score >= 40)) and not is_generic_catalog)
    return {
        "is_cod_lander": is_cod_lander,
        "cod_score": 0 if is_generic_catalog else min(100, score),
        "cod_signals": signals,
        "is_generic_catalog": is_generic_catalog,
        "is_soft_404": is_soft_404,
        "is_course": is_course,
    }


def parse_landing_html(
    html: str,
    url: str,
    fallback_image_url: Optional[str] = None,
) -> LandingPageData:
    """Parse HTML string into structured LandingPageData with core_phrase, model_code & COD DNA."""
    soup = BeautifulSoup(html, "html.parser")
    raw_title = _extract_title(soup)
    price, currency = _extract_price_and_currency(soup)
    image_url = _extract_image_url(soup, url, fallback_image_url=fallback_image_url)
    platform = detect_platform(html, url)
    dna = detect_cod_lander_dna(html, url, soup=soup, title=raw_title)

    core_phrase = extract_core_phrase(raw_title or "", max_words=4)
    if dna["is_generic_catalog"] or not is_valid_core_phrase(core_phrase, min_tokens=2):
        # Flag single-word or generic catalog titles so downstream arbitrage drops them cleanly.
        if _is_generic_catalog_title(raw_title):
            dna["is_generic_catalog"] = True
            dna["is_cod_lander"] = False

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
        is_cod_lander=dna["is_cod_lander"],
        cod_score=dna["cod_score"],
        cod_signals=dna["cod_signals"],
        is_generic_catalog=dna["is_generic_catalog"],
    )


async def fetch_landing_page_fast(
    url: str,
    client: Optional[httpx.AsyncClient] = None,
    timeout_s: float = 10.0,
    browser: Any = None,
    fallback_image_url: Optional[str] = None,
    allow_browser: bool = True,
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
                parsed = parse_landing_html(resp.text, str(resp.url), fallback_image_url=fallback_image_url)
                parsed.url = url
                parsed.final_url = str(resp.url)
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

    if not allow_browser:
        return parsed_fast or LandingPageData(
            url=url,
            final_url=url,
            image_url=fallback_image_url if fallback_image_url and _is_usable_hero_img(fallback_image_url) else None,
            platform="Custom PHP",
        )

    try:
        pw_res = await fetch_landing_page(url, timeout_ms=int(timeout_s * 1000), browser=browser)
        if not pw_res.image_url and fallback_image_url and _is_usable_hero_img(fallback_image_url):
            pw_res.image_url = fallback_image_url
        return pw_res
    except Exception:
        return parsed_fast or LandingPageData(
            url=url,
            final_url=url,
            image_url=fallback_image_url if fallback_image_url and _is_usable_hero_img(fallback_image_url) else None,
            platform="Custom PHP",
        )


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
