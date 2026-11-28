import asyncio
from datetime import date, timedelta
import logging
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from config import (
    COD_DISCOVERY_SIGNALS,
    DROPSHIP_CTAS,
    EST_DAILY_SPEND_PER_CREATIVE_USD,
    MARKETPLACE_BLOCKLIST,
    MAX_AD_AGE_DAYS,
    MIN_DURATION_DAYS,
    MIN_EST_SPEND_USD,
)
from product_matcher import _token_overlap_score, is_valid_core_phrase, normalize_product_tokens

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
) + MARKETPLACE_BLOCKLIST

_COLLECTION_URL_HINTS: tuple[str, ...] = (
    "/collections",
    "/catalog",
    "/category",
    "/categories",
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

    thumb = None
    video_url = first.get("url") if is_video else None
    for entry in media_list:
        if not isinstance(entry, dict):
            continue
        e_ctype = str(entry.get("content_type") or "").lower()
        e_is_video = "video" in e_ctype or bool(entry.get("video_preview") or entry.get("video_image_preview_url"))
        candidate_thumb = (
            entry.get("compressed_video_image_preview_url")
            or entry.get("video_image_preview_url")
            or entry.get("compressed_image_url")
            or (entry.get("url") if not e_is_video else None)
        )
        if not candidate_thumb and isinstance(entry.get("video_preview"), dict):
            candidate_thumb = entry["video_preview"].get("url")
        if candidate_thumb:
            thumb = str(candidate_thumb).strip()
            break
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
        "creation_time": raw.get("creation_time"),
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
    activity_days_amount_lte: Optional[int] = None,
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
    if activity_days_amount_lte is not None and activity_days_amount_lte >= 0:
        params.append(("activity_days_amount_lte", int(activity_days_amount_lte)))
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


def _parse_iso_date(val: Any) -> Optional[date]:
    text = str(val or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def within_ad_age_window(
    creative: dict[str, Any],
    *,
    min_days: int = MIN_DURATION_DAYS,
    max_age_days: int = MAX_AD_AGE_DAYS,
    today: Optional[date] = None,
) -> bool:
    """Ensure ad has run at least `min_days` and is not older than `max_age_days`."""
    days = int(creative.get("activity_days") or 0)
    if days < min_days or days > max_age_days:
        return False
    ref_today = today or date.today()
    for date_key in ("creation_time", "first_active_date"):
        created = _parse_iso_date(creative.get(date_key))
        if created is not None and (ref_today - created).days > max_age_days:
            return False
    return True


def estimate_ad_spend_usd(
    cumulative_active_days: int,
    total_impressions: int = 0,
    daily_spend_usd: float = EST_DAILY_SPEND_PER_CREATIVE_USD,
) -> tuple[float, str]:
    """Estimate cumulative ad spend across active creatives from active days and EU impressions."""
    days_spend = round(max(0, int(cumulative_active_days)) * float(daily_spend_usd), 2)
    if total_impressions and int(total_impressions) > 0:
        impr_spend = round((int(total_impressions) / 1000.0) * 5.0, 2)
        return max(days_spend, impr_spend), "cumulative_days+impressions"
    return days_spend, "cumulative_days"


def is_ecom_store_candidate(creative: dict[str, Any]) -> bool:
    """Return True if a Tyver creative targets a genuine physical-goods web store/landing."""
    landing = str(creative.get("landing_url") or "").strip()
    if not landing.startswith(("http://", "https://")):
        return False
    parsed = urlparse(landing)
    host = parsed.netloc.lower().split(":")[0].removeprefix("www.")
    if not host:
        return False
    for junk in _JUNK_STORE_DOMAINS:
        if junk.endswith("."):
            if junk in host:
                return False
        elif host == junk or host.endswith(f".{junk}"):
            return False

    path_lower = (parsed.path or "").lower()
    if any(hint in path_lower for hint in _COLLECTION_URL_HINTS):
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
    min_days: int = MIN_DURATION_DAYS,
    max_age_days: int = MAX_AD_AGE_DAYS,
    lookback_days: Optional[int] = None,
    languages: Optional[list[str]] = None,
    ctas: Optional[list[str]] = None,
    page_size: int = 60,
    client: Optional[httpx.AsyncClient] = None,
    proxy: Optional[str] = None,
) -> dict[str, Any]:
    """Stage 1: Discover active 3-14d e-commerce/COD ad winners from Tyver.io and deduplicate by landing."""
    age_cap = max(min_days, int(lookback_days if lookback_days is not None else max_age_days))
    since_date = (date.today() - timedelta(days=age_cap)).isoformat()
    target_country = (country or "UA").strip().upper()
    clean_q = (query or "").strip()
    langs = languages if languages is not None else (["uk"] if target_country == "UA" else [])
    cta_list = ctas if ctas is not None else list(DROPSHIP_CTAS)

    search_queries: list[str]
    if clean_q:
        search_queries = [clean_q]
    elif target_country == "UA":
        # Auto-discover diverse COD landers across proven UA cash-on-delivery queries + general CTA scan.
        search_queries = ["", *COD_DISCOVERY_SIGNALS]
    else:
        search_queries = [""]

    tasks = [
        fetch_tyver_creatives(
            api_key=api_key,
            mode="ecom_ua",
            query=sq,
            countries=[target_country],
            activity_days_amount_gte=max(1, min_days),
            activity_days_amount_lte=age_cap,
            creation_date_gte=since_date,
            languages=langs,
            cta=cta_list,
            sort="activity_days_amount",
            page_size=page_size,
            client=client,
            proxy=proxy,
        )
        for sq in search_queries
    ]
    responses = await asyncio.gather(*tasks)
    ok_responses = [r for r in responses if r.get("ok")]
    if not ok_responses:
        return responses[0]

    seen_creative_ids: set[str] = set()
    raw_items: list[dict[str, Any]] = []
    api_total = 0
    for resp in ok_responses:
        api_total = max(api_total, int(resp.get("total") or 0))
        for item in resp.get("items") or []:
            cid = str(item.get("id") or "")
            if cid and cid in seen_creative_ids:
                continue
            if cid:
                seen_creative_ids.add(cid)
            raw_items.append(item)

    grouped: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        if not is_ecom_store_candidate(item):
            continue
        if not within_ad_age_window(item, min_days=max(1, min_days), max_age_days=age_cap):
            continue
        key = _normalize_store_key(item["landing_url"])
        item_days = max(0, int(item.get("activity_days") or 0))
        item_impr = int(item.get("impressions") or 0) if isinstance(item.get("impressions"), (int, float)) else 0
        existing = grouped.get(key)
        if existing is None:
            copy = dict(item)
            copy["duplicate_count"] = 1
            copy["creative_ids"] = [item["id"]] if item.get("id") else []
            copy["cumulative_days"] = item_days
            copy["_total_impressions"] = item_impr
            grouped[key] = copy
        else:
            existing["duplicate_count"] += 1
            existing["cumulative_days"] += item_days
            existing["_total_impressions"] += item_impr
            if item.get("id") and item["id"] not in existing["creative_ids"]:
                existing["creative_ids"].append(item["id"])
            if item_days > int(existing.get("activity_days") or 0):
                existing["activity_days"] = item_days
            if not existing.get("thumbnail_url") and item.get("thumbnail_url"):
                existing["thumbnail_url"] = item["thumbnail_url"]
                existing["image_url"] = item.get("image_url") or item["thumbnail_url"]

    deduped: list[dict[str, Any]] = []
    for entry in grouped.values():
        impr = entry.pop("_total_impressions", 0)
        est_spend, spend_basis = estimate_ad_spend_usd(entry.get("cumulative_days") or 0, impr)
        entry["est_spend_usd"] = est_spend
        entry["spend_basis"] = spend_basis
        deduped.append(entry)

    deduped.sort(
        key=lambda x: (
            int(x.get("duplicate_count") or 1),
            float(x.get("est_spend_usd") or 0.0),
            int(x.get("activity_days") or 0),
        ),
        reverse=True,
    )
    return {
        "ok": True,
        "mode": "ecom_ua",
        "total": api_total or len(deduped),
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
    min_days: int = MIN_DURATION_DAYS,
    max_age_days: int = MAX_AD_AGE_DAYS,
    min_spend_usd: float = MIN_EST_SPEND_USD,
    client: Optional[httpx.AsyncClient] = None,
    proxy: Optional[str] = None,
) -> dict[str, Any]:
    """Stage 3: Query Tyver Pass #2 for core_phrase across advertisers within 3-14d to validate ad spend."""
    clean_phrase = (core_phrase or "").strip()
    if not is_valid_core_phrase(clean_phrase, min_tokens=2) or not (api_key or "").strip():
        return {
            "ok": False,
            "core_phrase": clean_phrase,
            "competitor_advertisers": 0,
            "total_matching_ads": 0,
            "cumulative_active_days": 0,
            "total_impressions": 0,
            "est_spend_usd": 0.0,
            "spend_basis": "none",
            "meets_ad_sets_threshold": False,
            "meets_spend_threshold": False,
            "competitor_ads": [],
            "error": "core_phrase requires at least 2 meaningful tokens" if (api_key or "").strip() else "TYVER_API_KEY missing",
        }

    since_date = (date.today() - timedelta(days=max(min_days, max_age_days))).isoformat()
    res = await fetch_tyver_creatives(
        api_key=api_key,
        mode="ecom_ua",
        query=clean_phrase,
        countries=[(country or "UA").strip().upper()],
        activity_days_amount_gte=max(1, min_days),
        activity_days_amount_lte=max(min_days, max_age_days),
        creation_date_gte=since_date,
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
            "est_spend_usd": 0.0,
            "spend_basis": "none",
            "meets_ad_sets_threshold": False,
            "meets_spend_threshold": False,
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
        if not within_ad_age_window(item, min_days=max(1, min_days), max_age_days=max(min_days, max_age_days)):
            continue
        blob_tokens = normalize_product_tokens(f"{item.get('title') or ''} {item.get('text') or ''}")
        recall, _ = _token_overlap_score(ref_tokens, blob_tokens)
        if ref_tokens and recall < 0.60:
            continue
        matched.append(item)
        adv_key = str(item.get("page_name") or item.get("target_domain") or item.get("id") or "").strip().lower()
        if adv_key:
            advertisers.add(adv_key)
        cum_days += max(0, int(item.get("activity_days") or 0))
        if isinstance(item.get("impressions"), (int, float)):
            total_impr += int(item["impressions"])

    est_spend, spend_basis = estimate_ad_spend_usd(cum_days, total_impr)
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
        "est_spend_usd": est_spend,
        "spend_basis": spend_basis,
        "meets_ad_sets_threshold": len(matched) >= min_ad_sets,
        "meets_spend_threshold": est_spend >= min_spend_usd,
        "competitor_ads": compact_proofs,
    }
