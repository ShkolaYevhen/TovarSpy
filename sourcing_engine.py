import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Optional
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup

from china_matcher import match_1688_suppliers
from config import (
    DEFAULT_FX_RATES_TO_USD,
    Settings,
    SupplierOffer,
    TelegramDropOffer,
    get_httpx_proxy,
    make_product_placeholder_svg,
)
from marketplace_intel import _fetch_image_dhash, fetch_ua_wholesale_offers, parse_prom_wholesale_tiers
from product_matcher import evaluate_product_match
from storage import get_cache, list_tg_channels, seed_default_channels, set_cache
from yml_feed_manager import search_yml_offers

logger = logging.getLogger(__name__)

_ANTI_BOT_MARKERS = (
    "captcha",
    "punish",
    "baxia",
    "x5sec",
    "verify you are human",
    "access denied",
)


def _is_blocked(html: str) -> bool:
    lowered = html.lower()
    return any(m in lowered for m in _ANTI_BOT_MARKERS)


def _to_float(raw: str) -> Optional[float]:
    cleaned = raw.replace("\u00a0", "").replace(" ", "").replace(",", ".").strip()
    try:
        val = float(cleaned)
        return round(val, 2) if val > 0 else None
    except ValueError:
        return None


def _matches_query(text: str, query: Optional[str]) -> bool:
    if not query or not query.strip():
        return True
    lowered = text.lower()
    q = query.strip().lower()
    if q in lowered:
        return True
    tokens = [t for t in re.split(r"\W+", q) if len(t) >= 3]
    if not tokens:
        return True
    return any(tok in lowered for tok in tokens)


def parse_telegram_channel_html(
    html: str,
    channel: str,
    query: Optional[str] = None,
) -> list[TelegramDropOffer]:
    """Parse public https://t.me/s/<channel> HTML posts into TelegramDropOffer items."""
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    wraps = soup.select("div.tgme_widget_message_wrap")
    uah_rate = DEFAULT_FX_RATES_TO_USD.get("UAH", 0.024)
    offers: list[TelegramDropOffer] = []

    for wrap in wraps:
        msg_node = wrap.select_one("[data-post]")
        data_post = str(msg_node["data-post"]) if msg_node and msg_node.get("data-post") else ""
        post_url = f"https://t.me/{data_post}" if data_post else f"https://t.me/{channel}"

        text_node = wrap.select_one(".tgme_widget_message_text")
        if not text_node:
            continue
        raw_text = text_node.get_text("\n", strip=True)
        if not raw_text or not _matches_query(raw_text, query):
            continue

        lines = [ln.strip(" \t•▪▫🔥✅📦💰-") for ln in raw_text.splitlines() if ln.strip()]
        title = lines[0][:140] if lines else "UA Drop Product"

        drop_match = re.search(
            r"(?:ціна\s*дроп|дроп\s*ціна|дроп|опт|drop)\s*[:\-–]?\s*(\d[\d\s]*(?:[.,]\d{1,2})?)\s*(?:грн|₴|uah)?",
            raw_text,
            re.I,
        )
        drop_uah = _to_float(drop_match.group(1)) if drop_match else None
        if drop_uah is None or drop_uah <= 0:
            continue

        rrc_match = re.search(
            r"(?:ррц|роздріб|рекомендована|ціна\s*продажу|цена\s*продажи)\s*[:\-–]?\s*(\d[\d\s]*(?:[.,]\d{1,2})?)\s*(?:грн|₴|uah)?",
            raw_text,
            re.I,
        )
        rrc_uah = _to_float(rrc_match.group(1)) if rrc_match else None
        if rrc_uah is None:
            margin_match = re.search(r"(?:маржа|націнка|прибуток)\s*[:\-–]?\s*(\d[\d\s]*(?:[.,]\d{1,2})?)", raw_text, re.I)
            margin_uah = _to_float(margin_match.group(1)) if margin_match else None
            if margin_uah:
                rrc_uah = round(drop_uah + margin_uah, 2)
        if rrc_uah is None:
            all_uah = [
                v for m in re.findall(r"(\d[\d\s]*(?:[.,]\d{1,2})?)\s*(?:грн|₴|uah)", raw_text, re.I)
                if (v := _to_float(m)) is not None and v > drop_uah
            ]
            rrc_uah = max(all_uah) if all_uah else round(drop_uah * 1.85, 2)

        photo_url = ""
        photo_wrap = wrap.select_one(".tgme_widget_message_photo_wrap")
        if photo_wrap and photo_wrap.get("style"):
            m_bg = re.search(r"url\(['\"]?(https?://[^'\")]+)['\"]?\)", str(photo_wrap["style"]))
            if m_bg:
                photo_url = m_bg.group(1)

        lowered = raw_text.lower()
        in_stock = not any(out in lowered for out in ("немає в наявності", "нет в наличии", "закінчився"))

        contact = ""
        at_match = re.search(r"(@[A-Za-z0-9_]{4,})", raw_text)
        if at_match:
            contact = at_match.group(1)
        else:
            for a in wrap.select("a[href*='t.me/']"):
                href = str(a["href"])
                if data_post and data_post in href:
                    continue
                contact = href
                break
        if not contact:
            contact = f"https://t.me/{channel}"

        offers.append(
            TelegramDropOffer(
                channel=channel,
                post_url=post_url,
                title=title,
                image_url=photo_url,
                drop_price_uah=round(drop_uah, 2),
                rrc_price_uah=round(rrc_uah, 2),
                drop_price_usd=round(drop_uah * uah_rate, 2),
                rrc_price_usd=round(rrc_uah * uah_rate, 2),
                in_stock=in_stock,
                supplier_contact=contact,
            )
        )
    return offers


