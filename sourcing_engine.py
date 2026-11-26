import asyncio
import json
import logging
import re
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
)
from storage import get_cache, list_tg_channels, seed_default_channels, set_cache

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
            channel="drop_ua",
            post_url="https://t.me/drop_ua/4102",
            title="EMS Масажер для шиї Smart Neck Pro",
            image_url="https://cdn.example.com/products/ems-neck-massager.jpg",
            drop_price_uah=340.0,
            rrc_price_uah=990.0,
            drop_price_usd=round(340.0 * uah_rate, 2),
            rrc_price_usd=round(990.0 * uah_rate, 2),
            in_stock=True,
            supplier_contact="@drop_ua_manager",
        ),
        TelegramDropOffer(
            channel="opt_drop_ua",
            post_url="https://t.me/opt_drop_ua/8831",
            title="Проекційна LED лампа Sunset Lamp 16 кольорів",
            image_url="https://cdn.example.com/products/sunset-lamp.jpg",
            drop_price_uah=260.0,
            rrc_price_uah=890.0,
            drop_price_usd=round(260.0 * uah_rate, 2),
            rrc_price_usd=round(890.0 * uah_rate, 2),
            in_stock=True,
            supplier_contact="@opt_drop_support",
        ),
        TelegramDropOffer(
            channel="tovarka_ua_opt",
            post_url="https://t.me/tovarka_ua_opt/5120",
            title="Міні автопилосос бездротовий 12000Pa",
            image_url="https://cdn.example.com/products/car-vacuum.jpg",
            drop_price_uah=410.0,
            rrc_price_uah=1190.0,
            drop_price_usd=round(410.0 * uah_rate, 2),
            rrc_price_usd=round(1190.0 * uah_rate, 2),
            in_stock=True,
            supplier_contact="https://t.me/tovarka_ua_opt",
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


def parse_aliexpress_html(html: str, query: str) -> list[SupplierOffer]:
    """Parse public AliExpress search HTML or embedded JSON into SupplierOffer records."""
    if not html or _is_blocked(html):
        return []

    rmb_rate = DEFAULT_FX_RATES_TO_USD.get("RMB", 0.14)
    offers: list[SupplierOffer] = []

    # 1. Try embedded JSON itemList / _dida_config_
    for script_match in re.findall(r'"itemList"\s*:\s*(\[\{.*?\}\])', html, re.S):
        try:
            items = json.loads(script_match)
        except json.JSONDecodeError:
            continue
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            price_val = item.get("price") or item.get("salePrice") or item.get("minPrice")
            usd = _to_float(str(price_val)) if price_val is not None else None
            if not usd:
                continue
            item_id = item.get("productId") or item.get("id") or "0"
            href = str(item.get("productUrl") or f"https://www.aliexpress.com/item/{item_id}.html")
            if href.startswith("//"):
                href = "https:" + href
            offers.append(
                SupplierOffer(
                    supplier_name=str(item.get("storeName") or "AliExpress Seller"),
                    product_title=str(item.get("title") or query)[:140],
                    product_url=href,
                    wholesale_price_rmb=round(usd / rmb_rate, 2),
                    wholesale_price_usd=round(usd, 2),
                    moq=1,
                    image_url=item.get("imageUrl"),
                    source="aliexpress",
                )
            )
        if offers:
            offers.sort(key=lambda o: o.wholesale_price_usd)
            return offers[:5]

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
            )
        )
    offers.sort(key=lambda o: o.wholesale_price_usd)
    return offers[:5]


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
    """Combine zero-API China sourcing, cached 1688 RapidAPI, and UA Telegram drop channels."""
    clean_title = (product_title or "trending gadget").strip()
    cache_key = f"sourcing:{clean_title.lower()}:{(image_url or '').strip()}"
    if not mock:
        cached = get_cache(settings.DB_PATH, cache_key, settings.CACHE_TTL_SECONDS)
        if isinstance(cached, dict):
            china_offers = [SupplierOffer.model_validate(c) for c in cached.get("china_offers", [])]
            ua_offers = [TelegramDropOffer.model_validate(u) for u in cached.get("ua_drop_offers", [])]
            return {
                "china_offers": china_offers,
                "ua_drop_offers": ua_offers,
                "best_china_offer": china_offers[0] if china_offers else None,
                "best_ua_drop_offer": ua_offers[0] if ua_offers else None,
            }

    async def _gather_all(http_client: httpx.AsyncClient) -> tuple[list[SupplierOffer], list[TelegramDropOffer]]:
        china_task = match_1688_suppliers(image_url or "", settings, client=http_client, mock=mock)
        tg_task = search_telegram_dropshipping(clean_title, settings, client=http_client, mock=mock)
        if mock:
            china_1688, ua_offers = await asyncio.gather(china_task, tg_task)
            return china_1688, ua_offers
        public_task = _fetch_public_china_offers(clean_title, settings, http_client)
        china_1688, public_china, ua_offers = await asyncio.gather(china_task, public_task, tg_task)
        combined_china = china_1688 + public_china
        combined_china.sort(key=lambda o: o.wholesale_price_usd)
        return combined_china[:5], ua_offers

    if client is not None:
        china_offers, ua_offers = await _gather_all(client)
    else:
        proxy = get_httpx_proxy(settings)
        async with httpx.AsyncClient(timeout=15.0, proxy=proxy, follow_redirects=True) as owned:
            china_offers, ua_offers = await _gather_all(owned)

    if not china_offers and not mock:
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
            )
        ]

    if not mock:
        set_cache(
            settings.DB_PATH,
            cache_key,
            {
                "china_offers": [c.model_dump(mode="json") for c in china_offers],
                "ua_drop_offers": [u.model_dump(mode="json") for u in ua_offers],
            },
        )

    return {
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
