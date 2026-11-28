from datetime import datetime, timezone
from typing import Any, Optional
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_FX_RATES_TO_USD: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.08,
    "PLN": 0.25,
    "UAH": 0.024,
    "RMB": 0.14,
    "CNY": 0.14,
}


# Ad discovery & COD filtering thresholds
COD_DISCOVERY_SIGNALS: tuple[str, ...] = (
    "Оплата при отриманні",
    "Оплата після отримання",
    "Нова Пошта",
    "замовити зі знижкою",
)
DROPSHIP_DOMAIN_TLDS: tuple[str, ...] = (
    ".store",
    ".shop",
    ".space",
    ".online",
    ".site",
    ".top",
    ".com.ua",
)
DROPSHIP_CTAS: tuple[str, ...] = ("Shop Now", "Order Now", "Buy Now")
MARKETPLACE_BLOCKLIST: tuple[str, ...] = (
    "rozetka.com.ua",
    "comfy.ua",
    "epicentrk.ua",
    "prom.ua",
    "olx.ua",
    "allo.ua",
    "foxtrot.com.ua",
    "eva.ua",
    "makeup.com.ua",
    "shafa.ua",
    "kasta.ua",
)
MIN_DURATION_DAYS: int = 3
MAX_AD_AGE_DAYS: int = 14
MIN_ACTIVE_AD_SETS: int = 5
MIN_EST_SPEND_USD: float = 100.0
EST_DAILY_SPEND_PER_CREATIVE_USD: float = 8.0


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # API credentials
    META_ACCESS_TOKEN: Optional[str] = None
    ADHEART_API_KEY: Optional[str] = None
    TIKTOK_ACCESS_TOKEN: Optional[str] = None
    TYVER_API_KEY: Optional[str] = None
    RAPIDAPI_1688_KEY: Optional[str] = None
    RAPIDAPI_1688_HOST: str = "1688-datahub.p.rapidapi.com"
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None
    SKIP_CHINA_SOURCING: bool = True

    # Ad filtering thresholds
    TARGET_COUNTRIES: list[str] = Field(
        default_factory=lambda: ["UA", "PL", "RO", "CZ", "DE", "US"]
    )
    MIN_DURATION_DAYS: int = MIN_DURATION_DAYS
    MAX_AD_AGE_DAYS: int = MAX_AD_AGE_DAYS
    MIN_ACTIVE_AD_SETS: int = MIN_ACTIVE_AD_SETS
    MIN_EST_SPEND_USD: float = MIN_EST_SPEND_USD
    EST_DAILY_SPEND_PER_CREATIVE_USD: float = EST_DAILY_SPEND_PER_CREATIVE_USD

    # Margin & profitability constants
    SHIPPING_COST_EST_USD: float = 2.50
    EST_CPA_USD: float = 6.00
    WINNER_NET_MARGIN_USD: float = 10.00
    USD_TO_UAH: float = 41.67
    FX_RATES_TO_USD: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_FX_RATES_TO_USD)
    )

    # Output & persistence configuration
    EXPORT_DIR: str = "exports"
    DB_PATH: str = "output/tovarspy.db"
    PROXY_URL: str = ""
    CACHE_TTL_SECONDS: int = 21600
    DEFAULT_TG_CHANNELS: list[str] = Field(default_factory=list)

    # Co-founder unit economics defaults
    UA_COD_RETURN_RATE: float = 0.22
    UA_NOVA_POSHTA_RETURN_FEE_USD: float = 2.60
    UA_CPA_USD: float = 3.50
    MARKETPLACE_FEE_PCT: float = 0.10
    PAYMENT_FEE_PCT: float = 0.029