def _mock_tg_offers(query: str) -> list[TelegramDropOffer]:
    uah_rate = DEFAULT_FX_RATES_TO_USD.get("UAH", 0.024)
    pool = [
        TelegramDropOffer(
            channel="demo_ua_drop",
            post_url="https://t.me/s/telegram",
            title="EMS Масажер для шиї Smart Neck Pro",
            image_url=make_product_placeholder_svg("EMS Neck Massager"),
            drop_price_uah=340.0,
            rrc_price_uah=990.0,
            drop_price_usd=round(340.0 * uah_rate, 2),
            rrc_price_usd=round(990.0 * uah_rate, 2),
            in_stock=True,
            supplier_contact="Demo Channel",
        ),
        TelegramDropOffer(
            channel="demo_ua_drop",
            post_url="https://t.me/s/telegram",
            title="Проекційна LED лампа Sunset Lamp 16 кольорів",
            image_url=make_product_placeholder_svg("Sunset LED Lamp"),
            drop_price_uah=260.0,
            rrc_price_uah=890.0,
            drop_price_usd=round(260.0 * uah_rate, 2),
            rrc_price_usd=round(890.0 * uah_rate, 2),
            in_stock=True,
            supplier_contact="Demo Channel",
        ),
        TelegramDropOffer(
            channel="demo_ua_drop",
            post_url="https://t.me/s/telegram",
            title="Міні автопилосос бездротовий 12000Pa",
            image_url=make_product_placeholder_svg("Mini Car Vacuum"),
            drop_price_uah=410.0,
            rrc_price_uah=1190.0,
            drop_price_usd=round(410.0 * uah_rate, 2),
            rrc_price_usd=round(1190.0 * uah_rate, 2),
            in_stock=True,
            supplier_contact="Demo Channel",
        ),
    ]
    matched = [o for o in pool if _matches_query(o.title, query)]
    return matched if matched else pool[:2]


