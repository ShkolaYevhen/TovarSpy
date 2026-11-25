from datetime import datetime, timezone
from typing import Optional
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
    RAPIDAPI_1688_KEY: Optional[str] = None
    RAPIDAPI_1688_HOST: str = "1688-datahub.p.rapidapi.com"
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None

    # Ad filtering thresholds
    TARGET_COUNTRIES: list[str] = Field(
        default_factory=lambda: ["UA", "PL", "RO", "CZ", "DE", "US"]
    )
    MIN_DURATION_DAYS: int = 7
    MIN_ACTIVE_AD_SETS: int = 5

    # Margin & profitability constants
    SHIPPING_COST_EST_USD: float = 2.50
    EST_CPA_USD: float = 6.00
    WINNER_NET_MARGIN_USD: float = 10.00
    FX_RATES_TO_USD: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_FX_RATES_TO_USD)
    )

    # Output configuration
    EXPORT_DIR: str = "exports"


class AdRecord(BaseModel):
    """Normalized active ad record extracted from an ad spy source."""

    ad_id: str
    page_name: str
    landing_url: str
    days_running: int = Field(ge=0)
    ad_creative_image_url: Optional[str] = None
    active_ad_sets: int = Field(default=1, ge=1)
    countries: list[str] = Field(default_factory=list)
    source: str = "meta"


class LandingPageData(BaseModel):
    """Extracted product metadata and platform fingerprint from a landing page."""

    url: str
    final_url: Optional[str] = None
    title: Optional[str] = None
    price: Optional[float] = Field(default=None, ge=0)
    currency: Optional[str] = None
    image_url: Optional[str] = None
    platform: str = "Custom PHP"


class SupplierOffer(BaseModel):
    """Matched 1688.com wholesale supplier offer."""

    supplier_name: str
    product_title: str
    product_url: str
    wholesale_price_rmb: float = Field(ge=0)
    wholesale_price_usd: float = Field(ge=0)
    rating: Optional[float] = Field(default=None, ge=0, le=5.0)
    years_on_1688: Optional[int] = Field(default=None, ge=0)
    moq: int = Field(default=1, ge=1)
    image_url: Optional[str] = None


class MarginResult(BaseModel):
    """Profitability calculation output for a product offer."""

    retail_price_usd: float = Field(ge=0)
    china_wholesale_price_usd: float = Field(ge=0)
    shipping_cost_est_usd: float = 2.50
    est_cpa_usd: float = 6.00
    net_margin_usd: float
    profitability_index: float = 0.0
    is_winner: bool = False


class AnalyzedProductReport(BaseModel):
    """Complete end-to-end analysis report for a single ad/product candidate."""

    ad: AdRecord
    landing: LandingPageData
    suppliers: list[SupplierOffer] = Field(default_factory=list)
    best_supplier: Optional[SupplierOffer] = None
    margin: MarginResult
    analyzed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def get_settings() -> Settings:
    """Return a fresh Settings instance."""
    return Settings()
