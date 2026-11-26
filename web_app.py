import ipaddress
import json
import logging
import socket
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from config import CURATED_CATEGORIES, AnalyzedProductReport, Settings, TelegramDropOffer, get_httpx_proxy, get_settings
from main import DEFAULT_EXTERNAL_BLOCKS, run_pipeline, run_super_parse
from margin_calculator import calculate_cofounder_economics
from notifier import export_reports, send_telegram_alert
from sourcing_engine import fetch_google_trends_rss, search_telegram_dropshipping
from storage import (
    delete_tg_channel,
    get_scan,
    list_scans,
    list_tg_channels,
    list_watchlist,
    remove_watchlist,
    seed_default_channels,
    upsert_tg_channel,
    upsert_watchlist,
)

logger = logging.getLogger(__name__)

STATIC_INDEX_PATH = Path(__file__).resolve().parent / "static" / "index.html"
_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif", "image/avif"}
_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_IMG_CACHE_TTL = 3600
_IMG_CACHE_MAX = 128


class SpyPrefixMiddleware:
    """ASGI middleware that strips an optional leading /spy mount prefix."""

    def __init__(self, asgi_app: Any, prefix: str = "/spy") -> None:
        self.asgi_app = asgi_app
        self.prefix = prefix

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            path = scope.get("path", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                new_path = path[len(self.prefix) :] or "/"
                scope = dict(scope)
                scope["path"] = new_path
                raw_path = scope.get("raw_path")
                if isinstance(raw_path, (bytes, bytearray)):
                    prefix_bytes = self.prefix.encode("ascii")
                    if raw_path == prefix_bytes or raw_path.startswith(prefix_bytes + b"/"):
                        scope["raw_path"] = raw_path[len(prefix_bytes) :] or b"/"
        await self.asgi_app(scope, receive, send)


class ScanRequest(BaseModel):
    """Request payload for triggering live or demo product discovery."""

    query: str = ""
    countries: Optional[list[str]] = None
    sources: Optional[list[str]] = None
    categories: Optional[list[str]] = None
    super_parse: bool = False
    geo: str = "UA"
    mock: bool = False
    use_mock: Optional[bool] = None
    send_alerts: bool = False
    proxy_url: Optional[str] = None
    min_duration_days: Optional[int] = Field(default=None, ge=0)
    min_active_ad_sets: Optional[int] = Field(default=None, ge=1)
    shipping_cost_est_usd: Optional[float] = Field(default=None, ge=0)
    est_cpa_usd: Optional[float] = Field(default=None, ge=0)
    winner_net_margin_usd: Optional[float] = None


class AlertRequest(BaseModel):
    """Request payload for dispatching a Telegram alert for one analyzed offer."""

    ad_id: Optional[str] = None
    index: Optional[int] = Field(default=None, ge=0)
    mock: bool = False


class SimulateRequest(BaseModel):
    """Live unit economics simulator inputs."""

    retail_price_usd: float = Field(default=34.99, ge=0)
    china_wholesale_usd: float = Field(default=3.50, ge=0)
    drop_price_uah: Optional[float] = Field(default=None, ge=0)
    rrc_price_uah: Optional[float] = Field(default=None, ge=0)
    ua_return_rate: Optional[float] = Field(default=None, ge=0, le=0.95)
    ua_cpa_usd: Optional[float] = Field(default=None, ge=0)
    cross_border_cpa_usd: Optional[float] = Field(default=None, ge=0)
    shipping_usd: Optional[float] = Field(default=None, ge=0)
    marketplace_fee_pct: Optional[float] = Field(default=None, ge=0, le=0.8)
    payment_fee_pct: Optional[float] = Field(default=None, ge=0, le=0.3)
    ad_days_running: int = Field(default=10, ge=0)
    ad_active_sets: int = Field(default=6, ge=1)


class WatchlistItemRequest(BaseModel):
    """Request payload for saving a product offer to watchlist."""

    product_key: str
    title: str
    target_market: str = "UA"
    opportunity_score: float = 0.0
    report: dict[str, Any] = Field(default_factory=dict)


class TgChannelRequest(BaseModel):
    """Request payload for adding/toggling a UA Telegram drop channel."""

    handle: str
    label: str = ""
    enabled: bool = True
    test_scrape: bool = False


class AppState:
    """In-memory report state backed by output_dir CSV/JSON and SQLite storage."""

    def __init__(self, output_dir: Path, settings: Settings) -> None:
        self.output_dir = output_dir
        self.settings = settings
        self.products: list[AnalyzedProductReport] = []
        self.exported: dict[str, str] = {}
        self.source_diagnostics: dict[str, str] = {}
        self.trending_keywords: list[dict[str, str]] = []
        self.external_blocks: list[dict[str, str]] = list(DEFAULT_EXTERNAL_BLOCKS)
        self.img_cache: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
        self.last_scan_meta: dict[str, Any] = {
            "query": "",
            "countries": list(settings.TARGET_COUNTRIES),
            "sources": ["aliexpress", "prom_ua", "olx_ua", "olx_pl"],
            "mock": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


def _load_or_init_state(output_dir: Path, settings: Settings) -> AppState:
    """Load persisted live JSON report from output_dir if valid; never auto-seed fake mocks."""
    seed_default_channels(settings.DB_PATH, settings.DEFAULT_TG_CHANNELS)
    state = AppState(output_dir=output_dir, settings=settings)
    json_path = output_dir / "spy_report.json"
    if json_path.exists():
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(raw, list) and raw:
                loaded = [AnalyzedProductReport.model_validate(item) for item in raw]
                live_only = [r for r in loaded if r.ad.is_live]
                if live_only:
                    state.products = live_only
                    csv_path = output_dir / "spy_report.csv"
                    if csv_path.exists():
                        state.exported = {"csv": str(csv_path), "json": str(json_path)}
                    else:
                        state.exported = export_reports(state.products, output_dir=output_dir)
                    return state
        except Exception as exc:
            logger.warning("Failed to load existing report %s: %s", json_path, exc)

    state.exported = export_reports([], output_dir=output_dir)
    return state


def _compute_summary(products: list[AnalyzedProductReport]) -> dict[str, Any]:
    """Compute aggregate KPI metrics across analyzed offers."""
    total = len(products)
    winners = sum(1 for p in products if p.margin.is_winner)
    live_count = sum(1 for p in products if p.ad.is_live)
    margins = [p.margin.net_margin_usd for p in products]
    pis = [p.margin.profitability_index for p in products]
    scores = [p.economics.opportunity_score for p in products if p.economics is not None]
    return {
        "total": total,
        "live_count": live_count,
        "winners": winners,
        "non_winners": total - winners,
        "avg_net_margin_usd": round(sum(margins) / total, 2) if total else 0.0,
        "max_net_margin_usd": round(max(margins), 2) if margins else 0.0,
        "avg_profitability_index": round(sum(pis) / total, 4) if total else 0.0,
        "avg_opportunity_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
        "top_opportunity_score": round(max(scores), 1) if scores else 0.0,
    }


def _serialize_product(p: AnalyzedProductReport) -> dict[str, Any]:
    """Serialize AnalyzedProductReport with convenience top-level gallery fields."""
    data = p.model_dump(mode="json")
    best = p.best_supplier or (p.suppliers[0] if p.suppliers else None)
    ali_sup = next((s for s in p.suppliers if s.source == "aliexpress"), best)
    prom_sig = next((s for s in p.marketplace_signals if s.platform == "prom_ua" and s.top_sample_url), None)
    olx_sig = next((s for s in p.marketplace_signals if s.platform in ("olx_ua", "olx_pl") and s.top_sample_url), None)

    image_url = p.landing.image_url or p.ad.ad_creative_image_url or (best.image_url if best else "") or ""
    product_url = p.landing.final_url or p.landing.url or p.ad.landing_url
    data.update({
        "image_url": image_url,
        "product_url": product_url,
        "ali_url": ali_sup.product_url if ali_sup else None,
        "prom_url": product_url if p.ad.source == "prom_ua" else (prom_sig.top_sample_url if prom_sig else None),
        "olx_url": product_url if p.ad.source in ("olx_ua", "olx_pl") else (olx_sig.top_sample_url if olx_sig else None),
        "sold_text": ali_sup.sales_count_text if ali_sup else "",
        "rating": ali_sup.rating if ali_sup else None,
        "source": p.ad.source,
        "is_live": p.ad.is_live,
    })
    return data


def _serialize_report_payload(state: AppState) -> dict[str, Any]:
    """Build API report payload containing metadata, summary KPIs, diagnostics, and products."""
    return {
        "meta": state.last_scan_meta,
        "summary": _compute_summary(state.products),
        "exported": state.exported,
        "source_diagnostics": state.source_diagnostics,
        "trending_keywords": state.trending_keywords,
        "external_blocks": state.external_blocks,
        "curated_categories": CURATED_CATEGORIES,
        "products": [_serialize_product(p) for p in state.products],
    }


def _validate_public_image_url(raw_url: str) -> str:
    """Validate http/https image URL and reject private/loopback/link-local/reserved IPs."""
    parsed = urlparse(raw_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Only http/https image URLs are allowed")

    host = parsed.hostname
    try:
        addr_info = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail=f"DNS resolution failed: {exc}") from exc

    if not addr_info:
        raise HTTPException(status_code=400, detail="No IP addresses resolved for host")

    for entry in addr_info:
        ip_str = entry[4][0].split("%")[0]
        ip_obj = ipaddress.ip_address(ip_str)
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_reserved
            or ip_obj.is_multicast
            or ip_obj.is_unspecified
        ):
            raise HTTPException(status_code=403, detail="Blocked non-public image host")
    return raw_url.strip()


def create_app(
    output_dir: Optional[Path] = None,
    settings: Optional[Settings] = None,
) -> SpyPrefixMiddleware:
    """Create the FastAPI web application wrapped with /spy subpath middleware."""
    active_settings = settings or get_settings()
    resolved_dir = Path(output_dir) if output_dir is not None else Path(active_settings.EXPORT_DIR)
    state = _load_or_init_state(resolved_dir, active_settings)

    fastapi_app = FastAPI(title="TovarSpy", version="3.0.0")

    @fastapi_app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        if STATIC_INDEX_PATH.exists():
            return HTMLResponse(STATIC_INDEX_PATH.read_text(encoding="utf-8"))
        return HTMLResponse(
            "<!DOCTYPE html><html><head><meta charset='utf-8'><title>TovarSpy</title></head>"
            "<body><h1>TovarSpy Dashboard</h1><p>UI static/index.html pending.</p></body></html>"
        )

    @fastapi_app.get("/api/health")
    @fastapi_app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "tovarspy",
            "products_loaded": len(state.products),
        }

    @fastapi_app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        s = state.settings
        return {
            "target_countries": s.TARGET_COUNTRIES,
            "min_duration_days": s.MIN_DURATION_DAYS,
            "min_active_ad_sets": s.MIN_ACTIVE_AD_SETS,
            "shipping_cost_est_usd": s.SHIPPING_COST_EST_USD,
            "est_cpa_usd": s.EST_CPA_USD,
            "winner_net_margin_usd": s.WINNER_NET_MARGIN_USD,
            "ua_cod_return_rate": s.UA_COD_RETURN_RATE,
            "ua_nova_poshta_return_fee_usd": s.UA_NOVA_POSHTA_RETURN_FEE_USD,
            "ua_cpa_usd": s.UA_CPA_USD,
            "marketplace_fee_pct": s.MARKETPLACE_FEE_PCT,
            "payment_fee_pct": s.PAYMENT_FEE_PCT,
            "fx_rates_to_usd": s.FX_RATES_TO_USD,
            "available_sources": ["aliexpress", "prom_ua", "olx_ua", "olx_pl", "telegram", "meta", "tiktok"],
            "curated_categories": CURATED_CATEGORIES,
            "external_blocks": state.external_blocks,
            "proxy_configured": bool(s.PROXY_URL.strip()),
            "telegram_configured": bool(s.TELEGRAM_BOT_TOKEN and s.TELEGRAM_CHAT_ID),
        }

    @fastapi_app.get("/api/img")
    async def proxy_image(url: str = Query(..., min_length=8)) -> Response:
        safe_url = _validate_public_image_url(url)
        now = time.time()
        cached = state.img_cache.get(safe_url)
        if cached is not None:
            ts, img_bytes, ctype = cached
            if (now - ts) <= _IMG_CACHE_TTL:
                state.img_cache.move_to_end(safe_url)
                return Response(
                    content=img_bytes,
                    media_type=ctype,
                    headers={"Cache-Control": f"public, max-age={_IMG_CACHE_TTL}"},
                )
            state.img_cache.pop(safe_url, None)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
        }
        try:
            async with httpx.AsyncClient(
                timeout=8.0,
                follow_redirects=True,
                proxy=get_httpx_proxy(state.settings),
            ) as client:
                async with client.stream("GET", safe_url, headers=headers) as resp:
                    if resp.status_code != 200:
                        raise HTTPException(status_code=502, detail=f"Upstream image HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype not in _ALLOWED_IMAGE_TYPES:
                        raise HTTPException(status_code=415, detail=f"Unsupported content-type: {ctype or 'unknown'}")

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_IMAGE_BYTES:
                            raise HTTPException(status_code=413, detail="Image exceeds 4 MB limit")
                        chunks.append(chunk)
                    body = b"".join(chunks)
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Image fetch failed: {exc}") from exc

        if len(state.img_cache) >= _IMG_CACHE_MAX:
            state.img_cache.popitem(last=False)
        state.img_cache[safe_url] = (now, body, ctype)
        return Response(
            content=body,
            media_type=ctype,
            headers={"Cache-Control": f"public, max-age={_IMG_CACHE_TTL}"},
        )

    @fastapi_app.get("/api/trends")
    async def get_trends(geo: str = "UA") -> dict[str, Any]:
        trends = await fetch_google_trends_rss(geo=geo, settings=state.settings)
        return {"geo": geo.upper(), "trends": trends}

    @fastapi_app.get("/api/report")
    async def get_report() -> dict[str, Any]:
        return _serialize_report_payload(state)

    async def _execute_scan_or_super(req: ScanRequest, force_super: bool = False) -> dict[str, Any]:
        scan_settings = state.settings.model_copy(deep=True)
        if req.min_duration_days is not None:
            scan_settings.MIN_DURATION_DAYS = req.min_duration_days
        if req.min_active_ad_sets is not None:
            scan_settings.MIN_ACTIVE_AD_SETS = req.min_active_ad_sets
        if req.shipping_cost_est_usd is not None:
            scan_settings.SHIPPING_COST_EST_USD = req.shipping_cost_est_usd
        if req.est_cpa_usd is not None:
            scan_settings.EST_CPA_USD = req.est_cpa_usd
        if req.winner_net_margin_usd is not None:
            scan_settings.WINNER_NET_MARGIN_USD = req.winner_net_margin_usd
        if req.proxy_url is not None:
            scan_settings.PROXY_URL = req.proxy_url.strip()

        use_mock = req.use_mock if req.use_mock is not None else req.mock
        countries = [c.strip().upper() for c in req.countries if c.strip()] if req.countries else None

        if force_super or req.super_parse or req.categories is not None or (not use_mock and not req.sources):
            res = await run_super_parse(
                query=req.query,
                categories=req.categories,
                countries=countries,
                geo_trends=req.geo,
                output_dir=state.output_dir,
                mock=use_mock,
                settings=scan_settings,
            )
            state.settings = scan_settings
            state.products = res["reports"]
            state.exported = res["exported"]
            state.source_diagnostics = res["source_diagnostics"]
            state.trending_keywords = res["trending_keywords"]
            state.external_blocks = res["external_blocks"]
        else:
            sources = [s.strip().lower() for s in req.sources if s.strip()] if req.sources else None
            reports, exported = await run_pipeline(
                query=req.query,
                countries=countries,
                sources=sources,
                output_dir=state.output_dir,
                mock=use_mock,
                send_alerts=req.send_alerts,
                settings=scan_settings,
            )
            state.settings = scan_settings
            state.products = reports
            state.exported = exported
            state.source_diagnostics = {"demo": f"mock: {len(reports)} items"} if use_mock else {}

        state.last_scan_meta = {
            "query": req.query,
            "countries": countries or list(scan_settings.TARGET_COUNTRIES),
            "categories": req.categories or list(CURATED_CATEGORIES.keys()),
            "mock": use_mock,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return _serialize_report_payload(state)

    @fastapi_app.post("/api/scan")
    async def run_scan(req: ScanRequest) -> dict[str, Any]:
        return await _execute_scan_or_super(req, force_super=False)

    @fastapi_app.post("/api/super-parse")
    async def super_parse(req: ScanRequest) -> dict[str, Any]:
        return await _execute_scan_or_super(req, force_super=True)

    @fastapi_app.get("/api/scans")
    async def get_scan_history() -> dict[str, Any]:
        return {"scans": list_scans(state.settings.DB_PATH)}

    @fastapi_app.get("/api/scans/{scan_id}")
    async def get_scan_detail(scan_id: int) -> dict[str, Any]:
        scan = get_scan(state.settings.DB_PATH, scan_id)
        if scan is None:
            raise HTTPException(status_code=404, detail="Scan not found")
        return scan

    @fastapi_app.post("/api/simulate")
    async def simulate_economics(req: SimulateRequest) -> dict[str, Any]:
        tg_offer = None
        if req.drop_price_uah is not None and req.drop_price_uah > 0:
            tg_offer = TelegramDropOffer(
                channel="simulator",
                post_url="https://t.me/simulator",
                title="Simulated Offer",
                drop_price_uah=req.drop_price_uah,
                rrc_price_uah=req.rrc_price_uah or 0.0,
            )
        econ = calculate_cofounder_economics(
            retail_price_usd=req.retail_price_usd,
            china_wholesale_usd=req.china_wholesale_usd,
            settings=state.settings,
            ad_days_running=req.ad_days_running,
            ad_active_sets=req.ad_active_sets,
            best_tg_offer=tg_offer,
            ua_return_rate=req.ua_return_rate,
            ua_cpa_usd=req.ua_cpa_usd,
            cross_border_cpa_usd=req.cross_border_cpa_usd,
            shipping_usd=req.shipping_usd,
            marketplace_fee_pct=req.marketplace_fee_pct,
            payment_fee_pct=req.payment_fee_pct,
        )
        return econ.model_dump(mode="json")

    @fastapi_app.get("/api/watchlist")
    async def get_watchlist() -> dict[str, Any]:
        return {"items": list_watchlist(state.settings.DB_PATH)}

    @fastapi_app.post("/api/watchlist")
    async def add_watchlist(req: WatchlistItemRequest) -> dict[str, Any]:
        upsert_watchlist(
            state.settings.DB_PATH,
            product_key=req.product_key,
            title=req.title,
            target_market=req.target_market,
            opportunity_score=req.opportunity_score,
            report=req.report,
        )
        return {"saved": True, "product_key": req.product_key}

    @fastapi_app.delete("/api/watchlist/{product_key}")
    async def delete_watchlist(product_key: str) -> dict[str, Any]:
        if not remove_watchlist(state.settings.DB_PATH, product_key):
            raise HTTPException(status_code=404, detail="Watchlist item not found")
        return {"deleted": True, "product_key": product_key}

    @fastapi_app.get("/api/tg-channels")
    async def get_tg_channels() -> dict[str, Any]:
        return {"channels": list_tg_channels(state.settings.DB_PATH)}

    @fastapi_app.post("/api/tg-channels")
    async def add_tg_channel(req: TgChannelRequest) -> dict[str, Any]:
        try:
            saved = upsert_tg_channel(
                state.settings.DB_PATH,
                handle=req.handle,
                label=req.label,
                enabled=req.enabled,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        preview_count = None
        if req.test_scrape:
            offers = await search_telegram_dropshipping("", state.settings, channels=[saved["handle"]])
            preview_count = len(offers)
        return {"channel": saved, "preview_count": preview_count}

    @fastapi_app.delete("/api/tg-channels/{handle}")
    async def remove_tg_channel(handle: str) -> dict[str, Any]:
        if not delete_tg_channel(state.settings.DB_PATH, handle):
            raise HTTPException(status_code=404, detail="Telegram channel not found")
        return {"deleted": True, "handle": handle}

    @fastapi_app.get("/api/export/{fmt}")
    async def download_export(fmt: Literal["csv", "json"]) -> FileResponse:
        file_str = state.exported.get(fmt)
        if not file_str or not Path(file_str).exists():
            state.exported = export_reports(state.products, output_dir=state.output_dir)
            file_str = state.exported[fmt]
        media_type = "text/csv; charset=utf-8" if fmt == "csv" else "application/json; charset=utf-8"
        return FileResponse(
            path=file_str,
            media_type=media_type,
            filename=f"spy_report.{fmt}",
        )

    @fastapi_app.post("/api/alert")
    async def trigger_alert(req: AlertRequest) -> dict[str, Any]:
        target: Optional[AnalyzedProductReport] = None
        if req.ad_id is not None:
            target = next((p for p in state.products if p.ad.ad_id == req.ad_id), None)
        elif req.index is not None and 0 <= req.index < len(state.products):
            target = state.products[req.index]

        if target is None:
            raise HTTPException(status_code=404, detail="Product report not found")

        sent = await send_telegram_alert(target, state.settings, mock=req.mock)
        return {
            "sent": sent,
            "ad_id": target.ad.ad_id,
            "is_winner": target.margin.is_winner,
            "mock": req.mock,
        }

    return SpyPrefixMiddleware(fastapi_app, prefix="/spy")


app = create_app()