async def search_telegram_dropshipping(
    query: str,
    settings: Settings,
    channels: Optional[list[str]] = None,
    client: Optional[httpx.AsyncClient] = None,
    mock: bool = False,
) -> list[TelegramDropOffer]:
    """Fetch public Ukrainian Telegram drop channels via https://t.me/s/<channel>."""
    if mock:
        return _mock_tg_offers(query)

    if channels is None:
        seed_default_channels(settings.DB_PATH, settings.DEFAULT_TG_CHANNELS)
        active = list_tg_channels(settings.DB_PATH, enabled_only=True)
        target_channels = [c["handle"] for c in active] or list(settings.DEFAULT_TG_CHANNELS)
    else:
        target_channels = channels

    if not target_channels:
        return []

    async def _fetch_channel(http_client: httpx.AsyncClient, handle: str) -> list[TelegramDropOffer]:
        cache_key = f"tg_chan:{handle.lower()}"
        html = get_cache(settings.DB_PATH, cache_key, settings.CACHE_TTL_SECONDS)
        if not isinstance(html, str):
            try:
                resp = await http_client.get(
                    f"https://t.me/s/{handle}",
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                )
                if resp.status_code != 200:
                    return []
                html = resp.text
                set_cache(settings.DB_PATH, cache_key, html)
            except httpx.HTTPError as exc:
                logger.warning("Telegram public channel fetch failed (%s): %s", handle, exc)
                return []
        return parse_telegram_channel_html(html, channel=handle, query=query)

    if client is not None:
        batches = await asyncio.gather(*(_fetch_channel(client, ch) for ch in target_channels))
    else:
        proxy = get_httpx_proxy(settings)
        async with httpx.AsyncClient(timeout=15.0, proxy=proxy, follow_redirects=True) as owned:
            batches = await asyncio.gather(*(_fetch_channel(owned, ch) for ch in target_channels))

    dedup: dict[tuple[str, str], TelegramDropOffer] = {}
    for batch in batches:
        for offer in batch:
            key = (offer.channel, offer.title.lower())
            if key not in dedup:
                dedup[key] = offer

    offers = list(dedup.values())
    offers.sort(key=lambda o: (o.rrc_price_uah - o.drop_price_uah), reverse=True)
    return offers


def parse_alibaba_html(html: str, query: str) -> list[SupplierOffer]:
    """Parse public Alibaba search HTML into SupplierOffer records."""
    if not html or _is_blocked(html):
        return []

    soup = BeautifulSoup(html, "html.parser")
    rmb_rate = DEFAULT_FX_RATES_TO_USD.get("RMB", 0.14)
    cards = soup.select(".organic-list .list-no-v2-outter, .fy23-search-card, [data-content='product']")
    if not cards:
        cards = [
            div for div in soup.find_all(["div", "article"])
            if "$" in div.get_text(" ", strip=True)
            and div.select_one("a[href*='alibaba.com']")
            and len(div.get_text(" ", strip=True)) < 1500
        ]

    offers: list[SupplierOffer] = []
    seen_urls: set[str] = set()
    for card in cards[:15]:
        text = card.get_text(" ", strip=True)
        price_match = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)", text)
        if not price_match:
            continue
        usd = float(price_match.group(1))
        if usd <= 0:
            continue

        link = card.find("a", href=True)
        if not link:
            continue
        href = str(link["href"])
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin("https://www.alibaba.com", href)
        if href in seen_urls:
            continue
        seen_urls.add(href)

        moq_match = re.search(r"(?:Min\.?\s*order|MOQ)\s*[:\-]?\s*(\d+)", text, re.I)
        moq = int(moq_match.group(1)) if moq_match else 1
        title_el = card.select_one("h2, .elements-title-normal, .search-card-e-title")
        title = title_el.get_text(" ", strip=True) if title_el else (link.get_text(" ", strip=True) or query)
        seller_el = card.select_one(".search-card-e-company, .company-name")
        seller = seller_el.get_text(" ", strip=True) if seller_el else "Alibaba Supplier"
        img_el = card.find("img", src=True)
        img_url = str(img_el["src"]) if img_el else None
        if img_url and img_url.startswith("//"):
            img_url = "https:" + img_url

        offers.append(
            SupplierOffer(
                supplier_name=seller[:100],
                product_title=title[:140],
                product_url=href,
                wholesale_price_rmb=round(usd / rmb_rate, 2),
                wholesale_price_usd=round(usd, 2),
                moq=max(1, moq),
                image_url=img_url,
                source="alibaba",
            )
        )
    offers.sort(key=lambda o: o.wholesale_price_usd)
    return offers[:5]


