"""交易日历缓存表：结果缓存到 DuckDB，禁止每 60 秒请求日历数据源。(market, trade_date) 复合主键。

a-share-historical-data（技术方案 §11）：新增四个可空列——
- 现有实时市场状态路径继续只写 market/trade_date/is_open，行为不变；
- 历史严格日历路径主动填充 exchange/pretrade_date/source/fetched_at，
  历史同步所用数据满足 market='CN' 且 source='tushare'（禁止 weekday 近似）。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TradingCalendarDay(Base):
    __tablename__ = "trading_calendar"

    market: Mapped[str] = mapped_column(String(8), primary_key=True)  # CN / HK
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_open: Mapped[bool] = mapped_column(Boolean)

    # --- 以下为历史严格日历扩展列（全部可空，旧路径不填即兼容） ---
    exchange: Mapped[str | None] = mapped_column(String(16))  # SSE/SZSE/BSE
    pretrade_date: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str | None] = mapped_column(String(32))  # 严格路径恒为 'tushare'
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
