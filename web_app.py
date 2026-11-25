import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from ad_collector import filter_winning_ads, generate_mock_ads
from china_matcher import _mock_suppliers
from config import AnalyzedProductReport, Settings, get_settings
from landing_parser import _mock_landing_for_url
from main import run_pipeline
from margin_calculator import calculate_profitability
from notifier import export_reports, send_telegram_alert

logger = logging.getLogger(__name__)

STATIC_INDEX_PATH = Path(__file__).resolve().parent / "static" / "index.html"


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
    """Request payload for triggering an ad spy scan."""

    query: str = ""
    countries: Optional[list[str]] = None
    sources: Optional[list[str]] = None
    mock: bool = True
    send_alerts: bool = False
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


class AppState:
    """In-memory report state backed by output_dir CSV/JSON artifacts."""

    def __init__(self, output_dir: Path, settings: Settings) -> None:
        self.output_dir = output_dir
        self.settings = settings
        self.products: list[AnalyzedProductReport] = []
        self.exported: dict[str, str] = {}
        self.last_scan_meta: dict[str, Any] = {
            "query": "",
            "countries": list(settings.TARGET_COUNTRIES),
            "sources": ["meta", "adheart", "tiktok"],
            "mock": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


def _build_initial_mock_reports(settings: Settings) -> list[AnalyzedProductReport]:
    """Synchronously assemble deterministic mock reports on first boot."""
    ads = filter_winning_ads(
        generate_mock_ads(countries=settings.TARGET_COUNTRIES),
        min_duration_days=settings.MIN_DURATION_DAYS,
        min_active_ad_sets=settings.MIN_ACTIVE_AD_SETS,
        target_countries=settings.TARGET_COUNTRIES,
    )
    reports: list[AnalyzedProductReport] = []
    for ad in ads:
        landing = _mock_landing_for_url(ad.landing_url)
        img = landing.image_url or ad.ad_creative_image_url or ""
        suppliers = _mock_suppliers(img, settings)
        best = suppliers[0] if suppliers else None
        wholesale_usd = best.wholesale_price_usd if best else 0.0
        margin = calculate_profitability(
            landing.price or 0.0,
            landing.currency or "USD",
            wholesale_usd,
            settings,
        )
        reports.append(
            AnalyzedProductReport(
                ad=ad,
                landing=landing,
                suppliers=suppliers,
                best_supplier=best,
                margin=margin,
            )
        )
    return reports


def _load_or_seed_state(output_dir: Path, settings: Settings) -> AppState:
    """Load persisted JSON report from output_dir or seed initial mock data."""
    state = AppState(output_dir=output_dir, settings=settings)
    json_path = output_dir / "spy_report.json"
    if json_path.exists():
        try:
            import json

            raw = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(raw, list) and raw:
                state.products = [AnalyzedProductReport.model_validate(item) for item in raw]
                csv_path = output_dir / "spy_report.csv"
                if not csv_path.exists():
                    state.exported = export_reports(state.products, output_dir=output_dir)
                else:
                    state.exported = {"csv": str(csv_path), "json": str(json_path)}
                return state
        except Exception as exc:
            logger.warning("Failed to load existing report %s: %s", json_path, exc)

    state.products = _build_initial_mock_reports(settings)
    state.exported = export_reports(state.products, output_dir=output_dir)
    return state


def _compute_summary(products: list[AnalyzedProductReport]) -> dict[str, Any]:
    """Compute aggregate KPI metrics across analyzed offers."""
    total = len(products)
    winners = sum(1 for p in products if p.margin.is_winner)
    margins = [p.margin.net_margin_usd for p in products]
    pis = [p.margin.profitability_index for p in products]
    return {
        "total": total,
        "winners": winners,
        "non_winners": total - winners,
        "avg_net_margin_usd": round(sum(margins) / total, 2) if total else 0.0,
        "max_net_margin_usd": round(max(margins), 2) if margins else 0.0,
        "avg_profitability_index": round(sum(pis) / total, 4) if total else 0.0,
    }


def _serialize_report_payload(state: AppState) -> dict[str, Any]:
    """Build API report payload containing metadata, summary KPIs, and products."""
    return {
        "meta": state.last_scan_meta,
        "summary": _compute_summary(state.products),
        "exported": state.exported,
        "products": [p.model_dump(mode="json") for p in state.products],
    }


def create_app(
    output_dir: Optional[Path] = None,
    settings: Optional[Settings] = None,
) -> SpyPrefixMiddleware:
    """Create the FastAPI web application wrapped with /spy subpath middleware."""
    active_settings = settings or get_settings()
    resolved_dir = Path(output_dir) if output_dir is not None else Path(active_settings.EXPORT_DIR)
    state = _load_or_seed_state(resolved_dir, active_settings)

    fastapi_app = FastAPI(title="TovarSpy", version="1.0.0")

    @fastapi_app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        if STATIC_INDEX_PATH.exists():
            return HTMLResponse(STATIC_INDEX_PATH.read_text(encoding="utf-8"))
        return HTMLResponse(
            "<!DOCTYPE html><html><head><meta charset='utf-8'><title>TovarSpy</title></head>"
            "<body><h1>TovarSpy Dashboard</h1><p>UI static/index.html pending.</p></body></html>"
        )

    @fastapi_app.get("/api/health")
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
            "fx_rates_to_usd": s.FX_RATES_TO_USD,
            "available_sources": ["meta", "adheart", "tiktok"],
            "telegram_configured": bool(s.TELEGRAM_BOT_TOKEN and s.TELEGRAM_CHAT_ID),
        }

    @fastapi_app.get("/api/report")
    async def get_report() -> dict[str, Any]:
        return _serialize_report_payload(state)

    @fastapi_app.post("/api/scan")
    async def run_scan(req: ScanRequest) -> dict[str, Any]:
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

        countries = [c.strip().upper() for c in req.countries if c.strip()] if req.countries else None
        sources = [s.strip().lower() for s in req.sources if s.strip()] if req.sources else None

        reports, exported = await run_pipeline(
            query=req.query,
            countries=countries,
            sources=sources,
            output_dir=state.output_dir,
            mock=req.mock,
            send_alerts=req.send_alerts,
            settings=scan_settings,
        )
        state.settings = scan_settings
        state.products = reports
        state.exported = exported
        state.last_scan_meta = {
            "query": req.query,
            "countries": countries or list(scan_settings.TARGET_COUNTRIES),
            "sources": sources or ["meta", "adheart", "tiktok"],
            "mock": req.mock,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return _serialize_report_payload(state)

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
