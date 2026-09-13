"""估值快照模型：最近一次估值（主要 A 股股票；指数与 ETF 不写入）。(instrument_id, trade_date) 复合主键。"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FundamentalSnapshot(Base):
    __tablename__ = "fundamental_snapshot"

    instrument_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("instrument.instrument_id", name="fk_fundamental_snapshot_instrument"),
        primary_key=True,
    )
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    pe_ttm: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    pb: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    dividend_yield_ttm: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)  # tushare
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
