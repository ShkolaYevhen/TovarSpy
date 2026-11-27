from datetime import date, timedelta
import logging
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from product_matcher import _token_overlap_score, normalize_product_tokens

logger = logging.getLogger(__name__)

TYVER_BASE_URL = "https://api.tyver.io/public/v1"

_JUNK_STORE_DOMAINS: tuple[str, ...] = (
    "amazon.",
    "instagram.com",
    "facebook.com",
    "fb.me",
    "t.me",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "google.",
    "play.google.com",
    "apps.apple.com",
    "wa.me",
    "viber.com",
)

_NON_ECOM_KEYWORDS: tuple[str, ...] = (
    "casino", "казино", "slots", "aviator", "betting", "букмекер", "фриспин", "фріспін",
    "бонус на депозит", "1win", "pin-up", "pinup", "parimatch", "favbet", "vbet",
    "cosmolot", "vulkan", "вулкан", "клініка", "стоматолог", "вебінар", "таро", "астролог",
)


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"X-Tyver-Authorization": api_key.strip()}


async def _tyver_get(
    path: str,
    api_key: str,
    params: Optional[list[tuple[str, Any]]] = None,
    client: Optional[httpx.AsyncClient] = None,
    proxy: Optional[str] = None,
) -> tuple[int, Any, str]:
    """Perform an authenticated GET against the Tyver Public API without logging secrets."""
    url = f"{TYVER_BASE_URL}{path}"
    headers = _auth_headers(api_key)

    async def _run(http_client: httpx.AsyncClient) -> tuple[int, Any, str]:
        resp = await http_client.get(url, headers=headers, params=params)
        text_preview = ""
        try:
            payload = resp.json()
        except ValueError:
            payload = None
            text_preview = resp.text[:240]
        return resp.status_code, payload, text_preview

    try:
        if client is not None:
            return await _run(client)
        async with httpx.AsyncClient(timeout=20.0, proxy=proxy, follow_redirects=True) as owned:
            return await _run(owned)
    except httpx.HTTPError as exc:
        logger.warning("Tyver request to %s failed: %s", path, type(exc).__name__)
        return 0, None, str(exc)


async def fetch_tyver_quota(
    api_key: str,
    client: Optional[httpx.AsyncClient] = None,
    proxy: Optional[str] = None,
) -> dict[str, Any]:
    """Check free GET /info/me for tariff and remaining view credits."""
    clean_key = (api_key or "").strip()
    if not clean_key:
        return {
            "ok": False,
            "configured": False,
            "status": "missing_key",
            "tariff": None,
            "remainder_views": 0,
            "error": "TYVER_API_KEY is not configured.",
        }

    status_code, payload, err_text = await _tyver_get("/info/me", clean_key, client=client, proxy=proxy)
    if status_code == 200 and isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        return {
            "ok": True,
            "configured": True,
            "status": "active",
            "tariff": data.get("tariff"),
            "remainder_views": int(data.get("remainder_views") or 0),
            "error": None,
        }

    detail = (payload or {}).get("detail") if isinstance(payload, dict) else err_text
    return {
        "ok": False,
        "configured": True,
        "status": f"http_{status_code}" if status_code else "network_error",
        "tariff": None,
        "remainder_views": 0,
        "error": str(detail or f"Tyver returned HTTP {status_code}"),
    }


async def fetch_tyver_ai_filters(
    api_key: str,
    client: Optional[httpx.AsyncClient] = None,
    proxy: Optional[str] = None,
) -> dict[str, Any]:
    """Fetch valid iGaming AI filter options from free GET /catalog/ai-filters."""
    clean_key = (api_key or "").strip()
    if not clean_key:
        return {"ok": False, "filters": {}, "error": "TYVER_API_KEY is not configured."}

    status_code, payload, err_text = await _tyver_get("/catalog/ai-filters", clean_key, client=client, proxy=proxy)
    if status_code == 200 and isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        return {"ok": True, "filters": data, "error": None}
    return {"ok": False, "filters": {}, "error": err_text or f"HTTP {status_code}"}


def _pick_media(media_list: Any) -> tuple[Optional[str], Optional[str], str]:
    """Extract (thumbnail_url, video_url, media_type) from a Tyver PublicCreativeSchema media list."""
    if not isinstance(media_list, list) or not media_list:
        return None, None, "UNKNOWN"

    first = media_list[0] if isinstance(media_list[0], dict) else {}
    ctype = str(first.get("content_type") or "").lower()
    is_video = "video" in ctype or bool(first.get("video_preview") or first.get("video_image_preview_url"))

    thumb = (
        first.get("compressed_video_image_preview_url")
        or first.get("video_image_preview_url")
        or first.get("compressed_image_url")
        or (first.get("url") if not is_video else None)
    )
    video_url = first.get("url") if is_video else None
    if not thumb and isinstance(first.get("video_preview"), dict):
        thumb = first["video_preview"].get("url")
    return thumb, video_url, ("VIDEO" if is_video else "IMAGE")


