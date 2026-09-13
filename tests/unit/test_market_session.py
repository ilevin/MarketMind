"""MarketSessionService 单测：A股/港股各时段 + 非交易日 + 时区规则（PRD 第 30 节）。"""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services.market_session_service import (
    BEIJING,
    MarketSessionService,
    MarketStatus,
    now_beijing,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC_TZ = UTC


class FakeCalendar:
    """永远返回交易日 / 非交易日。"""

    def __init__(self, trading_days: set | None = None):
        # None 表示全部交易日；set 为指定交易日
        self.trading_days = trading_days

    def is_trading_day(self, market: str, date) -> bool:
        if self.trading_days is None:
            return date.weekday() < 5  # 默认按周一至周五近似
        return date in self.trading_days


def at_bj(day: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+08:00")


# 2026-08-18 是周二（交易日）
TRADING_DAY = "2026-08-18"


@pytest.fixture()
def svc():
    return MarketSessionService(FakeCalendar())


# ---- A股 ----


def test_cn_morning_open(svc):
    assert svc.status("CN", at_bj(TRADING_DAY, "10:00")) == MarketStatus.OPEN


def test_cn_lunch_break(svc):
    assert svc.status("CN", at_bj(TRADING_DAY, "12:00")) == MarketStatus.LUNCH_BREAK


def test_cn_afternoon_open(svc):
    assert svc.status("CN", at_bj(TRADING_DAY, "14:00")) == MarketStatus.OPEN


def test_cn_closed_after_15(svc):
    assert svc.status("CN", at_bj(TRADING_DAY, "15:01")) == MarketStatus.CLOSED


def test_cn_closed_before_930(svc):
    assert svc.status("CN", at_bj(TRADING_DAY, "09:00")) == MarketStatus.CLOSED


def test_cn_boundary_1130_open_1131_not(svc):
    assert svc.status("CN", at_bj(TRADING_DAY, "11:30")) == MarketStatus.OPEN
    assert svc.status("CN", at_bj(TRADING_DAY, "11:31")) == MarketStatus.LUNCH_BREAK


# ---- 港股 ----


def test_hk_morning_open(svc):
    assert svc.status("HK", at_bj(TRADING_DAY, "10:00")) == MarketStatus.OPEN


def test_hk_lunch_break(svc):
    assert svc.status("HK", at_bj(TRADING_DAY, "12:30")) == MarketStatus.LUNCH_BREAK


def test_hk_afternoon_open(svc):
    assert svc.status("HK", at_bj(TRADING_DAY, "15:00")) == MarketStatus.OPEN


def test_hk_closed_after_16(svc):
    assert svc.status("HK", at_bj(TRADING_DAY, "16:01")) == MarketStatus.CLOSED


# ---- 两市场独立 ----


def test_cn_closed_hk_open_independent(svc):
    now = at_bj(TRADING_DAY, "15:30")
    assert svc.status("CN", now) == MarketStatus.CLOSED
    assert svc.status("HK", now) == MarketStatus.OPEN


# ---- 非交易日 ----


def test_holiday(svc):
    weekend = "2026-08-22"  # 周六
    assert svc.status("CN", at_bj(weekend, "10:00")) == MarketStatus.HOLIDAY
    assert svc.status("HK", at_bj(weekend, "10:00")) == MarketStatus.HOLIDAY


def test_calendar_overrides_weekday():
    # 交易日历是权威：周二但日历标记为非交易日（如节假日调休）
    tuesday_not_trading = datetime(2026, 8, 18, 10, 0, tzinfo=SHANGHAI)
    svc = MarketSessionService(FakeCalendar(trading_days=set()))
    assert svc.status("CN", tuesday_not_trading) == MarketStatus.HOLIDAY


# ---- should_refresh ----


def test_should_refresh_only_open(svc):
    assert svc.should_refresh("CN", at_bj(TRADING_DAY, "10:00")) is True
    assert svc.should_refresh("CN", at_bj(TRADING_DAY, "12:00")) is False
    assert svc.should_refresh("CN", at_bj(TRADING_DAY, "16:00")) is False
    assert svc.should_refresh("CN", at_bj("2026-08-22", "10:00")) is False


# ---- 时区规则（design.md D5.1）----


def test_utc_container_morning_open(svc):
    # UTC 时间 02:00 = 北京 10:00，UTC 日期与北京日期相同（08-18）
    now_utc = datetime(2026, 8, 18, 2, 0, tzinfo=UTC_TZ)
    assert svc.status("CN", now_utc) == MarketStatus.OPEN


def test_beijing_midnight_boundary_uses_beijing_date(svc):
    # UTC 17:00 = 北京次日 01:00：日历查询必须取北京日期（08-19），而非 UTC 日期（08-18）
    now_utc = datetime(2026, 8, 18, 17, 0, tzinfo=UTC_TZ)
    assert now_utc.astimezone(BEIJING).date().isoformat() == "2026-08-19"

    class DateCapturingCalendar(FakeCalendar):
        def __init__(self):
            super().__init__()
            self.seen_dates = []

        def is_trading_day(self, market, date):
            self.seen_dates.append(date.isoformat())
            return True

    cal = DateCapturingCalendar()
    MarketSessionService(cal).status("CN", now_utc)
    assert cal.seen_dates == ["2026-08-19"], "日历必须使用北京日期，而非 UTC/服务器日期"


def test_result_independent_of_system_timezone(svc):
    """同一时刻在 UTC 与 Asia/Shanghai 表达下判定一致。"""
    utc_now = datetime(2026, 8, 18, 2, 0, tzinfo=UTC_TZ)
    bj_now = utc_now.astimezone(SHANGHAI)
    assert svc.status("CN", utc_now) == svc.status("CN", bj_now)


def test_now_beijing_is_tz_aware():
    now = now_beijing()
    assert now.tzinfo is not None


def test_naive_now_rejected(svc):
    from datetime import datetime as dt

    with pytest.raises(ValueError):
        svc.status("CN", dt(2026, 8, 18, 10, 0))
