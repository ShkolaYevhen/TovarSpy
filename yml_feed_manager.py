import asyncio
import logging
import xml.etree.ElementTree as ET
from typing import Any, Optional

import httpx

from config import DEFAULT_FX_RATES_TO_USD, Settings, SupplierOffer, get_httpx_proxy
from product_matcher import evaluate_product_match
from storage import get_cache, list_yml_feeds, set_cache, upsert_yml_feed

logger = logging.getLogger(__name__)


def _to_float(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    cleaned = raw.replace("\u00a0", "").replace(" ", "").replace(",", ".").strip()
    try:
        val = float(cleaned)
        return round(val, 2) if val > 0 else None
    except ValueError:
        return None


def parse_yml_catalog(xml_text: str, source_label: str = "YML Feed") -> list[dict[str, Any]]:
    """Parse standard Prom/MyDrop/TopDrop <yml_catalog> XML into normalized wholesale offer dicts."""
    if not xml_text or "<offer" not in xml_text.lower():
        return []
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as exc:
        logger.warning("Invalid YML XML from %s: %s", source_label, exc)
        return []

    items: list[dict[str, Any]] = []
    for offer_el in root.findall(".//offer"):
        avail_attr = str(offer_el.get("available") or "true").strip().lower()
        if avail_attr == "false":
            continue

        title = (offer_el.findtext("name") or offer_el.findtext("model") or "").strip()
        if not title:
            continue

        price_val = _to_float(offer_el.findtext("price"))
        if price_val is None or price_val <= 0:
            continue

        currency = (offer_el.findtext("currencyId") or "UAH").strip().upper()
        fx = DEFAULT_FX_RATES_TO_USD.get(currency, 0.024)
        price_uah = round(price_val if currency == "UAH" else (price_val * fx * 41.67), 2)
        price_usd = round(price_val * fx, 2)

        rrc_raw = (
            offer_el.findtext("rrc")
            or offer_el.findtext("oldprice")
            or offer_el.findtext("price_old")
            or offer_el.findtext("price_promo")
        )
        if not rrc_raw:
            for param in offer_el.findall("param"):
                pname = str(param.get("name") or "").strip().lower()
                if pname in ("ррц", "rrc", "роздрібна ціна"):
                    rrc_raw = param.text
                    break
        rrc_uah = _to_float(rrc_raw) or 0.0

        vendor = (offer_el.findtext("vendor") or source_label or "UA Dropship YML").strip()
        vendor_code = (offer_el.findtext("vendorCode") or offer_el.get("id") or "").strip()
        pic = (offer_el.findtext("picture") or "").strip()
        url = (offer_el.findtext("url") or "").strip()
        moq_raw = offer_el.findtext("minimum_order_quantity")
        moq = max(1, int(_to_float(moq_raw) or 1))

        full_title = f"{title} {vendor_code}".strip() if vendor_code and vendor_code.lower() not in title.lower() else title
        items.append({
            "offer_id": str(offer_el.get("id") or vendor_code),
            "title": full_title[:180],
            "vendor": vendor[:100],
            "vendor_code": vendor_code,
            "price_uah": price_uah,
            "price_usd": price_usd,
            "rrc_uah": rrc_uah,
            "image_url": pic or None,
            "product_url": url,
            "moq": moq,
            "source_label": source_label,
        })
    return items


async def fetch_yml_feed_offers(
    feed_url: str,
    settings: Settings,
    label: str = "",
    client: Optional[httpx.AsyncClient] = None,
) -> list[dict[str, Any]]:
    """Download and cache a supplier YML/XML catalog, returning parsed offer dicts."""
    clean_url = (feed_url or "").strip()
    cache_key = f"yml_feed:{clean_url}"
    cached = get_cache(settings.DB_PATH, cache_key, settings.CACHE_TTL_SECONDS)
    if isinstance(cached, list):
        return cached

    headers = {"User-Agent": "Mozilla/5.0 (compatible; TovarSpy-YML/1.0)"}
    try:
        if client is not None:
            resp = await client.get(clean_url, headers=headers)
        else:
            proxy = get_httpx_proxy(settings)
            async with httpx.AsyncClient(timeout=20.0, proxy=proxy, follow_redirects=True) as owned:
                resp = await owned.get(clean_url, headers=headers)
        if resp.status_code != 200:
            return []
        parsed = parse_yml_catalog(resp.text, source_label=label or clean_url.split("/")[2])
        set_cache(settings.DB_PATH, cache_key, parsed)
        try:
            upsert_yml_feed(settings.DB_PATH, clean_url, label=label, enabled=True, last_count=len(parsed))
        except Exception:
            pass
        return parsed
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch YML feed %s: %s", clean_url, exc)
        return []


async def search_yml_offers(
    core_phrase: str,
    target_title: str,
    target_price_uah: Optional[float],
    settings: Settings,
    ref_dhash: Optional[int] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> tuple[list[SupplierOffer], str]:
    """Search enabled YML/XML feeds with Stage 4 exact-product match verification."""
    feeds = list_yml_feeds(settings.DB_PATH, enabled_only=True)
    if not feeds:
        return [], "0 active YML feeds"

    batches = await asyncio.gather(
        *(fetch_yml_feed_offers(f["url"], settings, label=f["label"], client=client) for f in feeds)
    )
    ref_name = (target_title or core_phrase or "").strip()
    verified: list[SupplierOffer] = []

    for f_meta, items in zip(feeds, batches):
        for it in items:
            verdict = evaluate_product_match(
                ref_name,
                it["title"],
                ref_price_uah=target_price_uah,
                cand_price_uah=it["price_uah"],
                candidate_role="wholesale",
            )
            if not verdict.is_match:
                continue
            verified.append(
                SupplierOffer(
                    supplier_name=f"{it['vendor']} (YML)",
                    product_title=it["title"],
                    product_url=it.get("product_url") or f_meta["url"],
                    wholesale_price_rmb=0.0,
                    wholesale_price_usd=it["price_usd"],
                    wholesale_price_uah=it["price_uah"],
                    moq=it["moq"],
                    image_url=it.get("image_url"),
                    source="yml_feed",
                    supplier_type="ua_wholesale",
                    sales_count_text=f"YML Drop ({it['rrc_uah']:.0f}₴ RRP)" if it.get("rrc_uah") else "YML Wholesale",
                    match_score=verdict.score,
                    match_label=verdict.label,
                    is_live=True,
                )
            )

    verified.sort(key=lambda o: (-o.match_score, o.wholesale_price_uah or 999999.0))
    return verified[:6], (f"live: {len(verified)} verified across {len(feeds)} feeds" if verified else f"0 matches across {len(feeds)} feeds")