def _extract_aliexpress_content_items(html: str) -> list[dict[str, Any]]:
    """Decode live AliExpress _init_data_ itemList.content array or legacy itemList array."""
    dec = json.JSONDecoder()
    # Live shape: "itemList":{"content":[{...},...]}
    m_obj = re.search(r'"itemList"\s*:\s*(\{)', html)
    if m_obj:
        try:
            obj, _ = dec.raw_decode(html[m_obj.start(1):])
            if isinstance(obj, dict) and isinstance(obj.get("content"), list):
                return [x for x in obj["content"] if isinstance(x, dict)]
        except json.JSONDecodeError:
            pass

    # Legacy shape: "itemList":[{...},...]
    m_arr = re.search(r'"itemList"\s*:\s*(\[)', html)
    if m_arr:
        try:
            arr, _ = dec.raw_decode(html[m_arr.start(1):])
            if isinstance(arr, list):
                return [x for x in arr if isinstance(x, dict)]
        except json.JSONDecodeError:
            pass
    return []


def parse_aliexpress_html(html: str, query: str) -> list[SupplierOffer]:
    """Parse public AliExpress search HTML or embedded _init_data_ JSON into SupplierOffer records."""
    if not html or _is_blocked(html):
        return []

    rmb_rate = DEFAULT_FX_RATES_TO_USD.get("RMB", 0.14)
    offers: list[SupplierOffer] = []

    for item in _extract_aliexpress_content_items(html)[:20]:
        prices_block = item.get("prices") if isinstance(item.get("prices"), dict) else {}
        sale_price = prices_block.get("salePrice") if isinstance(prices_block.get("salePrice"), dict) else {}
        raw_price = sale_price.get("minPrice") or item.get("price") or item.get("salePrice") or item.get("minPrice")
        price_val = _to_float(str(raw_price)) if raw_price is not None else None
        if not price_val:
            continue

        currency_code = str(sale_price.get("currencyCode") or "USD").upper()
        fx = DEFAULT_FX_RATES_TO_USD.get(currency_code, 1.0)
        usd = round(price_val * fx, 2)

        title_obj = item.get("title")
        if isinstance(title_obj, dict):
            title = str(title_obj.get("displayTitle") or title_obj.get("seoTitle") or query)
        else:
            title = str(title_obj or query)

        img_obj = item.get("image")
        img_url = img_obj.get("imgUrl") if isinstance(img_obj, dict) else item.get("imageUrl")
        if isinstance(img_url, str) and img_url.startswith("//"):
            img_url = "https:" + img_url

        trade_obj = item.get("trade") if isinstance(item.get("trade"), dict) else {}
        sales_text = str(trade_obj.get("tradeDesc") or "")
        eval_obj = item.get("evaluation") if isinstance(item.get("evaluation"), dict) else {}
        star_rating = _to_float(str(eval_obj.get("starRating"))) if eval_obj.get("starRating") is not None else None
        if star_rating is not None:
            star_rating = max(0.0, min(5.0, star_rating))

        store_obj = item.get("store") if isinstance(item.get("store"), dict) else {}
        store_name = str(store_obj.get("storeName") or item.get("storeName") or "AliExpress Choice")

        item_id = item.get("productId") or item.get("id") or "0"
        href = str(item.get("productUrl") or f"https://www.aliexpress.com/item/{item_id}.html")
        if href.startswith("//"):
            href = "https:" + href

        offers.append(
            SupplierOffer(
                supplier_name=store_name[:100],
                product_title=title[:140],
                product_url=href,
                wholesale_price_rmb=round(usd / rmb_rate, 2),
                wholesale_price_usd=usd,
                rating=star_rating,
                moq=1,
                image_url=img_url if isinstance(img_url, str) else None,
                source="aliexpress",
                sales_count_text=sales_text,
                is_live=True,
            )
        )
    if offers:
        offers.sort(key=lambda o: o.wholesale_price_usd)
        return offers[:10]

    # 2. Fallback HTML card parsing
    soup = BeautifulSoup(html, "html.parser")
    for card in soup.select("a[href*='/item/']")[:10]:
        text = card.get_text(" ", strip=True)
        price_match = re.search(r"(?:US\s*)?\$\s*(\d+(?:\.\d{1,2})?)", text)
        if not price_match:
            continue
        usd = float(price_match.group(1))
        href = str(card["href"])
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin("https://www.aliexpress.com", href)
        offers.append(
            SupplierOffer(
                supplier_name="AliExpress Seller",
                product_title=(text[:120] or query),
                product_url=href,
                wholesale_price_rmb=round(usd / rmb_rate, 2),
                wholesale_price_usd=round(usd, 2),
                moq=1,
                source="aliexpress",
                is_live=True,
            )
        )
    offers.sort(key=lambda o: o.wholesale_price_usd)
    return offers[:10]


