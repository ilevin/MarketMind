"""行情快照模型：股票、ETF、指数统一使用；每只证券一行（instrument_id 主键，upsert 更新）。

source_timestamp = 行情源的数据时间；fetched_at = 服务器实际请求时间，二者不可混淆。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class QuoteSnapshot(Base):
    __tablename__ = "quote_snapshot"

    instrument_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("instrument.instrument_id", name="fk_quote_snapshot_instrument"),
        primary_key=True,
    )
    price: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    change_percent: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    volume_ratio: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    previous_close: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    source: Mapped[str] = mapped_column(String(32))  # akshare / ...
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
