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


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


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


# ---- 认证（multi-user-auth） ----


class LoginResponse(BaseModel):
    username: str
    role: str


class MeResponse(BaseModel):
    user_id: int
    username: str
    role: str


class ChangePasswordResponse(BaseModel):
    success: bool


class LogoutResponse(BaseModel):
    success: bool


# ---- 用户管理（multi-user-auth，admin 专用） ----


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    role: str = Field(default="user", pattern="^(user|admin)$")


class UserPatchRequest(BaseModel):
    """修改角色 / 启用状态（可选字段，仅更新提供项）。"""

    role: str | None = Field(default=None, pattern="^(user|admin)$")
    is_active: bool | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=1, max_length=128)


class UserItem(BaseModel):
    """用户信息（不含 password_hash）。"""

    user_id: int
    username: str
    role: str
    is_active: bool
    must_change_password: bool
    created_at: str | None = None
    last_login_at: str | None = None


class UserListResponse(BaseModel):
    items: list[UserItem]


class ResetPasswordResponse(BaseModel):
    success: bool
