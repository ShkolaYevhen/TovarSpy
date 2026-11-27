import argparse
import asyncio
import html
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

import httpx

from ad_collector import collect_winning_ads
from china_matcher import match_1688_suppliers
from config import (
    CURATED_CATEGORIES,
    AdRecord,
    AnalyzedProductReport,
    CoFounderEconomics,
    LandingPageData,
    MarginResult,
    MarketplaceSignal,
    Settings,
    SupplierOffer,
    get_httpx_proxy,
    get_settings,
)
from landing_parser import parse_landings
from margin_calculator import calculate_cofounder_economics, calculate_profitability
from marketplace_intel import (
    analyze_marketplace_intel,
    fetch_live_marketplace_products,
    fetch_ua_wholesale_offers,
)
from notifier import export_reports, send_telegram_alert
from sourcing_engine import (
    fetch_aliexpress_live,
    fetch_google_trends_rss,
    find_zero_cost_suppliers,
    search_telegram_dropshipping,
)
from storage import save_scan
from tyver_client import fetch_tyver_creatives

logger = logging.getLogger(__name__)


async def run_pipeline(
    query: str = "",
    countries: Optional[list[str]] = None,
    sources: Optional[list[str]] = None,
    output_dir: str | Path = "reports",
    mock: bool = False,
    send_alerts: bool = False,
    settings: Optional[Settings] = None,
    include_marketplace: bool = True,
) -> tuple[list[AnalyzedProductReport], dict[str, str]]:
    """Run ad discovery, landing parsing, zero-cost sourcing, marketplace intel, economics, and export."""
    active_settings = settings or get_settings()
    ads = await collect_winning_ads(
        active_settings,
        query=query,
        countries=countries,
        sources=sources,
        mock=mock,
    )
    landings = await parse_landings([ad.landing_url for ad in ads], mock=mock)

    skip_china = bool(active_settings.SKIP_CHINA_SOURCING) and not mock
    reports: list[AnalyzedProductReport] = []
    for ad, landing in zip(ads, landings):
        try:
            image_url = landing.image_url or ad.ad_creative_image_url or ""
            product_title = landing.title or query or ad.page_name

            suppliers_1688 = [] if skip_china else await match_1688_suppliers(image_url, active_settings, mock=mock)
            zero_res = await find_zero_cost_suppliers(
                product_title,
                image_url,
                active_settings,
                mock=mock,
            )

            seen_urls = {s.product_url for s in suppliers_1688}
            suppliers: list[SupplierOffer] = list(suppliers_1688)
            candidate_lists = [zero_res.get("ua_wholesale_offers", []), zero_res.get("china_offers", [])]
            for extra_list in candidate_lists:
                for extra in extra_list:
                    if extra.product_url not in seen_urls:
                        suppliers.append(extra)
                        seen_urls.add(extra.product_url)
            suppliers.sort(key=lambda o: o.wholesale_price_usd)
            suppliers = suppliers[:5]

            best_supplier = suppliers[0] if suppliers else None
            wholesale_usd = best_supplier.wholesale_price_usd if best_supplier else 0.0
            wholesale_uah = best_supplier.wholesale_price_uah if best_supplier else None

            if landing.price is not None and landing.currency:
                margin = calculate_profitability(
                    landing.price,
                    landing.currency,
                    wholesale_usd,
                    active_settings,
                    shipping_usd=0.0 if skip_china else None,
                    cpa_usd=active_settings.UA_CPA_USD if skip_china else None,
                )
            else:
                ship = 0.0 if skip_china else active_settings.SHIPPING_COST_EST_USD
                cpa = active_settings.UA_CPA_USD if skip_china else active_settings.EST_CPA_USD
                total_cost = wholesale_usd + ship + cpa
                margin = MarginResult(
                    retail_price_usd=0.0,
                    china_wholesale_price_usd=wholesale_usd,
                    shipping_cost_est_usd=ship,
                    est_cpa_usd=cpa,
                    net_margin_usd=round(-total_cost, 2),
                    profitability_index=-1.0,
                    is_winner=False,
                )

            mkt_signals = []
            if include_marketplace:
                mkt_report = await analyze_marketplace_intel(
                    product_title,
                    active_settings,
                    platforms=["prom_ua", "olx_ua", "rozetka_ua"] if skip_china else None,
                    mock=mock,
                )
                mkt_signals = mkt_report.signals

            ua_offers = zero_res.get("ua_drop_offers", [])
            best_tg = zero_res.get("best_ua_drop_offer")
            economics = calculate_cofounder_economics(
                retail_price_usd=margin.retail_price_usd,
                china_wholesale_usd=0.0 if skip_china else wholesale_usd,
                settings=active_settings,
                ad_days_running=ad.days_running,
                ad_active_sets=ad.active_ad_sets,
                marketplace_signals=mkt_signals,
                best_tg_offer=best_tg,
                ua_wholesale_usd=wholesale_usd if skip_china and best_supplier else None,
                ua_wholesale_uah=wholesale_uah if skip_china else None,
                skip_china=skip_china,
            )

            report = AnalyzedProductReport(
                ad=ad,
                landing=landing,
                suppliers=suppliers,
                best_supplier=best_supplier,
                margin=margin,
                marketplace_signals=mkt_signals,
                tg_offers=ua_offers,
                economics=economics,
            )
            if send_alerts and report.margin.is_winner:
                await send_telegram_alert(report, active_settings, mock=mock)
            reports.append(report)
        except Exception as exc:
            logger.warning("Skipping ad %s due to pipeline error: %s", ad.ad_id, exc)

    reports.sort(
        key=lambda r: (r.economics.opportunity_score if r.economics else 0.0, r.margin.net_margin_usd),
        reverse=True,
    )
    exported = export_reports(reports, output_dir=output_dir)

    try:
        save_scan(
            active_settings.DB_PATH,
            query=query,
            countries=countries or list(active_settings.TARGET_COUNTRIES),
            sources=sources or ["meta", "adheart", "tiktok"],
            total_count=len(reports),
            winners_count=sum(1 for r in reports if r.margin.is_winner),
            reports=[r.model_dump(mode="json") for r in reports],
        )
    except Exception as exc:
        logger.warning("Could not persist scan history to SQLite: %s", exc)

    return reports, exported


