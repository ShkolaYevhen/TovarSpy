from config import MarginResult, Settings


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
