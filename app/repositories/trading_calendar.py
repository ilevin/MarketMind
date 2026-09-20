"""交易日历仓储。

a-share-historical-data（技术方案 §11）：实时市场状态路径继续使用
``save_days``（只写 market/trade_date/is_open，行为不变）；严格日历路径
使用 ``save_days_strict``（upsert 并填充 exchange/pretrade_date/source/
fetched_at 元数据）。旧行 source=NULL，严格判定以 source='tushare' 为准。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.trading_calendar import TradingCalendarDay


@dataclass(frozen=True)
class CalendarDayRecord:
    """严格日历单日记录（每日变化的数据；exchange/source 为批量元数据）。"""

    trade_date: date
    is_open: bool
    pretrade_date: date | None = None


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

    # ---- 严格日历（a-share-historical-data，技术方案 §11/§31.6） ----

    def has_strict_year(self, market: str, year: int) -> bool:
        """该年是否已有严格日历数据（拉取按整年一次完成，存在即全年）。"""
        stmt = select(TradingCalendarDay.market).where(
            TradingCalendarDay.market == market,
            TradingCalendarDay.source == "tushare",
            TradingCalendarDay.trade_date >= date(year, 1, 1),
            TradingCalendarDay.trade_date <= date(year, 12, 31),
        )
        return self.session.scalar(stmt) is not None

    def save_days_strict(
        self,
        market: str,
        days: list[CalendarDayRecord],
        *,
        exchange: str,
        source: str,
        fetched_at: datetime,
    ) -> None:
        """严格日历落库：upsert——已存在行（含实时路径写入的旧行）刷新
        is_open 并补齐元数据；commit 由调用方负责。"""
        for rec in days:
            row = self.session.get(TradingCalendarDay, (market, rec.trade_date))
            if row is None:
                row = TradingCalendarDay(market=market, trade_date=rec.trade_date)
                self.session.add(row)
            row.is_open = rec.is_open
            row.exchange = exchange
            row.pretrade_date = rec.pretrade_date
            row.source = source
            row.fetched_at = fetched_at

    def get_days_between(
        self, market: str, start: date, end: date
    ) -> list[TradingCalendarDay]:
        """按日期范围升序返回日历行（含元数据列）。"""
        return list(
            self.session.scalars(
                select(TradingCalendarDay)
                .where(
                    TradingCalendarDay.market == market,
                    TradingCalendarDay.trade_date >= start,
                    TradingCalendarDay.trade_date <= end,
                )
                .order_by(TradingCalendarDay.trade_date)
            )
        )