DEFAULT_EXTERNAL_BLOCKS: list[dict[str, str]] = [
    {
        "source": "China / 1688 / AliExpress",
        "status": "skipped",
        "reason": "Skipped by default (SKIP_CHINA_SOURCING=True) to focus strictly on Ukraine local wholesale & COD margins.",
        "action": "Prom.ua wholesale, OLX.ua opt, Bigl.ua, and UA Telegram drop channels provide local UAH pricing without air cargo.",
    },
    {
        "source": "Tyver.io Ad Spy",
        "status": "configured",
        "reason": "Supports live iGaming (ai_category=GAMBLING) and UA e-com ad creatives via X-Tyver-Authorization.",
        "action": "Use the Tyver Ad Spy tab for iGaming verticals and active UA Facebook/Instagram creatives.",
    },
    {
        "source": "Telegram UA Drop Channels",
        "status": "optional",
        "reason": "Requires real public @channel handles (https://t.me/s/<handle>) configured in the UA Telegram Drops tab.",
        "action": "Add active public Ukrainian drop channel handles in the UI to merge local UAH drop prices.",
    },
    {
        "source": "Zakupka / Rozetka / Meta Direct",
        "status": "blocked",
        "reason": "Protected by anti-bot challenges or Meta verification requirements on datacenter IPs (never bypassed).",
        "action": "Use Prom.ua wholesale, OLX.ua opt, Bigl.ua, and Tyver.io API instead.",
    },
]


def _normalize_title_key(title: str) -> str:
    return re.sub(r"[^a-zа-яіїєґ0-9]+", "", title.lower())[:40]


