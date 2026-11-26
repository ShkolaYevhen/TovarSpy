from typing import Optional

from config import (
    CoFounderEconomics,
    MarginResult,
    MarketplaceSignal,
    Settings,
    TelegramDropOffer,
)


def _clamp(val: float, low: float = 0.0, high: float = 100.0) -> float:
    return round(max(low, min(high, val)), 2)


def convert_to_usd(amount: float, currency: str, settings: Settings) -> float:
    """Convert an amount in a supported currency (UAH, USD, EUR, PLN, RMB/CNY) to USD."""
    if amount < 0:
        raise ValueError(f"Amount must be non-negative, got {amount}")
    code = currency.strip().upper()
    rate = settings.FX_RATES_TO_USD.get(code)
    if rate is None:
        raise ValueError(f"Unsupported currency: {currency}")
    return round(amount * rate, 2)


def calculate_profitability(
    offer_price: float,
    currency: str,
    wholesale_price_usd: float,
    settings: Settings,
) -> MarginResult:
    """Calculate Net_Margin, Profitability Index, and IS_WINNER flag."""
    if wholesale_price_usd < 0:
        raise ValueError(f"Wholesale price must be non-negative, got {wholesale_price_usd}")

    retail_price_usd = convert_to_usd(offer_price, currency, settings)
    shipping = settings.SHIPPING_COST_EST_USD
    cpa = settings.EST_CPA_USD
    total_cost = wholesale_price_usd + shipping + cpa
    net_margin = round(retail_price_usd - total_cost, 2)
    profitability_index = round(net_margin / total_cost, 4) if total_cost > 0 else 0.0
    is_winner = net_margin >= settings.WINNER_NET_MARGIN_USD

    return MarginResult(
        retail_price_usd=retail_price_usd,
        china_wholesale_price_usd=round(wholesale_price_usd, 2),
        shipping_cost_est_usd=shipping,
        est_cpa_usd=cpa,
        net_margin_usd=net_margin,
        profitability_index=profitability_index,
        is_winner=is_winner,
    )


