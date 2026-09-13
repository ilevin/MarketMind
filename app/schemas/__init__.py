"""API Pydantic Schema（PRD 第 17 节）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---- 请求 ----


class WatchlistAddRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    market: str = Field(pattern="^(CN|HK)$")
    asset_type: str


class OrderItem(BaseModel):
    instrument_id: str
    sort_order: int


class OrderUpdateRequest(BaseModel):
    items: list[OrderItem]


class TagCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TagUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class WatchlistTagsRequest(BaseModel):
    """设置自选条目的全部标签（全量替换语义）：空数组即解除全部关联。"""

    tag_ids: list[int] = Field(default_factory=list)


# ---- 响应 ----


class TagBrief(BaseModel):
    """行情 / 自选响应中内嵌的标签对象。"""

    id: int
    name: str


class WatchlistItem(BaseModel):
    instrument_id: str
    symbol: str
    name: str
    market: str
    asset_type: str
    sort_order: int
    tags: list[TagBrief] = Field(default_factory=list)


class TagItem(BaseModel):
    id: int
    name: str
    usage_count: int


class TagListResponse(BaseModel):
    items: list[TagItem]


class WatchlistListResponse(BaseModel):
    items: list[WatchlistItem]


class QuoteItem(BaseModel):
    instrument_id: str
    symbol: str
    name: str
    market: str
    asset_type: str
    price: float | None = None
    change_percent: float | None = None
    volume_ratio: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    dividend_yield_ttm: float | None = None
    quote_source: str | None = None
    fundamental_source: str | None = None
    source_timestamp: str | None = None
    is_stale: bool = False
    delayed: bool = False
    tags: list[TagBrief] = Field(default_factory=list)


class QuotesResponse(BaseModel):
    market_status: dict[str, str]
    items: list[QuoteItem]


class IndexQuoteItem(BaseModel):
    instrument_id: str
    symbol: str
    name: str
    market: str
    asset_type: str
    price: float | None = None
    change_percent: float | None = None
    quote_source: str | None = None
    source_timestamp: str | None = None
    is_stale: bool = False


class IndicesResponse(BaseModel):
    items: list[IndexQuoteItem]
    market_status: dict[str, str] = Field(default_factory=dict)


class RefreshResult(BaseModel):
    success: bool
    updated: int
    failed: int
