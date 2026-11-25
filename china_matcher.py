import logging
from typing import Any, Optional

import httpx

from config import Settings, SupplierOffer

logger = logging.getLogger(__name__)


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return default


def _to_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return default


def _mock_suppliers(image_url: str, settings: Settings) -> list[SupplierOffer]:
    """Return 3 deterministic 1688 supplier offers for offline dry-run mode."""
    rmb_rate = settings.FX_RATES_TO_USD.get("RMB", 0.14)
    lowered = image_url.lower()
    if "posture" in lowered:
        prices_rmb = [42.0, 46.0, 50.0]
        base_title = "Adjustable Magnetic Posture Corrector Belt"
    elif "lamp" in lowered:
        prices_rmb = [22.0, 26.5, 31.0]
        base_title = "USB Sunset Projection Ambient Lamp"
    elif "vacuum" in lowered:
        prices_rmb = [28.0, 33.0, 38.5]
        base_title = "Mini Cordless Car Vacuum Cleaner 12000Pa"
    else:
        prices_rmb = [24.0, 28.5, 32.0]
        base_title = "Smart EMS Portable Neck Massager"

    templates = [
        ("Yiwu BestSource Trading Co., Ltd.", 4.9, 5, 5, "https://detail.1688.com/offer/810001.html"),
        ("Shenzhen BrightTech Factory", 4.7, 3, 10, "https://detail.1688.com/offer/810002.html"),
        ("Ningbo Direct Supply Ltd.", 4.6, 4, 20, "https://detail.1688.com/offer/810003.html"),
    ]
    offers: list[SupplierOffer] = []
    for idx, (supplier, rating, years, moq, url) in enumerate(templates):
        rmb = prices_rmb[idx]
        offers.append(
            SupplierOffer(
                supplier_name=supplier,
                product_title=f"{base_title} (Batch #{idx + 1})",
                product_url=url,
                wholesale_price_rmb=round(rmb, 2),
                wholesale_price_usd=round(rmb * rmb_rate, 2),
                rating=rating,
                years_on_1688=years,
                moq=moq,
                image_url=image_url,
            )
        )
    return offers


def _extract_raw_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract supplier list from common RapidAPI 1688 response shapes."""
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("resultList", "items", "list"):
            if isinstance(result.get(key), list):
                return result[key]
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("items", "resultList", "list"):
            if isinstance(data.get(key), list):
                return data[key]
    if isinstance(data, list):
        return data
    if isinstance(payload.get("items"), list):
        return payload["items"]
    return []


def _parse_supplier_item(item: dict[str, Any], rmb_rate: float) -> Optional[SupplierOffer]:
    """Normalize a single 1688 API item into SupplierOffer."""
    product_block = item.get("item") if isinstance(item.get("item"), dict) else item
    price_rmb = _to_float(
        product_block.get("price")
        or product_block.get("wholesale_price_rmb")
        or product_block.get("sku_price")
    )
    if price_rmb is None or price_rmb < 0:
        return None

    price_usd = _to_float(product_block.get("wholesale_price_usd"))
    if price_usd is None:
        price_usd = round(price_rmb * rmb_rate, 2)

    offer_id = product_block.get("num_iid") or product_block.get("offerId") or product_block.get("id") or "0"
    product_url = str(
        product_block.get("detail_url")
        or product_block.get("product_url")
        or product_block.get("url")
        or f"https://detail.1688.com/offer/{offer_id}.html"
    )
    seller_info = product_block.get("seller") if isinstance(product_block.get("seller"), dict) else {}
    supplier_name = str(
        product_block.get("supplier_name")
        or product_block.get("companyName")
        or seller_info.get("name")
        or "1688 Verified Supplier"
    )
    title = str(product_block.get("title") or product_block.get("subject") or "1688 Wholesale Product")
    rating = _to_float(product_block.get("rating") or seller_info.get("score"))
    if rating is not None:
        rating = max(0.0, min(5.0, rating))
    years = _to_int(product_block.get("years_on_1688") or seller_info.get("years"))
    moq = max(1, _to_int(product_block.get("moq") or product_block.get("minOrderQuantity"), default=1) or 1)
    img_url = product_block.get("pic_url") or product_block.get("image") or product_block.get("image_url")

    return SupplierOffer(
        supplier_name=supplier_name,
        product_title=title,
        product_url=product_url,
        wholesale_price_rmb=round(price_rmb, 2),
        wholesale_price_usd=round(price_usd, 2),
        rating=rating,
        years_on_1688=years,
        moq=moq,
        image_url=str(img_url) if img_url else None,
    )


async def match_1688_suppliers(
    image_url: str,
    settings: Settings,
    client: Optional[httpx.AsyncClient] = None,
    mock: bool = False,
) -> list[SupplierOffer]:
    """Query 1688 image search via RapidAPI and return top-3 matched suppliers."""
    if not image_url:
        return []
    if mock:
        return _mock_suppliers(image_url, settings)
    if not settings.RAPIDAPI_1688_KEY:
        return []

    endpoint = f"https://{settings.RAPIDAPI_1688_HOST}/item_search_img"
    headers = {
        "X-RapidAPI-Key": settings.RAPIDAPI_1688_KEY,
        "X-RapidAPI-Host": settings.RAPIDAPI_1688_HOST,
    }
    params = {"imgUrl": image_url}

    async def _fetch(http_client: httpx.AsyncClient) -> dict[str, Any]:
        resp = await http_client.get(endpoint, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    try:
        if client is not None:
            payload = await _fetch(client)
        else:
            async with httpx.AsyncClient(timeout=20.0) as owned_client:
                payload = await _fetch(owned_client)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("1688 image search failed: %s", exc)
        return []

    rmb_rate = settings.FX_RATES_TO_USD.get("RMB", 0.14)
    offers: list[SupplierOffer] = []
    for raw_item in _extract_raw_items(payload):
        if not isinstance(raw_item, dict):
            continue
        parsed = _parse_supplier_item(raw_item, rmb_rate)
        if parsed is not None:
            offers.append(parsed)

    offers.sort(key=lambda o: (o.wholesale_price_usd, -(o.rating or 0.0)))
    return offers[:3]
