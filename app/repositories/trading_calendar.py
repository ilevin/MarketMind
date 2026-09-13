"""交易日历仓储。"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.trading_calendar import TradingCalendarDay


class TradingCalendarRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, market: str, day: date) -> bool | None:
        row = self.session.scalar(
            select(TradingCalendarDay).where(
                TradingCalendarDay.market == market,
                TradingCalendarDay.trade_date == day,
            )
        )
        return row.is_open if row is not None else None

    def has_year(self, market: str, year: int) -> bool:
        stmt = select(TradingCalendarDay.market).where(
            TradingCalendarDay.market == market,
            TradingCalendarDay.trade_date >= date(year, 1, 1),
            TradingCalendarDay.trade_date <= date(year, 12, 31),
        )
        return self.session.scalar(stmt) is not None

    def save_days(self, market: str, days: list[tuple[date, bool]]) -> None:
        """逐日落库（幂等：复合主键 (market, trade_date)，已存在跳过）；commit 由调用方负责。"""
        for day, is_open in days:
            if self.get(market, day) is None:
                self.session.add(TradingCalendarDay(market=market, trade_date=day, is_open=is_open))
