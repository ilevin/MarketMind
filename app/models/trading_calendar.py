"""交易日历缓存表：结果缓存到 DuckDB，禁止每 60 秒请求日历数据源。(market, trade_date) 复合主键。"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TradingCalendarDay(Base):
    __tablename__ = "trading_calendar"

    market: Mapped[str] = mapped_column(String(8), primary_key=True)  # CN / HK
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_open: Mapped[bool] = mapped_column(Boolean)
