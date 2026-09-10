from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class ProductCreate(BaseModel):
    name: str = Field(min_length=2, max_length=240)
    brand: str | None = None
    model: str | None = None
    gtin: str | None = None
    image_url: str | None = None


class ProductOut(ProductCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime


class SearchResult(BaseModel):
    result_key: str
    source: str
    source_name: str
    local_product_id: int | None = None
    external_product_id: str | None = None
    name: str
    brand: str | None = None
    model: str | None = None
    gtin: str | None = None
    image_url: str | None = None
    price: float | None = None
    original_price: float | None = None
    currency: str = "BRL"
    seller_name: str | None = None
    url: str | None = None
    shipping_free: bool | None = None
    tracked: bool = False


class ProviderStatus(BaseModel):
    slug: str
    name: str
    configured: bool
    ok: bool
    message: str | None = None


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    providers: list[ProviderStatus]


class TrackSearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=240)


class TrackExternalRequest(BaseModel):
    provider_slug: str
    external_product_id: str


class MercadoLivreCredentials(BaseModel):
    access_token: str = Field(min_length=10)
    refresh_token: str | None = None
    app_id: str | None = None
    client_secret: str | None = None


class ObservationCreate(BaseModel):
    retailer_slug: str
    retailer_name: str
    external_id: str
    offer_title: str
    seller_name: str | None = None
    url: str | None = None
    price: float = Field(gt=0)
    pix_price: float | None = Field(default=None, gt=0)
    shipping_price: float | None = Field(default=None, ge=0)
    available: bool = True
    captured_at: datetime | None = None
    source_kind: str = "observed"
    source_name: str = "manual"


class HistoryImportPoint(BaseModel):
    retailer_slug: str
    retailer_name: str
    price: float = Field(gt=0)
    captured_at: datetime
    external_id: str | None = None
    source_name: str


class HistoryPoint(BaseModel):
    date: datetime
    retailer: str
    retailer_slug: str
    price: float
    pix_price: float | None
    shipping_price: float | None
    source_kind: str
    source_name: str


class PriceSummary(BaseModel):
    current_min: float | None
    historical_min: float | None
    historical_max: float | None
    average: float | None
    observations: int
    first_observation: datetime | None
    last_observation: datetime | None


class LatestOffer(BaseModel):
    retailer: str
    retailer_slug: str
    price: float
    pix_price: float | None
    shipping_price: float | None
    seller_name: str | None
    url: str | None
    captured_at: datetime
    source_kind: str
    source_name: str