async def fetch_aliexpress_live(
    query: str,
    settings: Settings,
    limit: int = 8,
    client: Optional[httpx.AsyncClient] = None,
) -> list[SupplierOffer]:
    """Fetch live AliExpress search items with SQLite caching."""
    clean_q = (query or "trending gadget").strip()
    cache_key = f"ae_live:{clean_q.lower()}"
    cached = get_cache(settings.DB_PATH, cache_key, settings.CACHE_TTL_SECONDS)
    if isinstance(cached, list) and cached:
        return [SupplierOffer.model_validate(c) for c in cached][:limit]

    slug = quote_plus(clean_q).replace("+", "-")
    url = f"https://www.aliexpress.com/w/wholesale-{slug}.html"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    async def _run(http_client: httpx.AsyncClient) -> list[SupplierOffer]:
        try:
            resp = await http_client.get(url, headers=headers)
            if resp.status_code != 200:
                return []
            return parse_aliexpress_html(resp.text, clean_q)
        except httpx.HTTPError as exc:
            logger.warning("AliExpress live fetch failed (%s): %s", clean_q, exc)
            return []

    if client is not None:
        offers = await _run(client)
    else:
        proxy = get_httpx_proxy(settings)
        async with httpx.AsyncClient(timeout=15.0, proxy=proxy, follow_redirects=True) as owned:
            offers = await _run(owned)

    if offers:
        set_cache(settings.DB_PATH, cache_key, [o.model_dump(mode="json") for o in offers])
    return offers[:limit]


def parse_google_trends_rss(xml_text: str, geo: str = "UA") -> list[dict[str, str]]:
    """Parse Google Trends daily RSS XML into trending topic dicts."""
    if not xml_text or "<rss" not in xml_text.lower():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items: list[dict[str, str]] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        traffic = ""
        for child in item:
            if child.tag.endswith("approx_traffic") and child.text:
                traffic = child.text.strip()
                break
        items.append({"title": title, "traffic": traffic, "geo": geo.upper()})
    return items[:15]


