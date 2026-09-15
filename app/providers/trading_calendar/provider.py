"""交易日历 Provider：Tushare trade_cal + DuckDB 缓存（按年批量）。

时区规则（design.md D5.1）：
    日历查询所用日期由调用方（MarketSessionService）以北京时间推导，
    本 Provider 只接收 date，不做时区换算；Tushare 日历日期即市场本地自然日。

数据源降级：
    - CN：Tushare trade_cal（SSE）
    - HK：尝试 Tushare trade_cal（HKEX）；不可用则回退「周一至周五」近似，
      并记录警告（节假日会误判为交易日，只影响刷新尝试，不影响数据正确性）
    - 无 Token / 请求失败：回退近似规则，不缓存近似结果，待数据源可用后修正
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.config import AppConfig
from app.db import write_coordinator
from app.repositories.trading_calendar import TradingCalendarRepository

logger = logging.getLogger(__name__)

# Tushare 交易所代码
_EXCHANGES = {"CN": "SSE", "HK": "HKEX"}


def _weekday_approx(day: date) -> bool:
    """近似规则：周一至周五视为交易日（无数据源时的回退，不缓存）。"""
    return day.weekday() < 5


class TushareTradingCalendarProvider:
    def __init__(self, config: AppConfig, session_factory: Callable[[], Session]):
        self.config = config
        self.session_factory = session_factory
        self._warned_fallback = False

    def is_trading_day(self, market: str, day: date) -> bool:
        market = market.upper()
        with self.session_factory() as session:
            cached = TradingCalendarRepository(session).get(market, day)
        if cached is not None:
            return cached

        self._load_year(market, day.year)

        with self.session_factory() as session:
            cached = TradingCalendarRepository(session).get(market, day)
        if cached is not None:
            return cached

        # 数据源不可用：近似规则（不写库，避免把不可靠数据固化）
        if not self._warned_fallback:
            logger.warning(
                "交易日历数据源不可用（market=%s），临时使用周一至周五近似规则", market
            )
            self._warned_fallback = True
        return _weekday_approx(day)

    def _load_year(self, market: str, year: int) -> None:
        with self.session_factory() as session:
            if TradingCalendarRepository(session).has_year(market, year):
                return

        # 网络请求在写锁外；落库 + 提交持锁串行化（design D6）。
        days = self._fetch_year_from_tushare(market, year)
        if days:
            with write_coordinator.write():
                with self.session_factory() as session:
                    repo = TradingCalendarRepository(session)
                    # 其他任务可能已在网络请求期间完成同一年份的缓存。
                    if repo.has_year(market, year):
                        return
                    try:
                        repo.save_days(market, days)
                        session.commit()
                    except Exception:
                        session.rollback()
                        raise
            logger.info("已缓存 %s 年 %s 交易日历（%d 天）", year, market, len(days))

    def _fetch_year_from_tushare(self, market: str, year: int) -> list[tuple[date, bool]] | None:
        if not self.config.has_tushare_token:
            return None
        try:
            import tushare as ts

            pro = ts.pro_api(self.config.tushare.token)
            df = pro.trade_cal(
                exchange=_EXCHANGES.get(market, "SSE"),
                start_date=f"{year}0101",
                end_date=f"{year}1231",
            )
        except Exception as exc:
            logger.warning("Tushare 交易日历获取失败（market=%s, %s）: %s", market, year, exc)
            return None

        if df is None or len(df) == 0:
            # 交易所不支持（如 HKEX）等导致空返回：不缓存近似数据
            logger.warning("Tushare 交易日历返回为空（market=%s, %s），不缓存", market, year)
            return None

        calibrate = {
            date(int(str(r.cal_date)[:4]), int(str(r.cal_date)[4:6]), int(str(r.cal_date)[6:8])): bool(r.is_open)
            for r in df.itertuples(index=False)
        }
        days: list[tuple[date, bool]] = []
        start = date(year, 1, 1)
        for i in range(366):
            day = start + timedelta(days=i)
            if day.year != year:
                break
            days.append((day, calibrate.get(day, day.weekday() < 5)))
        return days