def calculate_cofounder_economics(
    retail_price_usd: float,
    china_wholesale_usd: float,
    settings: Settings,
    ad_days_running: int = 7,
    ad_active_sets: int = 5,
    marketplace_signals: Optional[list[MarketplaceSignal]] = None,
    best_tg_offer: Optional[TelegramDropOffer] = None,
    *,
    ua_return_rate: Optional[float] = None,
    ua_cpa_usd: Optional[float] = None,
    cross_border_cpa_usd: Optional[float] = None,
    shipping_usd: Optional[float] = None,
    marketplace_fee_pct: Optional[float] = None,
    payment_fee_pct: Optional[float] = None,
) -> CoFounderEconomics:
    """Compute UA Local COD vs Cross-border unit economics and 0-100 Opportunity Score."""
    signals = marketplace_signals or []

    # Model A: UA Local COD (Nova Poshta buyout/return drag)
    r = settings.UA_COD_RETURN_RATE if ua_return_rate is None else max(0.0, min(0.95, ua_return_rate))
    cpa_ua = settings.UA_CPA_USD if ua_cpa_usd is None else max(0.0, ua_cpa_usd)
    return_fee = settings.UA_NOVA_POSHTA_RETURN_FEE_USD

    if best_tg_offer is not None and best_tg_offer.drop_price_uah > 0:
        ua_wholesale_usd = convert_to_usd(best_tg_offer.drop_price_uah, "UAH", settings)
        ua_retail_usd = (
            convert_to_usd(best_tg_offer.rrc_price_uah, "UAH", settings)
            if best_tg_offer.rrc_price_uah > 0
            else max(0.0, retail_price_usd)
        )
    else:
        ua_wholesale_usd = round(max(0.0, china_wholesale_usd) * 1.35, 2)
        ua_signal_usd = next(
            (s.median_price_usd for s in signals if s.country == "UA" and s.median_price_usd > 0),
            0.0,
        )
        ua_retail_usd = max(0.0, retail_price_usd) or ua_signal_usd

    buyout_rate = 1.0 - r
    ua_return_drag_usd = round(r * return_fee, 2)
    expected_ua_gross = buyout_rate * (ua_retail_usd - ua_wholesale_usd)
    ua_cod_net_profit_usd = round(expected_ua_gross - ua_return_drag_usd - cpa_ua, 2)
    ua_cost_basis = (buyout_rate * ua_wholesale_usd) + ua_return_drag_usd + cpa_ua
    ua_cod_roi_pct = round((ua_cod_net_profit_usd / ua_cost_basis) * 100.0, 2) if ua_cost_basis > 0 else 0.0

    # Model B: Cross-Border EU/PL/US (China COGS + shipping + CPA + platform & payment fees)
    ship = settings.SHIPPING_COST_EST_USD if shipping_usd is None else max(0.0, shipping_usd)
    cpa_cb = settings.EST_CPA_USD if cross_border_cpa_usd is None else max(0.0, cross_border_cpa_usd)
    mkt_fee = settings.MARKETPLACE_FEE_PCT if marketplace_fee_pct is None else max(0.0, marketplace_fee_pct)
    pay_fee = settings.PAYMENT_FEE_PCT if payment_fee_pct is None else max(0.0, payment_fee_pct)

    cb_retail = max(0.0, retail_price_usd)
    fees_usd = cb_retail * (mkt_fee + pay_fee)
    cb_total_cost = max(0.0, china_wholesale_usd) + ship + cpa_cb + fees_usd
    cross_border_net_profit_usd = round(cb_retail - cb_total_cost, 2)
    cross_border_roi_pct = (
        round((cross_border_net_profit_usd / cb_total_cost) * 100.0, 2) if cb_total_cost > 0 else 0.0
    )

    # Composite Scoring (0-100)
    ad_vel = min(100.0, (max(0, ad_days_running) * 2.5) + (max(0, ad_active_sets) * 4.0))
    mkt_vels = [s.sold_velocity_score for s in signals if s.sold_velocity_score > 0]
    if mkt_vels:
        demand_velocity_score = _clamp(0.45 * ad_vel + 0.55 * (sum(mkt_vels) / len(mkt_vels)))
    else:
        demand_velocity_score = _clamp(ad_vel)

    densities = [s.seller_density for s in signals if s.seller_density > 0]
    if densities:
        avg_density = sum(densities) / len(densities)
        competition_score = _clamp((avg_density / 150.0) * 100.0)
    else:
        competition_score = 25.0

    best_profit = max(ua_cod_net_profit_usd, cross_border_net_profit_usd)
    best_roi = max(ua_cod_roi_pct, cross_border_roi_pct)
    profit_score = _clamp((max(0.0, best_profit) / 25.0) * 60.0 + (max(0.0, best_roi) / 150.0) * 40.0)
    opportunity_score = _clamp(
        0.35 * demand_velocity_score
        + 0.40 * profit_score
        + 0.25 * (100.0 - competition_score)
    )

    if best_profit < 3.0:
        recommended_model = "PASS"
    elif ua_cod_net_profit_usd >= cross_border_net_profit_usd:
        recommended_model = "UA_COD"
    else:
        recommended_model = "CROSS_BORDER"

    return CoFounderEconomics(
        ua_cod_net_profit_usd=ua_cod_net_profit_usd,
        ua_cod_roi_pct=ua_cod_roi_pct,
        ua_return_drag_usd=ua_return_drag_usd,
        cross_border_net_profit_usd=cross_border_net_profit_usd,
        cross_border_roi_pct=cross_border_roi_pct,
        demand_velocity_score=demand_velocity_score,
        competition_score=competition_score,
        opportunity_score=opportunity_score,
        recommended_model=recommended_model,
    )
