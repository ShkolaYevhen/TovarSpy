import html
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
import telegram

from config import AnalyzedProductReport, Settings

logger = logging.getLogger(__name__)

_CSV_COLUMNS = [
    "ad_id",
    "source",
    "page_name",
    "countries",
    "days_running",
    "active_ad_sets",
    "landing_url",
    "platform",
    "product_title",
    "offer_price",
    "offer_currency",
    "retail_price_usd",
    "product_image_url",
    "best_supplier_name",
    "best_supplier_url",
    "wholesale_price_rmb",
    "wholesale_price_usd",
    "supplier_rating",
    "supplier_moq",
    "shipping_cost_est_usd",
    "est_cpa_usd",
    "net_margin_usd",
    "profitability_index",
    "is_winner",
    "analyzed_at",
]


def format_winner_alert(product: AnalyzedProductReport) -> str:
    """Format a Telegram HTML alert for a winning product report."""
    title = html.escape(product.landing.title or "Unnamed Product")
    platform = html.escape(product.landing.platform)
    countries = ", ".join(product.ad.countries) if product.ad.countries else "N/A"
    image_url = html.escape(product.landing.image_url or product.ad.ad_creative_image_url or "N/A")

    if product.landing.price is not None and product.landing.currency:
        retail_orig = f"{product.landing.price:.2f} {html.escape(product.landing.currency)}"
    else:
        retail_orig = "N/A"
    retail_usd = f"${product.margin.retail_price_usd:.2f}"

    supplier = product.best_supplier or (product.suppliers[0] if product.suppliers else None)
    if supplier is not None:
        wholesale_str = f"¥{supplier.wholesale_price_rmb:.2f} RMB (${supplier.wholesale_price_usd:.2f})"
        rating_str = f"{supplier.rating:.1f}" if supplier.rating is not None else "N/A"
        supplier_meta = f"Rating: {rating_str} | MOQ: {supplier.moq}"
        supplier_link = f'<a href="{html.escape(supplier.product_url)}">1688 Supplier</a>'
    else:
        wholesale_str = f"${product.margin.china_wholesale_price_usd:.2f}"
        supplier_meta = "No supplier link"
        supplier_link = "1688 Supplier: N/A"

    ad_link = (
        f'<a href="{html.escape(product.ad.ad_creative_image_url)}">Ad Creative</a>'
        if product.ad.ad_creative_image_url
        else "Ad Creative: N/A"
    )
    landing_link = f'<a href="{html.escape(product.landing.final_url or product.landing.url)}">Target Landing Page</a>'

    return (
        f"🏆 <b>IS_WINNER: {product.margin.is_winner}</b>\n"
        f"📸 <b>{title}</b>\n"
        f"Image: {image_url}\n"
        f"Platform: {platform} | Countries: {html.escape(countries)}\n"
        f"💰 <b>Retail vs China Wholesale:</b> {retail_orig} ({retail_usd}) vs {wholesale_str}\n"
        f"📦 {supplier_meta}\n"
        f"📈 <b>Est. Net Margin:</b> ${product.margin.net_margin_usd:.2f} "
        f"(PI: {product.margin.profitability_index:.2f})\n"
        f"🔗 {ad_link} | {landing_link} | {supplier_link}"
    )


async def send_telegram_alert(
    product: AnalyzedProductReport,
    settings: Settings,
    mock: bool = False,
) -> bool:
    """Send a Telegram alert when IS_WINNER is True."""
    if not product.margin.is_winner:
        return False

    message = format_winner_alert(product)
    if mock:
        logger.info("Mock Telegram alert prepared for ad %s", product.ad.ad_id)
        return True

    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing; skipping alert for ad %s", product.ad.ad_id)
        return False

    try:
        bot = telegram.Bot(token=settings.TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=settings.TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="HTML",
        )
        return True
    except Exception as exc:
        logger.warning("Failed to send Telegram alert for ad %s: %s", product.ad.ad_id, exc)
        return False


def _flatten_report(product: AnalyzedProductReport) -> dict[str, Any]:
    """Flatten an AnalyzedProductReport into a tabular CSV row."""
    supplier = product.best_supplier or (product.suppliers[0] if product.suppliers else None)
    return {
        "ad_id": product.ad.ad_id,
        "source": product.ad.source,
        "page_name": product.ad.page_name,
        "countries": ",".join(product.ad.countries),
        "days_running": product.ad.days_running,
        "active_ad_sets": product.ad.active_ad_sets,
        "landing_url": product.landing.final_url or product.landing.url,
        "platform": product.landing.platform,
        "product_title": product.landing.title or "",
        "offer_price": product.landing.price,
        "offer_currency": product.landing.currency or "",
        "retail_price_usd": product.margin.retail_price_usd,
        "product_image_url": product.landing.image_url or "",
        "best_supplier_name": supplier.supplier_name if supplier else "",
        "best_supplier_url": supplier.product_url if supplier else "",
        "wholesale_price_rmb": supplier.wholesale_price_rmb if supplier else None,
        "wholesale_price_usd": product.margin.china_wholesale_price_usd,
        "supplier_rating": supplier.rating if supplier else None,
        "supplier_moq": supplier.moq if supplier else None,
        "shipping_cost_est_usd": product.margin.shipping_cost_est_usd,
        "est_cpa_usd": product.margin.est_cpa_usd,
        "net_margin_usd": product.margin.net_margin_usd,
        "profitability_index": product.margin.profitability_index,
        "is_winner": product.margin.is_winner,
        "analyzed_at": product.analyzed_at.isoformat(),
    }


def export_reports(
    products: list[AnalyzedProductReport],
    output_dir: str | Path,
    base_name: str = "spy_report",
) -> dict[str, str]:
    """Export analyzed product reports to CSV via Pandas and schema-aligned JSON."""
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    csv_path = out_path / f"{base_name}.csv"
    json_path = out_path / f"{base_name}.json"

    rows = [_flatten_report(p) for p in products]
    df = pd.DataFrame(rows, columns=_CSV_COLUMNS)
    df.to_csv(csv_path, index=False)

    json_payload = [p.model_dump(mode="json") for p in products]
    json_path.write_text(json.dumps(json_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {"csv": str(csv_path), "json": str(json_path)}
