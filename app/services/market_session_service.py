"""市场状态判定服务（唯一判定点，见 PRD 8.4 / design.md D5、D5.1）。

时区规则：
    一律以北京时间（Asia/Shanghai）判断「今天是几号」和当前时分。
    禁止使用裸 datetime.now() / UTC 日期 / 服务器本地时区，
    否则容器为 UTC 时北京时间 0:00-8:00 会把日期错判为前一天。
"""

from __future__ import annotations

from datetime import datetime, time
from enum import Enum
from zoneinfo import ZoneInfo

from app.config import BUSINESS_TZ_NAME
from app.providers.base import TradingCalendarProvider

BEIJING = ZoneInfo(BUSINESS_TZ_NAME)

# 各市场正常连续交易时段（北京时间）
TRADING_SESSIONS: dict[str, list[tuple[time, time]]] = {
    "CN": [(time(9, 30), time(11, 30)), (time(13, 0), time(15, 0))],
    "HK": [(time(9, 30), time(12, 0)), (time(13, 0), time(16, 0))],
}


class MarketStatus(str, Enum):
    OPEN = "OPEN"
    LUNCH_BREAK = "LUNCH_BREAK"
    CLOSED = "CLOSED"
    HOLIDAY = "HOLIDAY"


def now_beijing() -> datetime:
    """当前北京时间（tz-aware）。全项目禁止使用裸 datetime.now()。"""
    return datetime.now(BEIJING)


class MarketSessionService:
    def __init__(self, calendar: TradingCalendarProvider):
        self.calendar = calendar

    def status(self, market: str, now: datetime | None = None) -> MarketStatus:
        market = market.upper()
        if market not in TRADING_SESSIONS:
            raise ValueError(f"不支持的市场: {market}")
        now = now or now_beijing()
        if now.tzinfo is None:
            raise ValueError("now 必须是 tz-aware datetime")

        # 关键：换算到北京时间后再取日期，与服务器/容器时区无关
        local = now.astimezone(BEIJING)
        day = local.date()

        if not self.calendar.is_trading_day(market, day):
            return MarketStatus.HOLIDAY

        t = local.time()
        sessions = TRADING_SESSIONS[market]
        for start, end in sessions:
            if start <= t <= end:
                return MarketStatus.OPEN
        # 上午收盘后、下午开盘前 -> 午间休市
        morning_end = sessions[0][1]
        afternoon_start = sessions[1][0]
        if morning_end < t < afternoon_start:
            return MarketStatus.LUNCH_BREAK
        return MarketStatus.CLOSED

    def should_refresh(self, market: str, now: datetime | None = None) -> bool:
        """仅 OPEN 状态执行常规行情刷新。"""
        return self.status(market, now) == MarketStatus.OPEN

    def all_status(self, now: datetime | None = None) -> dict[str, MarketStatus]:
        return {market: self.status(market, now) for market in TRADING_SESSIONS}
