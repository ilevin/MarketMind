"""交易日历 Provider 的 Session 生命周期与并发写入回归测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import threading

from sqlalchemy import select

from app.config import AppConfig
from app.models import TradingCalendarDay
from app.providers.trading_calendar.provider import TushareTradingCalendarProvider


class _CalendarProvider(TushareTradingCalendarProvider):
    def __init__(self, session_factory, days):
        super().__init__(AppConfig(), session_factory)
        self.days = days
        self.fetch_barrier = None

    def _fetch_year_from_tushare(self, market, year):
        if self.fetch_barrier is not None:
            self.fetch_barrier.wait(timeout=10)
        return self.days


def test_concurrent_first_year_load_is_serialized(session_factory):
    """两个任务同时加载同一年度时，最终只提交一份日历数据。"""
    days = [(date(2026, 9, 14), True), (date(2026, 9, 15), True)]
    provider = _CalendarProvider(session_factory, days)
    provider.fetch_barrier = threading.Barrier(2)

    def load():
        return provider.is_trading_day("CN", date(2026, 9, 15))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: load(), range(2)))

    assert results == [True, True]
    with session_factory() as session:
        rows = session.scalars(
            select(TradingCalendarDay).where(TradingCalendarDay.market == "CN")
        ).all()
        assert len(rows) == len(days)


def test_calendar_write_failure_rolls_back_and_next_read_recovers(
    session_factory, monkeypatch
):
    """年度缓存失败后事务回滚，后续调用可以重新加载并正常查询。"""
    days = [(date(2026, 9, 15), True)]
    provider = _CalendarProvider(session_factory, days)

    from app.repositories.trading_calendar import TradingCalendarRepository

    original_method = TradingCalendarRepository.save_days
    failed = True

    def fail_once(repo, market, rows):
        nonlocal failed
        if failed:
            failed = False
            repo.session.add(
                TradingCalendarDay(
                    market=market, trade_date=rows[0][0], is_open=rows[0][1]
                )
            )
            raise RuntimeError("injected calendar write failure")
        return original_method(repo, market, rows)

    monkeypatch.setattr(TradingCalendarRepository, "save_days", fail_once)

    try:
        provider.is_trading_day("CN", date(2026, 9, 15))
    except RuntimeError as exc:
        assert str(exc) == "injected calendar write failure"
    else:
        raise AssertionError("首次日历写入应失败")

    assert provider.is_trading_day("CN", date(2026, 9, 15)) is True
    with session_factory() as session:
        assert session.scalar(
            select(TradingCalendarDay.is_open).where(
                TradingCalendarDay.market == "CN",
                TradingCalendarDay.trade_date == date(2026, 9, 15),
            )
        ) is True