async def fetch_google_trends_rss(
    geo: str = "UA",
    settings: Optional[Settings] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> list[dict[str, str]]:
    """Fetch public Google Trends RSS feed for a country code."""
    active_settings = settings or Settings()
    cache_key = f"gtrends:{geo.upper()}"
    cached = get_cache(active_settings.DB_PATH, cache_key, active_settings.CACHE_TTL_SECONDS)
    if isinstance(cached, list) and cached:
        return cached

    url = f"https://trends.google.com/trending/rss?geo={geo.upper()}"

    async def _run(http_client: httpx.AsyncClient) -> list[dict[str, str]]:
        try:
            resp = await http_client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                return []
            return parse_google_trends_rss(resp.text, geo=geo)
        except httpx.HTTPError as exc:
            logger.warning("Google Trends RSS failed (%s): %s", geo, exc)
            return []

    if client is not None:
        trends = await _run(client)
    else:
        proxy = get_httpx_proxy(active_settings)
        async with httpx.AsyncClient(timeout=12.0, proxy=proxy, follow_redirects=True) as owned:
            trends = await _run(owned)

    if trends:
        set_cache(active_settings.DB_PATH, cache_key, trends)
    return trends


async def _fetch_public_china_offers(
    query: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> list[SupplierOffer]:
    """Fetch Alibaba and AliExpress public search listings with caching."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    q_enc = quote_plus(query or "gadget")
    targets = [
        ("alibaba", f"https://www.alibaba.com/trade/search?SearchText={q_enc}", parse_alibaba_html),
        ("aliexpress", f"https://www.aliexpress.com/w/wholesale-{q_enc.replace('+', '-')}.html", parse_aliexpress_html),
    ]
    results: list[SupplierOffer] = []
    for name, url, parser in targets:
        try:
            resp = await http_client.get(url, headers=headers)
            if resp.status_code == 200:
                results.extend(parser(resp.text, query))
        except httpx.HTTPError as exc:
            logger.warning("Public %s fetch failed: %s", name, exc)
    results.sort(key=lambda o: o.wholesale_price_usd)
    return results[:5]


async def find_zero_cost_suppliers(
    product_title: str,
    image_url: Optional[str],
    settings: Settings,
    client: Optional[httpx.AsyncClient] = None,
    mock: bool = False,
) -> dict[str, Any]:
    """Route supplier discovery to Ukraine local wholesale + TG drops when SKIP_CHINA_SOURCING=True."""
    clean_title = (product_title or "trending gadget").strip()
    skip_china = bool(settings.SKIP_CHINA_SOURCING)
    cache_key = f"sourcing:{'ua' if skip_china else 'cn'}:{clean_title.lower()}:{(image_url or '').strip()}"
    if not mock:
        cached = get_cache(settings.DB_PATH, cache_key, settings.CACHE_TTL_SECONDS)
        if isinstance(cached, dict):
            china_offers = [SupplierOffer.model_validate(c) for c in cached.get("china_offers", [])]
            ua_wholesale = [SupplierOffer.model_validate(u) for u in cached.get("ua_wholesale_offers", [])]
            ua_offers = [TelegramDropOffer.model_validate(u) for u in cached.get("ua_drop_offers", [])]
            return {
                "ua_wholesale_offers": ua_wholesale,
                "best_ua_wholesale_offer": ua_wholesale[0] if ua_wholesale else None,
                "china_offers": china_offers,
                "ua_drop_offers": ua_offers,
                "best_china_offer": china_offers[0] if china_offers else None,
                "best_ua_drop_offer": ua_offers[0] if ua_offers else None,
            }

    async def _gather_all(
        http_client: httpx.AsyncClient,
    ) -> tuple[list[SupplierOffer], list[SupplierOffer], list[TelegramDropOffer]]:
        tg_task = search_telegram_dropshipping(clean_title, settings, client=http_client, mock=mock)
        if mock:
            china_task = match_1688_suppliers(image_url or "", settings, client=http_client, mock=True)
            china_1688, ua_offers = await asyncio.gather(china_task, tg_task)
            return [], china_1688, ua_offers

        if skip_china:
            ua_ws_task = fetch_ua_wholesale_offers(clean_title, settings, client=http_client)
            (ua_wholesale, _diag), ua_offers = await asyncio.gather(ua_ws_task, tg_task)
            return ua_wholesale[:6], [], ua_offers

        china_task = match_1688_suppliers(image_url or "", settings, client=http_client, mock=False)
        public_task = _fetch_public_china_offers(clean_title, settings, http_client)
        china_1688, public_china, ua_offers = await asyncio.gather(china_task, public_task, tg_task)
        combined_china = china_1688 + public_china
        combined_china.sort(key=lambda o: o.wholesale_price_usd)
        return [], combined_china[:5], ua_offers

    if client is not None:
        ua_wholesale, china_offers, ua_offers = await _gather_all(client)
    else:
        proxy = get_httpx_proxy(settings)
        async with httpx.AsyncClient(timeout=15.0, proxy=proxy, follow_redirects=True) as owned:
            ua_wholesale, china_offers, ua_offers = await _gather_all(owned)

    if not china_offers and not mock and not skip_china:
        rmb_rate = settings.FX_RATES_TO_USD.get("RMB", 0.14)
        est_usd = 4.20
        china_offers = [
            SupplierOffer(
                supplier_name="Estimated 1688 Benchmark",
                product_title=clean_title,
                product_url=f"https://www.alibaba.com/trade/search?SearchText={quote_plus(clean_title)}",
                wholesale_price_rmb=round(est_usd / rmb_rate, 2),
                wholesale_price_usd=est_usd,
                moq=5,
                source="heuristic_estimate",
                is_live=False,
            )
        ]

    if not mock:
        set_cache(
            settings.DB_PATH,
            cache_key,
            {
                "ua_wholesale_offers": [u.model_dump(mode="json") for u in ua_wholesale],
                "china_offers": [c.model_dump(mode="json") for c in china_offers],
                "ua_drop_offers": [u.model_dump(mode="json") for u in ua_offers],
            },
        )

    return {
        "ua_wholesale_offers": ua_wholesale,
        "best_ua_wholesale_offer": ua_wholesale[0] if ua_wholesale else None,
        "china_offers": china_offers,
        "ua_drop_offers": ua_offers,
        "best_china_offer": china_offers[0] if china_offers else None,
        "best_ua_drop_offer": ua_offers[0] if ua_offers else None,
    }


async def find_suppliers(
    product_title: str,
    image_url: Optional[str],
    settings: Settings,
    mock: bool = False,
) -> list[SupplierOffer]:
    """Compatibility helper returning China supplier offers."""
    res = await find_zero_cost_suppliers(product_title, image_url, settings, mock=mock)
    return res["china_offers"]


async def find_verified_ua_wholesale(
    core_phrase: str,
    target_title: str,
    target_price_uah: Optional[float],
    settings: Settings,
    *,
    ref_image_url: Optional[str] = None,
    ref_dhash: Optional[int] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """Stage 5 Real UA Wholesale: Prom real wholesalePrices tiers + YML feeds + TG drop channels only."""
    clean_q = (core_phrase or target_title or "").strip()
    ref_name = (target_title or clean_q).strip()
    if not clean_q:
        return {
            "offers": [],
            "tg_offers": [],
            "wholesale_match_status": "none",
            "wholesale_match_note": "No verified wholesale match yet",
            "diagnostics": {},
        }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.7",
    }
    uah_rate = settings.FX_RATES_TO_USD.get("UAH", 0.024)

    async def _fetch_prom_tiers(http_client: httpx.AsyncClient) -> tuple[list[dict[str, Any]], str]:
        url = f"https://prom.ua/ua/search?search_term={quote_plus(clean_q)}&sale_type=wholesale"
        try:
            resp = await http_client.get(url, headers=headers)
            if resp.status_code != 200:
                return [], f"HTTP {resp.status_code}"
            if _is_blocked(resp.text):
                return [], "blocked"
            parsed = parse_prom_wholesale_tiers(resp.text, clean_q, limit=10)
            return parsed, f"tiers: {len(parsed)}" if parsed else "0 real wholesalePrices"
        except httpx.HTTPError as exc:
            return [], f"error: {type(exc).__name__}"

    async def _run(http_client: httpx.AsyncClient) -> tuple[Optional[int], list[dict[str, Any]], str, list[SupplierOffer], str, list[TelegramDropOffer]]:
        active_dhash = ref_dhash if ref_dhash is not None else await _fetch_image_dhash(ref_image_url, http_client)
        prom_task = _fetch_prom_tiers(http_client)
        yml_task = search_yml_offers(clean_q, ref_name, target_price_uah, settings, ref_dhash=active_dhash, client=http_client)
        tg_task = search_telegram_dropshipping(clean_q, settings, client=http_client)
        (prom_raw, prom_diag), (yml_offers, yml_diag), tg_raw = await asyncio.gather(prom_task, yml_task, tg_task)
        return active_dhash, prom_raw, prom_diag, yml_offers, yml_diag, tg_raw

    if client is not None:
        resolved_dhash, prom_raw, prom_diag, yml_offers, yml_diag, tg_raw = await _run(client)
    else:
        proxy = get_httpx_proxy(settings)
        async with httpx.AsyncClient(timeout=15.0, proxy=proxy, follow_redirects=True) as owned:
            resolved_dhash, prom_raw, prom_diag, yml_offers, yml_diag, tg_raw = await _run(owned)

    verified: list[SupplierOffer] = []

    # 1. Verify Prom.ua real wholesalePrices tiers (with optional visual dHash check)
    async def _score_prom(it: dict[str, Any], http_client: httpx.AsyncClient) -> Optional[SupplierOffer]:
        cand_dhash = None
        if resolved_dhash is not None and it.get("image_url"):
            cand_dhash = await _fetch_image_dhash(it["image_url"], http_client)
        verdict = evaluate_product_match(
            ref_name,
            it["title"],
            ref_price_uah=target_price_uah,
            cand_price_uah=float(it["price"]),
            candidate_role="wholesale",
            ref_dhash=resolved_dhash,
            cand_dhash=cand_dhash,
        )
        if not verdict.is_match:
            return None
        return SupplierOffer(
            supplier_name=str(it.get("seller_name") or "Prom Wholesale"),
            product_title=str(it["title"]),
            product_url=str(it["product_url"]),
            wholesale_price_rmb=0.0,
            wholesale_price_usd=round(float(it["price"]) * uah_rate, 2),
            wholesale_price_uah=round(float(it["price"]), 2),
            moq=max(2, int(it.get("moq") or 2)),
            image_url=it.get("image_url") or None,
            source="prom_wholesale",
            supplier_type="ua_wholesale",
            sales_count_text=str(it.get("tier_note") or "Prom Опт"),
            match_score=verdict.score,
            match_label=verdict.label,
            is_live=True,
        )

    if prom_raw:
        if client is not None:
            prom_scored = await asyncio.gather(*(_score_prom(it, client) for it in prom_raw))
        else:
            proxy = get_httpx_proxy(settings)
            async with httpx.AsyncClient(timeout=10.0, proxy=proxy, follow_redirects=True) as owned:
                prom_scored = await asyncio.gather(*(_score_prom(it, owned) for it in prom_raw))
        verified.extend(o for o in prom_scored if o is not None)

    # 2. YML verified offers
    verified.extend(yml_offers)

    # 3. Verify UA Telegram drop offers via product_matcher
    matched_tg: list[TelegramDropOffer] = []
    for tg in tg_raw:
        verdict = evaluate_product_match(
            ref_name,
            tg.title,
            ref_price_uah=target_price_uah,
            cand_price_uah=tg.drop_price_uah,
            candidate_role="wholesale",
        )
        if not verdict.is_match:
            continue
        matched_tg.append(tg)
        verified.append(
            SupplierOffer(
                supplier_name=f"@{tg.channel} (TG Drop)",
                product_title=tg.title,
                product_url=tg.post_url,
                wholesale_price_rmb=0.0,
                wholesale_price_usd=tg.drop_price_usd or round(tg.drop_price_uah * uah_rate, 2),
                wholesale_price_uah=tg.drop_price_uah,
                moq=1,
                image_url=tg.image_url or None,
                source="telegram_drop",
                supplier_type="ua_wholesale",
                sales_count_text=f"Drop (RRP {tg.rrc_price_uah:.0f}₴)",
                match_score=verdict.score,
                match_label=verdict.label,
                is_live=True,
            )
        )

    verified.sort(key=lambda o: (-o.match_score, o.wholesale_price_uah or 999999.0))
    verified = verified[:6]
    status = "verified" if verified else "none"
    note = (
        f"{len(verified)} verified UA wholesale match(es)"
        if verified
        else "No verified wholesale match yet"
    )
    diagnostics = {
        "prom_wholesale": f"verified: {sum(1 for v in verified if v.source == 'prom_wholesale')} ({prom_diag})",
        "yml_feeds": yml_diag,
        "telegram": f"verified: {len(matched_tg)}" if matched_tg else "0 verified TG drops",
    }
    return {
        "offers": verified,
        "tg_offers": matched_tg[:4],
        "wholesale_match_status": status,
        "wholesale_match_note": note,
        "diagnostics": diagnostics,
    }
