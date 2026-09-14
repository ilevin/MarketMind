"""自选列表模型：股票/ETF 自选与指数配置彼此独立，均按用户隔离。

- (user_id, instrument_id) 复合主键：同一证券可被多个用户同时关注；
- instrument 为全局共享主数据，多用户复用同一行（user-data-isolation spec）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Watchlist(Base):
    """股票 / ETF 自选（仅 STOCK / ETF）。标签关联见 watchlist_tag（多对多）。"""

    __tablename__ = "watchlist"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app_user.user_id", name="fk_watchlist_user"),
        primary_key=True,
    )
    instrument_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("instrument.instrument_id", name="fk_watchlist_instrument"),
        primary_key=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IndexWatchlist(Base):
    """首页指数行情区配置（仅 INDEX），每个用户可拥有不同配置。"""

    __tablename__ = "index_watchlist"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app_user.user_id", name="fk_index_watchlist_user"),
        primary_key=True,
    )
    instrument_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("instrument.instrument_id", name="fk_index_watchlist_instrument"),
        primary_key=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