def normalize_tyver_creative(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a single Tyver creative record into a consistent dictionary."""
    bodies = [str(b).strip() for b in (raw.get("bodies") or []) if str(b).strip()]
    link_titles = [str(t).strip() for t in (raw.get("link_titles") or []) if str(t).strip()]
    link_descriptions = [str(d).strip() for d in (raw.get("link_descriptions") or []) if str(d).strip()]
    title = link_titles[0] if link_titles else (bodies[0][:100] if bodies else str(raw.get("provider_page_name") or "Tyver Creative"))
    text = "\n".join(bodies) if bodies else (link_descriptions[0] if link_descriptions else "")

    thumb, video_url, inferred_media = _pick_media(raw.get("media"))
    target_url = str(raw.get("target_url") or "").strip()
    domain = str(raw.get("domain") or "").strip()
    if not target_url and domain and "." in domain and not domain.replace(".", "").isdigit():
        target_url = f"https://{domain}"

    return {
        "id": str(raw.get("provider_id") or raw.get("id") or ""),
        "page_name": str(raw.get("provider_page_name") or "Unknown Advertiser"),
        "title": title[:160],
        "text": text[:600],
        "cta": raw.get("cta"),
        "target_domain": domain or None,
        "landing_url": target_url,
        "image_url": thumb,
        "video_url": video_url,
        "thumbnail_url": thumb,
        "media_type": raw.get("media_type") or inferred_media,
        "countries": [str(c).upper() for c in (raw.get("countries") or [])],
        "activity_days": int(raw.get("activity_days_amount") or 0),
        "is_active": bool(raw.get("is_active", True)),
        "first_active_date": raw.get("first_active_date"),
        "last_active_date": raw.get("last_active_date"),
        "ai_category": raw.get("ai_category") or ("GAMBLING" if "GAMBLING" in (raw.get("verticals") or []) else None),
        "ai_angle": raw.get("ai_angle"),
        "ai_gambling_game_type": raw.get("ai_gambling_game_type"),
        "verticals": raw.get("verticals") or [],
        "cms": raw.get("cms"),
        "impressions": raw.get("impressions"),
        "raw_source": "tyver",
    }


async def fetch_tyver_creatives(
    api_key: str,
    mode: str = "ecom_ua",
    query: str = "",
    countries: Optional[list[str]] = None,
    ai_category: Optional[str] = None,
    ai_angle: Optional[str] = None,
    ai_gambling_game_type: Optional[str] = None,
    activity_days_amount_gte: Optional[int] = None,
    creation_date_gte: Optional[str] = None,
    languages: Optional[list[str]] = None,
    cta: Optional[str | list[str]] = None,
    cms: Optional[str | list[str]] = None,
    sort: str = "activity_days_amount",
    cursor: Optional[str] = None,
    page: int = 1,
    page_size: int = 24,
    client: Optional[httpx.AsyncClient] = None,
    proxy: Optional[str] = None,
) -> dict[str, Any]:
    """Query GET /creatives/ on Tyver.io for either iGaming or Ukraine e-com ads."""
    clean_key = (api_key or "").strip()
    if not clean_key:
        return {
            "ok": False,
            "mode": mode,
            "total": 0,
            "cursor": None,
            "items": [],
            "error": "TYVER_API_KEY is not configured.",
        }

    norm_mode = (mode or "ecom_ua").strip().lower()
    params: list[tuple[str, Any]] = [("sort", sort or "activity_days_amount")]

    target_countries = [c.strip().upper() for c in (countries or []) if c and c.strip()]
    if norm_mode == "igaming":
        params.append(("ai_category", ai_category or "GAMBLING"))
        for c in target_countries:
            params.append(("countries", c))
        if ai_angle:
            params.append(("ai_angle", ai_angle))
        if ai_gambling_game_type:
            params.append(("ai_gambling_game_type", ai_gambling_game_type))
    else:
        # Note: Tyver returns 0 results if vertical=ECOMMERCE is passed; filter UA by countries + query/cta/cms.
        for c in (target_countries or ["UA"]):
            params.append(("countries", c))

    clean_q = (query or "").strip()
    if len(clean_q) >= 2:
        params.append(("search", clean_q))
    if activity_days_amount_gte is not None and activity_days_amount_gte >= 0:
        params.append(("activity_days_amount_gte", int(activity_days_amount_gte)))
    if creation_date_gte:
        params.append(("creation_date_gte", creation_date_gte.strip()))
    for lang in (languages or []):
        if lang and lang.strip():
            params.append(("languages", lang.strip().lower()))
    if isinstance(cta, list):
        for c_btn in cta:
            if c_btn and c_btn.strip():
                params.append(("cta", c_btn.strip()))
    elif cta:
        params.append(("cta", cta))
    if isinstance(cms, list):
        for cms_val in cms:
            if cms_val and cms_val.strip():
                params.append(("cms", cms_val.strip()))
    elif cms:
        params.append(("cms", cms))
    if cursor:
        params.append(("cursor", cursor))

    status_code, payload, err_text = await _tyver_get(
        "/creatives/", clean_key, params=params, client=client, proxy=proxy
    )
    if status_code != 200 or payload is None:
        detail = payload.get("detail") if isinstance(payload, dict) else err_text
        return {
            "ok": False,
            "mode": norm_mode,
            "total": 0,
            "cursor": None,
            "items": [],
            "error": str(detail or f"Tyver HTTP {status_code}"),
        }

    data_block = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
    raw_items: list[dict[str, Any]] = []
    next_cursor: Optional[str] = None
    total = 0

    if isinstance(data_block, list):
        raw_items = [x for x in data_block if isinstance(x, dict)]
        total = len(raw_items)
    elif isinstance(data_block, dict):
        candidates = data_block.get("items") or data_block.get("results") or []
        raw_items = [x for x in candidates if isinstance(x, dict)]
        next_cursor = data_block.get("cursor")
        total = int(data_block.get("total") or len(raw_items))

    # Apply client-side page slice only when caller requests smaller slice without cursor pagination
    normalized = [normalize_tyver_creative(item) for item in raw_items]
    if page_size > 0 and len(normalized) > page_size:
        start = max(0, (max(1, page) - 1) * page_size) if not cursor else 0
        sliced = normalized[start : start + page_size]
        normalized = sliced if sliced else normalized[:page_size]

    return {
        "ok": True,
        "mode": norm_mode,
        "total": total,
        "cursor": next_cursor,
        "items": normalized,
        "error": None,
    }


def _normalize_store_key(url: str) -> str:
    """Normalize landing URL to domain + clean path for creative deduplication."""
    parsed = urlparse((url or "").strip())
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"{host}{path}" if host else (url or "").strip().lower()


def is_ecom_store_candidate(creative: dict[str, Any]) -> bool:
    """Return True if a Tyver creative targets a genuine physical-goods web store/landing."""
    landing = str(creative.get("landing_url") or "").strip()
    if not landing.startswith(("http://", "https://")):
        return False
    parsed = urlparse(landing)
    host = parsed.netloc.lower()
    if not host or any(junk in host for junk in _JUNK_STORE_DOMAINS):
        return False

    verticals = [str(v).upper() for v in (creative.get("verticals") or [])]
    if "GAMBLING" in verticals or creative.get("ai_category") == "GAMBLING":
        return False

    blob = " ".join(
        str(creative.get(k) or "") for k in ("title", "text", "page_name", "target_domain", "landing_url")
    ).lower()
    if any(kw in blob for kw in _NON_ECOM_KEYWORDS):
        return False
    return True


async def discover_winning_ads(
    api_key: str,
    *,
    country: str = "UA",
    query: str = "",
    min_days: int = 3,
    lookback_days: int = 120,
    languages: Optional[list[str]] = None,
    ctas: Optional[list[str]] = None,
    page_size: int = 60,
    client: Optional[httpx.AsyncClient] = None,
    proxy: Optional[str] = None,
) -> dict[str, Any]:
    """Stage 1: Discover active recent e-commerce/COD ad winners from Tyver.io and deduplicate by landing."""
    since_date = (date.today() - timedelta(days=max(7, lookback_days))).isoformat()
    target_country = (country or "UA").strip().upper()
    clean_q = (query or "").strip()
    langs = languages if languages is not None else (["uk"] if target_country == "UA" and not clean_q else [])
    cta_list = ctas if ctas is not None else ["Shop Now", "Order Now", "Buy Now"]

    res = await fetch_tyver_creatives(
        api_key=api_key,
        mode="ecom_ua",
        query=clean_q,
        countries=[target_country],
        activity_days_amount_gte=max(1, min_days),
        creation_date_gte=since_date,
        languages=langs,
        cta=cta_list,
        sort="activity_days_amount",
        page_size=page_size,
        client=client,
        proxy=proxy,
    )
    if not res.get("ok"):
        return res

    raw_items = res.get("items") or []
    grouped: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        if not is_ecom_store_candidate(item):
            continue
        key = _normalize_store_key(item["landing_url"])
        existing = grouped.get(key)
        if existing is None:
            copy = dict(item)
            copy["duplicate_count"] = 1
            copy["creative_ids"] = [item["id"]] if item.get("id") else []
            grouped[key] = copy
        else:
            existing["duplicate_count"] += 1
            if item.get("id") and item["id"] not in existing["creative_ids"]:
                existing["creative_ids"].append(item["id"])
            if int(item.get("activity_days") or 0) > int(existing.get("activity_days") or 0):
                existing["activity_days"] = item["activity_days"]

    deduped = list(grouped.values())
    deduped.sort(
        key=lambda x: (int(x.get("duplicate_count") or 1), int(x.get("activity_days") or 0)),
        reverse=True,
    )
    return {
        "ok": True,
        "mode": "ecom_ua",
        "total": res.get("total", len(deduped)),
        "raw_count": len(raw_items),
        "items": deduped,
        "error": None,
    }


async def validate_cross_advertiser_demand(
    api_key: str,
    core_phrase: str,
    *,
    country: str = "UA",
    min_ad_sets: int = 5,
    min_days: int = 3,
    client: Optional[httpx.AsyncClient] = None,
    proxy: Optional[str] = None,
) -> dict[str, Any]:
    """Stage 3: Query Tyver Pass #2 for core_phrase across advertisers to validate cumulative ad spend."""
    clean_phrase = (core_phrase or "").strip()
    if len(clean_phrase) < 2 or not (api_key or "").strip():
        return {
            "ok": False,
            "core_phrase": clean_phrase,
            "competitor_advertisers": 0,
            "total_matching_ads": 0,
            "cumulative_active_days": 0,
            "total_impressions": 0,
            "meets_ad_sets_threshold": False,
            "competitor_ads": [],
        }

    res = await fetch_tyver_creatives(
        api_key=api_key,
        mode="ecom_ua",
        query=clean_phrase,
        countries=[(country or "UA").strip().upper()],
        activity_days_amount_gte=max(1, min_days),
        sort="activity_days_amount",
        page_size=60,
        client=client,
        proxy=proxy,
    )
    if not res.get("ok"):
        return {
            "ok": False,
            "core_phrase": clean_phrase,
            "competitor_advertisers": 0,
            "total_matching_ads": 0,
            "cumulative_active_days": 0,
            "total_impressions": 0,
            "meets_ad_sets_threshold": False,
            "competitor_ads": [],
            "error": res.get("error"),
        }

    ref_tokens = normalize_product_tokens(clean_phrase)
    matched: list[dict[str, Any]] = []
    advertisers: set[str] = set()
    cum_days = 0
    total_impr = 0

    for item in res.get("items") or []:
        if not is_ecom_store_candidate(item):
            continue
        blob_tokens = normalize_product_tokens(f"{item.get('title') or ''} {item.get('text') or ''}")
        recall, _ = _token_overlap_score(ref_tokens, blob_tokens)
        if ref_tokens and recall < 0.34:
            continue
        matched.append(item)
        adv_key = str(item.get("page_name") or item.get("target_domain") or item.get("id") or "").strip().lower()
        if adv_key:
            advertisers.add(adv_key)
        cum_days += max(0, int(item.get("activity_days") or 0))
        if isinstance(item.get("impressions"), (int, float)):
            total_impr += int(item["impressions"])

    total_ads = max(len(matched), int(res.get("total") or len(matched)) if matched else 0)
    compact_proofs = [
        {
            "id": m.get("id"),
            "page_name": m.get("page_name"),
            "domain": m.get("target_domain"),
            "landing_url": m.get("landing_url"),
            "activity_days": m.get("activity_days"),
            "image_url": m.get("thumbnail_url") or m.get("image_url"),
            "cta": m.get("cta"),
        }
        for m in matched[:8]
    ]
    return {
        "ok": True,
        "core_phrase": clean_phrase,
        "competitor_advertisers": len(advertisers),
        "total_matching_ads": len(matched),
        "api_total_hits": total_ads,
        "cumulative_active_days": cum_days,
        "total_impressions": total_impr,
        "meets_ad_sets_threshold": len(matched) >= min_ad_sets,
        "competitor_ads": compact_proofs,
    }
