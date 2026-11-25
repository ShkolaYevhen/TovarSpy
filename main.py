import argparse
import asyncio
import html
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from ad_collector import collect_winning_ads
from china_matcher import match_1688_suppliers
from config import AnalyzedProductReport, MarginResult, Settings, get_settings
from landing_parser import parse_landings
from margin_calculator import calculate_profitability
from notifier import export_reports, send_telegram_alert

logger = logging.getLogger(__name__)


async def run_pipeline(
    query: str = "",
    countries: Optional[list[str]] = None,
    sources: Optional[list[str]] = None,
    output_dir: str | Path = "reports",
    mock: bool = False,
    send_alerts: bool = True,
    settings: Optional[Settings] = None,
) -> tuple[list[AnalyzedProductReport], dict[str, str]]:
    """Run the end-to-end ad spy, landing parser, 1688 matcher, margin, and export pipeline."""
    active_settings = settings or get_settings()
    ads = await collect_winning_ads(
        active_settings,
        query=query,
        countries=countries,
        sources=sources,
        mock=mock,
    )
    landings = await parse_landings([ad.landing_url for ad in ads], mock=mock)

    reports: list[AnalyzedProductReport] = []
    for ad, landing in zip(ads, landings):
        try:
            image_url = landing.image_url or ad.ad_creative_image_url or ""
            suppliers = await match_1688_suppliers(image_url, active_settings, mock=mock)
            best_supplier = suppliers[0] if suppliers else None
            wholesale_usd = best_supplier.wholesale_price_usd if best_supplier else 0.0

            if landing.price is not None and landing.currency:
                margin = calculate_profitability(
                    landing.price,
                    landing.currency,
                    wholesale_usd,
                    active_settings,
                )
            else:
                total_cost = wholesale_usd + active_settings.SHIPPING_COST_EST_USD + active_settings.EST_CPA_USD
                margin = MarginResult(
                    retail_price_usd=0.0,
                    china_wholesale_price_usd=wholesale_usd,
                    shipping_cost_est_usd=active_settings.SHIPPING_COST_EST_USD,
                    est_cpa_usd=active_settings.EST_CPA_USD,
                    net_margin_usd=round(-total_cost, 2),
                    profitability_index=-1.0,
                    is_winner=False,
                )

            report = AnalyzedProductReport(
                ad=ad,
                landing=landing,
                suppliers=suppliers,
                best_supplier=best_supplier,
                margin=margin,
            )
            if send_alerts and report.margin.is_winner:
                await send_telegram_alert(report, active_settings, mock=mock)
            reports.append(report)
        except Exception as exc:
            logger.warning("Skipping ad %s due to pipeline error: %s", ad.ad_id, exc)

    exported = export_reports(reports, output_dir=output_dir)
    return reports, exported


def _render_dashboard_html(items: list[dict]) -> str:
    """Render a minimal HTML table of analyzed product offers."""
    rows_html: list[str] = []
    for item in items:
        ad = item.get("ad", {})
        landing = item.get("landing", {})
        margin = item.get("margin", {})
        best = item.get("best_supplier") or {}
        title = html.escape(str(landing.get("title") or "N/A"))
        platform = html.escape(str(landing.get("platform") or "N/A"))
        countries = html.escape(",".join(ad.get("countries", [])))
        retail = f"${margin.get('retail_price_usd', 0.0):.2f}"
        wholesale = f"${margin.get('china_wholesale_price_usd', 0.0):.2f}"
        net_margin = f"${margin.get('net_margin_usd', 0.0):.2f}"
        winner = "YES" if margin.get("is_winner") else "NO"
        landing_url = html.escape(str(landing.get("final_url") or landing.get("url") or "#"))
        supplier_url = html.escape(str(best.get("product_url") or "#"))
        rows_html.append(
            f"<tr><td>{title}</td><td>{platform}</td><td>{countries}</td>"
            f"<td>{retail}</td><td>{wholesale}</td><td>{net_margin}</td>"
            f"<td><b>{winner}</b></td>"
            f'<td><a href="{landing_url}">Landing</a> | <a href="{supplier_url}">1688</a></td></tr>'
        )
    body_rows = "\n".join(rows_html) if rows_html else '<tr><td colspan="8">No offers found.</td></tr>'
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>TovarSpy Report</title>"
        "<style>body{font-family:sans-serif;margin:24px;}table{border-collapse:collapse;width:100%;}"
        "th,td{border:1px solid #ddd;padding:8px;text-align:left;}th{background:#f4f4f4;}</style></head>"
        "<body><h1>TovarSpy Analyzed Offers</h1>"
        "<table><thead><tr><th>Product</th><th>Platform</th><th>Countries</th>"
        "<th>Retail (USD)</th><th>1688 (USD)</th><th>Net Margin</th><th>Winner</th><th>Links</th>"
        f"</tr></thead><tbody>{body_rows}</tbody></table></body></html>"
    )


def create_web_server(
    report_json_path: Path,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> ThreadingHTTPServer:
    """Create an HTTP server exposing / dashboard and /api/report JSON endpoint."""

    class SpyReportHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            raw_text = report_json_path.read_text(encoding="utf-8") if report_json_path.exists() else "[]"
            if self.path == "/api/report":
                payload = raw_text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if self.path in ("/", "/index.html"):
                items = json.loads(raw_text)
                html_bytes = _render_dashboard_html(items).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
                return

            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), SpyReportHandler)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TovarSpy Trending Product Analyzer")
    parser.add_argument("--query", default="", help="Search keyword for ad spy APIs")
    parser.add_argument("--countries", default="", help="Comma-separated country codes (e.g. UA,PL,US)")
    parser.add_argument("--sources", default="", help="Comma-separated ad sources (meta,adheart,tiktok)")
    parser.add_argument("--min-days", type=int, default=None, help="Minimum active ad days")
    parser.add_argument("--min-ad-sets", type=int, default=None, help="Minimum active ad sets")
    parser.add_argument("--output-dir", default="reports", help="Directory for CSV/JSON reports")
    parser.add_argument("--mock", action="store_true", help="Run offline deterministic dry-run mode")
    parser.add_argument("--no-telegram", action="store_true", help="Disable Telegram alerts")
    parser.add_argument("--serve", action="store_true", help="Start FastAPI web dashboard server")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface for web dashboard")
    parser.add_argument("--port", type=int, default=8000, help="Port for web dashboard")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint for TovarSpy."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _parse_args(argv)
    settings = get_settings()
    if args.min_days is not None:
        settings.MIN_DURATION_DAYS = args.min_days
    if args.min_ad_sets is not None:
        settings.MIN_ACTIVE_AD_SETS = args.min_ad_sets

    countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()] or None
    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()] or None

    should_run_scan = not args.serve or bool(args.query or countries or sources or args.mock)
    if should_run_scan:
        reports, exported = asyncio.run(
            run_pipeline(
                query=args.query,
                countries=countries,
                sources=sources,
                output_dir=args.output_dir,
                mock=args.mock,
                send_alerts=not args.no_telegram,
                settings=settings,
            )
        )
        winners = sum(1 for r in reports if r.margin.is_winner)
        print(f"Analyzed {len(reports)} offers ({winners} winners).")
        print(f"CSV: {exported['csv']}")
        print(f"JSON: {exported['json']}")

    if args.serve:
        import uvicorn

        print(f"Serving TovarSpy FastAPI dashboard at http://{args.host}:{args.port}/")
        uvicorn.run("web_app:app", host=args.host, port=args.port)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