def _pick_matching_ua_wholesale(
    retail_uah: float,
    wholesale_offers: list[SupplierOffer],
) -> Optional[SupplierOffer]:
    """Pick the cheapest UA wholesale offer whose UAH price stays below retail (or lowest overall)."""
    if not wholesale_offers:
        return None
    cheaper = [
        o for o in wholesale_offers
        if (o.wholesale_price_uah or 0.0) > 0 and (o.wholesale_price_uah or 0.0) < retail_uah
    ]
    if cheaper:
        return min(cheaper, key=lambda o: o.wholesale_price_uah or 0.0)
    return min(wholesale_offers, key=lambda o: o.wholesale_price_usd)


async def run_super_parse(
    query: str = "",
    categories: Optional[list[str]] = None,
    countries: Optional[list[str]] = None,
    geo_trends: str = "UA",
    output_dir: str | Path = "reports",
    mock: bool = False,
    settings: Optional[Settings] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """Run Ukraine-focused super-parse across Prom.ua, OLX.ua, Bigl.ua, Tyver UA, Trends, and TG."""
    active_settings = settings or get_settings()
    if mock:
        reports, exported = await run_pipeline(
            query=query,
            countries=countries or ["UA"],
            output_dir=output_dir,
            mock=True,
            send_alerts=False,
            settings=active_settings,
        )
        return {
            "reports": reports,
            "exported": exported,
            "source_diagnostics": {"demo": f"mock: {len(reports)} items"},
            "trending_keywords": [],
            "external_blocks": DEFAULT_EXTERNAL_BLOCKS,
        }

    skip_china = bool(active_settings.SKIP_CHINA_SOURCING)
    clean_q = query.strip()
    if clean_q:
        cat_tasks = [("custom", {"label": "Custom Search", "en": clean_q, "ua": clean_q, "pl": clean_q})]
    else:
        keys = categories or list(CURATED_CATEGORIES.keys())
        cat_tasks = [(k, CURATED_CATEGORIES[k]) for k in keys if k in CURATED_CATEGORIES]
        if not cat_tasks:
            cat_tasks = list(CURATED_CATEGORIES.items())

    async def _execute(http_client: httpx.AsyncClient) -> tuple[list[dict[str, str]], list[tuple[Any, ...]]]:
        trends_task = fetch_google_trends_rss(geo=geo_trends or "UA", settings=active_settings, client=http_client)

        async def _scan_cat(cat_key: str, cat_cfg: dict[str, str]):
            ua_retail_task = fetch_live_marketplace_products(
                cat_cfg["ua"],
                active_settings,
                platforms=["prom_ua", "olx_ua"],
                limit_per_platform=6,
                client=http_client,
            )
            ua_ws_task = fetch_ua_wholesale_offers(
                cat_cfg["ua"], active_settings, limit_per_source=6, client=http_client
            )
            tg_task = search_telegram_dropshipping(cat_cfg["ua"], active_settings, client=http_client)

            if skip_china:
                ae_task = asyncio.sleep(0, result=[])
                pl_task = asyncio.sleep(0, result=([], {}))
            else:
                ae_task = fetch_aliexpress_live(cat_cfg["en"], active_settings, limit=6, client=http_client)
                pl_task = fetch_live_marketplace_products(
                    cat_cfg["pl"], active_settings, platforms=["olx_pl"], limit_per_platform=4, client=http_client
                )

            if active_settings.TYVER_API_KEY and cat_key in (cat_tasks[0][0], "custom"):
                tyver_task = fetch_tyver_creatives(
                    active_settings.TYVER_API_KEY,
                    mode="ecom_ua",
                    query=cat_cfg["ua"],
                    countries=["UA"],
                    page_size=4,
                    client=http_client,
                )
            else:
                tyver_task = asyncio.sleep(0, result={"ok": False, "items": []})

            ae_items, (ua_retail, ua_diag), (ua_ws, ws_diag), (pl_items, pl_diag), tg_items, tyver_res = await asyncio.gather(
                ae_task, ua_retail_task, ua_ws_task, pl_task, tg_task, tyver_task
            )
            merged_diag = {**ua_diag, **ws_diag, **pl_diag}
            merged_diag["china_1688"] = "skipped (UA-only mode)" if skip_china else (f"live: {len(ae_items)} items" if ae_items else "empty: 0 items")
            merged_diag["telegram"] = f"live: {len(tg_items)} offers" if tg_items else "0 channels/matches"
            tyver_items = tyver_res.get("items", []) if isinstance(tyver_res, dict) and tyver_res.get("ok") else []
            if active_settings.TYVER_API_KEY:
                merged_diag["tyver_ua"] = f"live: {len(tyver_items)} creatives" if tyver_items else (tyver_res.get("error") or "0 creatives")
            return cat_key, cat_cfg, ae_items, ua_retail + pl_items, ua_ws, merged_diag, tg_items, tyver_items

        cat_results = await asyncio.gather(*(_scan_cat(k, c) for k, c in cat_tasks))
        trends = await trends_task
        return trends, list(cat_results)

    if client is not None:
        trends, cat_results = await _execute(client)
    else:
        proxy = get_httpx_proxy(active_settings)
        async with httpx.AsyncClient(timeout=15.0, proxy=proxy, follow_redirects=True) as owned:
            trends, cat_results = await _execute(owned)

    reports: list[AnalyzedProductReport] = []
    seen_keys: set[str] = set()
    agg_diag: dict[str, int] = {
        "prom_ua": 0,
        "prom_wholesale": 0,
        "olx_ua": 0,
        "olx_opt": 0,
        "bigl_ua": 0,
        "telegram": 0,
        "tyver_ua": 0,
        "aliexpress": 0,
    }
    last_status: dict[str, str] = {}
    uah_rate = active_settings.FX_RATES_TO_USD.get("UAH", 0.024)

    for _cat_key, cat_cfg, ae_items, mkt_items, ua_ws, diag, tg_items, tyver_items in cat_results:
        for k, v in diag.items():
            last_status[k] = v
        agg_diag["aliexpress"] += len(ae_items)
        agg_diag["telegram"] += len(tg_items)
        agg_diag["tyver_ua"] += len(tyver_items)
        for mi in mkt_items:
            src = mi.get("source", "")
            if src in agg_diag:
                agg_diag[src] += 1
        for ws in ua_ws:
            if ws.source in agg_diag:
                agg_diag[ws.source] += 1

        # Retail UAH/USD benchmark for this category
        ua_retail_prices = sorted(float(i["price"]) for i in mkt_items if i.get("currency") == "UAH" and float(i.get("price") or 0) > 0)
        ref_retail_uah = ua_retail_prices[len(ua_retail_prices) // 2] if ua_retail_prices else 990.0
        ref_retail_usd = round(ref_retail_uah * uah_rate, 2)

        cat_signals: list[MarketplaceSignal] = []
        for plat in ("prom_ua", "olx_ua", "olx_pl"):
            p_items = [i for i in mkt_items if i.get("source") == plat]
            if p_items:
                p_vals = sorted(i["price"] for i in p_items)
                med = p_vals[len(p_vals) // 2]
                cat_signals.append(
                    MarketplaceSignal(
                        source=plat,
                        platform=plat,
                        country=p_items[0]["country"],
                        query=cat_cfg["label"],
                        listing_count=len(p_items),
                        median_price=med,
                        currency=p_items[0]["currency"],
                        median_price_usd=p_items[len(p_items) // 2]["price_usd"],
                        sold_velocity_score=min(90.0, float(len(p_items) * 10)),
                        seller_density=len(p_items),
                        seller_count=len(p_items),
                        top_sample_url=p_items[0].get("product_url"),
                        status="live",
                    )
                )

        # 1. Retail UA listings paired with cheapest matching UA wholesale supplier
        for idx, item in enumerate(mkt_items[:6]):
            t_key = _normalize_title_key(item["title"])
            if not t_key or t_key in seen_keys:
                continue
            seen_keys.add(t_key)
            retail_uah = float(item["price"]) if item["currency"] == "UAH" else round(float(item["price_usd"]) * active_settings.USD_TO_UAH, 2)
            matched_ws = _pick_matching_ua_wholesale(retail_uah, ua_ws) if skip_china else (ae_items[0] if ae_items else None)
            if matched_ws is not None:
                ws_usd = matched_ws.wholesale_price_usd
                ws_uah = matched_ws.wholesale_price_uah or round(ws_usd * active_settings.USD_TO_UAH, 2)
            elif tg_items:
                ws_uah = tg_items[0].drop_price_uah
                ws_usd = round(ws_uah * uah_rate, 2)
            else:
                ws_uah = round(retail_uah * 0.45, 2) if skip_china else round(item["price_usd"] * 0.35, 2)
                ws_usd = round(ws_uah * uah_rate, 2)

            margin = calculate_profitability(
                item["price"],
                item["currency"],
                ws_usd,
                active_settings,
                shipping_usd=0.0 if skip_china else None,
                cpa_usd=active_settings.UA_CPA_USD if skip_china else None,
            )
            econ = calculate_cofounder_economics(
                retail_price_usd=margin.retail_price_usd,
                china_wholesale_usd=0.0 if skip_china else ws_usd,
                settings=active_settings,
                ad_days_running=10,
                ad_active_sets=6,
                marketplace_signals=cat_signals,
                best_tg_offer=tg_items[0] if tg_items else None,
                ua_wholesale_usd=ws_usd if skip_china else None,
                ua_wholesale_uah=ws_uah if skip_china else None,
                ua_retail_uah=retail_uah if skip_china else None,
                skip_china=skip_china,
            )
            sup_pool = (ua_ws[:4] if skip_china else ae_items[:3])
            best_sup = matched_ws or (sup_pool[0] if sup_pool else None)
            reports.append(
                AnalyzedProductReport(
                    ad=AdRecord(
                        ad_id=f"{item['source']}-{_cat_key}-{idx + 1}",
                        page_name=item.get("seller_name") or item["source"].upper(),
                        landing_url=item["product_url"],
                        days_running=10,
                        ad_creative_image_url=item.get("image_url") or None,
                        active_ad_sets=max(5, len(mkt_items)),
                        countries=[item["country"]],
                        source=item["source"],
                        is_live=True,
                    ),
                    landing=LandingPageData(
                        url=item["product_url"],
                        final_url=item["product_url"],
                        title=item["title"],
                        price=item["price"],
                        currency=item["currency"],
                        image_url=item.get("image_url") or None,
                        platform=item["source"].upper(),
                    ),
                    suppliers=sup_pool,
                    best_supplier=best_sup,
                    margin=margin,
                    marketplace_signals=cat_signals,
                    tg_offers=tg_items[:3],
                    economics=econ,
                    category=cat_cfg["label"],
                    source_diagnostics=diag,
                )
            )

        # 2. Direct UA local wholesale bargains (or legacy AliExpress if skip_china=False)
        direct_offers = ua_ws[:4] if skip_china else ae_items[:4]
        for idx, sup in enumerate(direct_offers):
            t_key = _normalize_title_key(sup.product_title)
            if not t_key or t_key in seen_keys:
                continue
            seen_keys.add(t_key)
            if skip_china:
                ws_uah = sup.wholesale_price_uah or round(sup.wholesale_price_usd * active_settings.USD_TO_UAH, 2)
                retail_uah = ref_retail_uah if ref_retail_uah > ws_uah * 1.35 else round(max(ws_uah * 2.1, ws_uah + 350.0), 2)
                margin = calculate_profitability(
                    retail_uah,
                    "UAH",
                    sup.wholesale_price_usd,
                    active_settings,
                    shipping_usd=0.0,
                    cpa_usd=active_settings.UA_CPA_USD,
                )
                econ = calculate_cofounder_economics(
                    retail_price_usd=margin.retail_price_usd,
                    china_wholesale_usd=0.0,
                    settings=active_settings,
                    ad_days_running=12,
                    ad_active_sets=7,
                    marketplace_signals=cat_signals,
                    best_tg_offer=tg_items[0] if tg_items else None,
                    ua_wholesale_usd=sup.wholesale_price_usd,
                    ua_wholesale_uah=ws_uah,
                    ua_retail_uah=retail_uah,
                    skip_china=True,
                )
                land_price, land_curr, land_plat = retail_uah, "UAH", f"{sup.source.upper()} (UA Opt)"
            else:
                retail_usd = ref_retail_usd if ref_retail_usd > sup.wholesale_price_usd else round(max(sup.wholesale_price_usd * 2.6, 19.99), 2)
                margin = calculate_profitability(retail_usd, "USD", sup.wholesale_price_usd, active_settings)
                econ = calculate_cofounder_economics(
                    retail_price_usd=retail_usd,
                    china_wholesale_usd=sup.wholesale_price_usd,
                    settings=active_settings,
                    ad_days_running=14,
                    ad_active_sets=8,
                    marketplace_signals=cat_signals,
                    best_tg_offer=tg_items[0] if tg_items else None,
                    skip_china=False,
                )
                land_price, land_curr, land_plat = retail_usd, "USD", "AliExpress Live"

            reports.append(
                AnalyzedProductReport(
                    ad=AdRecord(
                        ad_id=f"{sup.source}-{_cat_key}-{idx + 1}",
                        page_name=sup.supplier_name,
                        landing_url=sup.product_url,
                        days_running=12,
                        ad_creative_image_url=sup.image_url,
                        active_ad_sets=7,
                        countries=["UA"] if skip_china else ["UA", "PL", "US"],
                        source=sup.source,
                        is_live=True,
                    ),
                    landing=LandingPageData(
                        url=sup.product_url,
                        final_url=sup.product_url,
                        title=sup.product_title,
                        price=land_price,
                        currency=land_curr,
                        image_url=sup.image_url,
                        platform=land_plat,
                    ),
                    suppliers=[sup] + [s for s in direct_offers if s.product_url != sup.product_url][:3],
                    best_supplier=sup,
                    margin=margin,
                    marketplace_signals=cat_signals,
                    tg_offers=tg_items[:3],
                    economics=econ,
                    category=cat_cfg["label"],
                    source_diagnostics=diag,
                )
            )

        # 3. Optional Tyver UA creatives enrichment
        for idx, tc in enumerate(tyver_items[:2]):
            if not tc.get("landing_url"):
                continue
            t_key = _normalize_title_key(tc.get("title") or tc.get("page_name") or "")
            if not t_key or t_key in seen_keys:
                continue
            seen_keys.add(t_key)
            best_ws = _pick_matching_ua_wholesale(ref_retail_uah, ua_ws)
            ws_usd = best_ws.wholesale_price_usd if best_ws else round(ref_retail_usd * 0.4, 2)
            ws_uah = (best_ws.wholesale_price_uah if best_ws else None) or round(ws_usd * active_settings.USD_TO_UAH, 2)
            margin = calculate_profitability(
                ref_retail_uah,
                "UAH",
                ws_usd,
                active_settings,
                shipping_usd=0.0 if skip_china else None,
                cpa_usd=active_settings.UA_CPA_USD if skip_china else None,
            )
            econ = calculate_cofounder_economics(
                retail_price_usd=margin.retail_price_usd,
                china_wholesale_usd=0.0 if skip_china else ws_usd,
                settings=active_settings,
                ad_days_running=max(7, int(tc.get("activity_days") or 7)),
                ad_active_sets=8,
                marketplace_signals=cat_signals,
                best_tg_offer=tg_items[0] if tg_items else None,
                ua_wholesale_usd=ws_usd if skip_china else None,
                ua_wholesale_uah=ws_uah if skip_china else None,
                ua_retail_uah=ref_retail_uah if skip_china else None,
                skip_china=skip_china,
            )
            reports.append(
                AnalyzedProductReport(
                    ad=AdRecord(
                        ad_id=f"tyver-{tc.get('id') or idx}",
                        page_name=tc.get("page_name") or "Tyver Advertiser",
                        landing_url=tc["landing_url"],
                        days_running=int(tc.get("activity_days") or 7),
                        ad_creative_image_url=tc.get("image_url") or tc.get("thumbnail_url"),
                        active_ad_sets=8,
                        countries=tc.get("countries") or ["UA"],
                        source="tyver",
                        is_live=True,
                    ),
                    landing=LandingPageData(
                        url=tc["landing_url"],
                        final_url=tc["landing_url"],
                        title=tc.get("title") or tc.get("page_name") or "Tyver Creative",
                        price=ref_retail_uah,
                        currency="UAH",
                        image_url=tc.get("image_url") or tc.get("thumbnail_url"),
                        platform="Tyver Meta Ad",
                    ),
                    suppliers=ua_ws[:3],
                    best_supplier=best_ws,
                    margin=margin,
                    marketplace_signals=cat_signals,
                    tg_offers=tg_items[:3],
                    economics=econ,
                    category=cat_cfg["label"],
                    source_diagnostics=diag,
                )
            )

    reports.sort(
        key=lambda r: (r.economics.opportunity_score if r.economics else 0.0, r.margin.net_margin_usd),
        reverse=True,
    )
    exported = export_reports(reports, output_dir=output_dir)

    source_diagnostics = {
        "china_1688": "skipped (UA-only mode)" if skip_china else (f"live: {agg_diag['aliexpress']} items" if agg_diag["aliexpress"] else "empty"),
        "prom_ua": f"live: {agg_diag['prom_ua']} items" if agg_diag["prom_ua"] else last_status.get("prom_ua", "empty"),
        "prom_wholesale": f"live: {agg_diag['prom_wholesale']} items" if agg_diag["prom_wholesale"] else last_status.get("prom_wholesale", "empty"),
        "olx_ua": f"live: {agg_diag['olx_ua']} items" if agg_diag["olx_ua"] else last_status.get("olx_ua", "empty"),
        "olx_opt": f"live: {agg_diag['olx_opt']} items" if agg_diag["olx_opt"] else last_status.get("olx_opt", "empty"),
        "bigl_ua": f"live: {agg_diag['bigl_ua']} items" if agg_diag["bigl_ua"] else last_status.get("bigl_ua", "empty"),
        "google_trends": f"live: {len(trends)} topics ({(geo_trends or 'UA').upper()})" if trends else "empty",
        "telegram": f"live: {agg_diag['telegram']} drop offers" if agg_diag["telegram"] else "0 active channels",
    }
    if active_settings.TYVER_API_KEY:
        source_diagnostics["tyver_ua"] = f"live: {agg_diag['tyver_ua']} creatives" if agg_diag["tyver_ua"] else last_status.get("tyver_ua", "0 creatives")

    try:
        save_scan(
            active_settings.DB_PATH,
            query=clean_q or "super-parse:ua-wholesale",
            countries=countries or ["UA"],
            sources=["prom_ua", "prom_wholesale", "olx_ua", "olx_opt", "bigl_ua", "google_trends"],
            total_count=len(reports),
            winners_count=sum(1 for r in reports if r.margin.is_winner),
            reports=[r.model_dump(mode="json") for r in reports],
        )
    except Exception as exc:
        logger.warning("Could not persist super-parse scan to SQLite: %s", exc)

    return {
        "reports": reports,
        "exported": exported,
        "source_diagnostics": source_diagnostics,
        "trending_keywords": trends,
        "external_blocks": DEFAULT_EXTERNAL_BLOCKS,
    }


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