CURATED_CATEGORIES: dict[str, dict[str, str]] = {
    "home_kitchen": {
        "label": "Home & Kitchen",
        "en": "led sunset lamp",
        "ua": "кухонний гаджет",
        "pl": "organizer kuchenny",
    },
    "beauty_wellness": {
        "label": "Beauty & Wellness",
        "en": "neck massager",
        "ua": "масажер для шиї",
        "pl": "masazer karku",
    },
    "auto_car": {
        "label": "Auto & Car",
        "en": "mini car vacuum",
        "ua": "автопилосос",
        "pl": "odkurzacz samochodowy",
    },
    "pet_care": {
        "label": "Pet Care",
        "en": "steam pet brush",
        "ua": "щітка для тварин",
        "pl": "szczotka dla psa",
    },
    "smart_gadgets": {
        "label": "Smart Gadgets",
        "en": "mini thermal printer",
        "ua": "міні принтер",
        "pl": "mini drukarka",
    },
    "fitness_posture": {
        "label": "Fitness & Posture",
        "en": "posture corrector",
        "ua": "коректор постави",
        "pl": "korektor postawy",
    },
}


class AdRecord(BaseModel):
    """Normalized active ad or discovered live product record."""

    ad_id: str
    page_name: str
    landing_url: str
    days_running: int = Field(ge=0)
    ad_creative_image_url: Optional[str] = None
    active_ad_sets: int = Field(default=1, ge=1)
    est_spend_usd: float = Field(default=0.0, ge=0)
    countries: list[str] = Field(default_factory=list)
    source: str = "meta"
    is_live: bool = True


class LandingPageData(BaseModel):
    """Extracted product metadata and platform fingerprint from a landing page."""

    url: str
    final_url: Optional[str] = None
    title: Optional[str] = None
    price: Optional[float] = Field(default=None, ge=0)
    currency: Optional[str] = None
    image_url: Optional[str] = None
    fallback_image_url: Optional[str] = None
    platform: str = "Custom PHP"
    core_phrase: str = ""
    model_code: str = ""
    is_cod_lander: bool = False
    cod_score: int = Field(default=0, ge=0, le=100)
    cod_signals: list[str] = Field(default_factory=list)
    is_generic_catalog: bool = False


class SupplierOffer(BaseModel):
    """Matched wholesale supplier offer (UA local wholesale or legacy 1688/Alibaba)."""

    supplier_name: str
    product_title: str
    product_url: str
    wholesale_price_rmb: float = Field(default=0.0, ge=0)
    wholesale_price_usd: float = Field(ge=0)
    wholesale_price_uah: Optional[float] = Field(default=None, ge=0)
    rating: Optional[float] = Field(default=None, ge=0, le=5.0)
    years_on_1688: Optional[int] = Field(default=None, ge=0)
    moq: int = Field(default=1, ge=1)
    image_url: Optional[str] = None
    source: str = "ua_wholesale"
    supplier_type: str = ""
    sales_count_text: str = ""
    match_score: float = Field(default=0.0, ge=0, le=100)
    match_label: str = ""
    is_live: bool = True


class MarginResult(BaseModel):
    """Profitability calculation output for a product offer."""

    retail_price_usd: float = Field(ge=0)
    china_wholesale_price_usd: float = Field(ge=0)
    shipping_cost_est_usd: float = 2.50
    est_cpa_usd: float = 6.00
    net_margin_usd: float
    profitability_index: float = 0.0
    is_winner: bool = False


class MarketplaceSignal(BaseModel):
    """Public marketplace demand, pricing, and seller saturation snapshot."""

    source: str = ""
    platform: str = ""
    country: str = ""
    query: str
    listing_count: int = Field(default=0, ge=0)
    sold_count: int = Field(default=0, ge=0)
    median_price: float = Field(default=0.0, ge=0)
    min_price: float = Field(default=0.0, ge=0)
    max_price: float = Field(default=0.0, ge=0)
    currency: str = "USD"
    median_price_usd: float = Field(default=0.0, ge=0)
    sold_velocity_score: float = Field(default=0.0, ge=0, le=100)
    seller_density: int = Field(default=0, ge=0)
    seller_count: int = Field(default=0, ge=0)
    review_velocity: float = Field(default=0.0, ge=0)
    top_sample_url: Optional[str] = None
    status: str = "empty"
    blocked: bool = False
    note: str = ""


