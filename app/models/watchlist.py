"""自选列表模型：股票/ETF 自选与指数配置彼此独立。instrument_id 直接作主键（一证券一行）。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Watchlist(Base):
    """股票 / ETF 自选（仅 STOCK / ETF）。标签关联见 watchlist_tag（多对多，v0.03b）。"""

    __tablename__ = "watchlist"

    instrument_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("instrument.instrument_id", name="fk_watchlist_instrument"),
        primary_key=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IndexWatchlist(Base):
    """首页指数行情区配置（仅 INDEX）。"""

    __tablename__ = "index_watchlist"

    instrument_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("instrument.instrument_id", name="fk_index_watchlist_instrument"),
        primary_key=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