class MarketplaceIntelReport(BaseModel):
    """Aggregated marketplace demand, saturation, and regional pricing report."""

    query: str
    signals: list[MarketplaceSignal] = Field(default_factory=list)
    demand_velocity_score: float = Field(default=0.0, ge=0, le=100)
    saturation_score: float = Field(default=0.0, ge=0, le=100)
    ua_median_price_uah: Optional[float] = None
    pl_median_price_pln: Optional[float] = None
    us_median_price_usd: Optional[float] = None


class TelegramDropOffer(BaseModel):
    """Wholesale dropship offer parsed from a public Ukrainian Telegram channel."""

    channel: str
    post_url: str
    title: str
    image_url: str = ""
    drop_price_uah: float = Field(default=0.0, ge=0)
    rrc_price_uah: float = Field(default=0.0, ge=0)
    drop_price_usd: float = Field(default=0.0, ge=0)
    rrc_price_usd: float = Field(default=0.0, ge=0)
    in_stock: bool = True
    supplier_contact: str = ""


class CoFounderEconomics(BaseModel):
    """Dual unit economics (UA COD vs Cross-border) and composite opportunity score."""

    ua_cod_net_profit_usd: float = 0.0
    ua_cod_roi_pct: float = 0.0
    ua_return_drag_usd: float = 0.0
    cross_border_net_profit_usd: float = 0.0
    cross_border_roi_pct: float = 0.0
    demand_velocity_score: float = Field(default=0.0, ge=0, le=100)
    competition_score: float = Field(default=0.0, ge=0, le=100)
    opportunity_score: float = Field(default=0.0, ge=0, le=100)
    recommended_model: str = "UA_COD"
    ua_wholesale_price_uah: Optional[float] = None
    ua_retail_price_uah: Optional[float] = None
    ua_net_profit_uah: Optional[float] = None


class AnalyzedProductReport(BaseModel):
    """Complete end-to-end analysis report for a single ad/product candidate."""

    ad: AdRecord
    landing: LandingPageData
    suppliers: list[SupplierOffer] = Field(default_factory=list)
    best_supplier: Optional[SupplierOffer] = None
    margin: MarginResult
    marketplace_signals: list[MarketplaceSignal] = Field(default_factory=list)
    tg_offers: list[TelegramDropOffer] = Field(default_factory=list)
    economics: Optional[CoFounderEconomics] = None
    source_diagnostics: dict[str, str] = Field(default_factory=dict)
    category: str = ""
    competitor_advertisers: int = Field(default=1, ge=0)
    total_matching_ads: int = Field(default=1, ge=0)
    cumulative_active_days: int = Field(default=0, ge=0)
    est_spend_usd: float = Field(default=0.0, ge=0)
    spend_basis: str = ""
    competitor_ads: list[dict[str, Any]] = Field(default_factory=list)
    wholesale_match_status: str = ""
    wholesale_match_note: str = ""
    analyzed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def make_product_placeholder_svg(label: str) -> str:
    """Return a clean inline SVG data URI for offline demo products."""
    import urllib.parse

    short = (label or "Product")[:22]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="240" viewBox="0 0 320 240">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0%" stop-color="#0f172a"/><stop offset="100%" stop-color="#1e293b"/>'
        '</linearGradient></defs>'
        '<rect width="320" height="240" fill="url(#g)"/>'
        '<rect x="20" y="20" width="280" height="200" rx="14" fill="none" stroke="#38bdf8" stroke-opacity="0.35"/>'
        f'<text x="160" y="118" fill="#e2e8f0" font-family="sans-serif" font-size="16" font-weight="bold" text-anchor="middle">{short}</text>'
        '<text x="160" y="144" fill="#38bdf8" font-family="monospace" font-size="11" text-anchor="middle">DEMO ITEM</text>'
        '</svg>'
    )
    return "data:image/svg+xml;utf8," + urllib.parse.quote(svg)


def get_httpx_proxy(settings: Settings) -> Optional[str]:
    """Return configured HTTP/SOCKS proxy URL or None."""
    proxy = settings.PROXY_URL.strip()
    return proxy or None


def get_settings() -> Settings:
    """Return a fresh Settings instance."""
    return Settings()
