"""刷新策略与 stale 单测（PRD 第 13、16、30 节）。"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import AppConfig, QuoteConfig
from app.models.instrument import Instrument
from app.providers.base import Quote
from app.repositories.quote import QuoteSnapshotRepository
from app.services.market_session_service import (
    BEIJING,
    MarketSessionService,
    MarketStatus,
)
from app.services.quote_cache import QuoteCache
from app.services.refresh_service import RefreshService

SHANGHAI = ZoneInfo("Asia/Shanghai")


class FakeCalendar:
    def __init__(self, trading: bool = True):
        # trading=False 表示全部非交易日；True 时按周一至周五近似（周末即休市）
        self.trading = trading

    def is_trading_day(self, market, date):
        if not self.trading:
            return False
        return date.weekday() < 5


class FakeRegistry:
    """可编程的行情 Provider 注册表。"""

    def __init__(self):
        self.quotes: dict[str, Quote] = {}
        self.calls: list = []
        self.fail_markets: set = set()

    def get_quotes(self, instruments):
        result = {}
        groups: dict[str, list] = {}
        for i in instruments:
            groups.setdefault(i.market, []).append(i)
        for market, group in groups.items():
            self.calls.append(market)
            if market in self.fail_markets:
                continue  # 与真实 QuoteProviderRegistry 一致：单市场失败不影响其他市场
            result.update(
                {i.instrument_id: self.quotes[i.instrument_id] for i in group if i.instrument_id in self.quotes}
            )
        return result


def _inst(iid, market, asset_type="STOCK"):
    symbol = iid.split(":")[-1]
    return Instrument(
        instrument_id=iid, symbol=symbol, name="X", market=market,
        asset_type=asset_type, currency="CNY" if market == "CN" else "HKD",
    )


@pytest.fixture()
def env(session_factory):
    registry = FakeRegistry()
    registry.quotes = {
        "CN:STOCK:600519": Quote("CN:STOCK:600519", 1450.12, 1.25, volume_ratio=0.86, source="akshare"),
        "HK:STOCK:00700": Quote("HK:STOCK:00700", 442.4, -0.90, source="tencent", delayed=True),
        "CN:INDEX:000001": Quote("CN:INDEX:000001", 3990.30, 0.19, source="tencent"),
    }

    # 预置自选 + 指数
    from app.repositories.instrument import InstrumentRepository
    from app.repositories.watchlist import IndexWatchlistRepository, WatchlistRepository
    from app.services.instrument_id import MARKET_CURRENCY

    with session_factory() as s:
        irepo = InstrumentRepository(s)
        for iid, at in [("CN:STOCK:600519", "STOCK"), ("HK:STOCK:00700", "STOCK"), ("CN:INDEX:000001", "INDEX")]:
            mkt, _, sym = iid.split(":", 2)
            irepo.upsert(instrument_id=iid, symbol=sym, name="X", market=mkt,
                         asset_type=at, currency=MARKET_CURRENCY[mkt])
        WatchlistRepository(s).add("CN:STOCK:600519", 10)
        WatchlistRepository(s).add("HK:STOCK:00700", 20)
        IndexWatchlistRepository(s).add("CN:INDEX:000001", 10)
        s.commit()

    config = AppConfig(quote=QuoteConfig(refresh_seconds=60, stale_seconds=180))
    session_service = MarketSessionService(FakeCalendar())
    cache = QuoteCache(stale_seconds=180)
    service = RefreshService(config, session_factory, registry, session_service, cache)
    return {
        "factory": session_factory, "registry": registry, "cache": cache,
        "service": service, "session_service": session_service,
    }


def _bj(day: str, hm: str):
    from datetime import datetime

    return datetime.fromisoformat(f"{day}T{hm}:00+08:00")


TRADING_DAY = "2026-08-18"


# ---- 刷新策略 ----


def test_open_market_refreshes(env):
    result = env["service"].tick(now=_bj(TRADING_DAY, "10:00"))
    assert result.updated == 3  # CN股票 + HK股票 + CN指数
    assert set(result.markets.values()) == {"ok"}
    assert env["cache"].get("CN:STOCK:600519") is not None


def test_lunch_break_no_refresh(env):
    result = env["service"].tick(now=_bj(TRADING_DAY, "12:30"))
    assert result.updated == 0
    assert env["registry"].calls == []  # 未调用任何 Provider


def test_after_close_no_regular_refresh(env):
    result = env["service"].tick(now=_bj(TRADING_DAY, "16:01"))
    assert result.updated == 0
    assert env["registry"].calls == []


def test_holiday_no_refresh(env):
    result = env["service"].tick(now=_bj("2026-08-22", "10:00"))  # 周六
    assert result.updated == 0
    assert env["registry"].calls == []


def test_cn_closed_hk_open_refreshes_only_hk(env):
    # 15:30：A股收盘、港股交易中 -> 只刷新港股标的
    result = env["service"].tick(now=_bj(TRADING_DAY, "15:30"))
    assert env["registry"].calls == ["HK"]
    assert result.updated == 1
    assert env["cache"].get("CN:STOCK:600519") is None  # A股未被刷新


def test_open_to_closed_triggers_closing_refresh(env):
    svc = env["service"]
    # 第一轮：上午交易 -> 刷新
    svc.tick(now=_bj(TRADING_DAY, "10:00"))
    n_calls = len(env["registry"].calls)
    # 第二轮：收盘 -> 补抓一次（不是零调用）
    result = svc.tick(now=_bj(TRADING_DAY, "15:01"))
    assert "CN" in env["registry"].calls[n_calls:]  # A股补抓
    assert result.updated >= 1
    # 第三轮：港股 16:01 收盘 -> 港股也补抓一次（A股已无调用）
    n_calls = len(env["registry"].calls)
    svc.tick(now=_bj(TRADING_DAY, "16:05"))
    assert env["registry"].calls[n_calls:] == ["HK"]  # 仅港股收盘补抓
    # 第四轮：两市场均已收盘且无新边沿 -> 不再刷新
    n_calls = len(env["registry"].calls)
    svc.tick(now=_bj(TRADING_DAY, "16:10"))
    assert len(env["registry"].calls) == n_calls


def test_provider_failure_keeps_cache(env):
    env["service"].tick(now=_bj(TRADING_DAY, "10:00"))
    cached_before = env["cache"].get("HK:STOCK:00700")
    assert cached_before is not None

    # 港股 Provider 报错：A股仍正常，港股缓存保留
    env["registry"].fail_markets.add("HK")
    result = env["service"].tick(now=_bj(TRADING_DAY, "10:02"))
    assert env["cache"].get("HK:STOCK:00700") is cached_before  # 未被清空
    assert env["cache"].get("CN:STOCK:600519").quote.price == 1450.12


def test_snapshot_saved_to_duckdb(env):
    env["service"].tick(now=_bj(TRADING_DAY, "10:00"))
    with env["factory"]() as s:
        snap = QuoteSnapshotRepository(s).latest("CN:STOCK:600519")
        assert snap is not None and float(snap.price) == 1450.12
        # source_timestamp 与 fetched_at 是不同概念，都应存在
        assert snap.fetched_at is not None


# ---- stale ----


def test_stale_after_180s_when_open(env):
    from datetime import datetime

    svc = env["service"]
    tick_time = _bj(TRADING_DAY, "10:00")
    svc.tick(now=tick_time)

    now_200s = tick_time + timedelta(seconds=200)
    assert env["cache"].is_stale("CN:STOCK:600519", MarketStatus.OPEN, now=now_200s) is True
    now_100s = tick_time + timedelta(seconds=100)
    assert env["cache"].is_stale("CN:STOCK:600519", MarketStatus.OPEN, now=now_100s) is False


def test_not_stale_when_closed_even_hours_later(env):
    svc = env["service"]
    tick_time = _bj(TRADING_DAY, "10:00")
    svc.tick(now=tick_time)

    # 晚上访问：收盘状态不因时间流逝标记 stale
    evening = _bj(TRADING_DAY, "21:00")
    assert env["cache"].is_stale("CN:STOCK:600519", MarketStatus.CLOSED, now=evening) is False
    assert env["cache"].is_stale("CN:STOCK:600519", MarketStatus.LUNCH_BREAK, now=evening) is False
    assert env["cache"].is_stale("CN:STOCK:600519", MarketStatus.HOLIDAY, now=evening) is False


# ---- 缓存回退：重启后从 DuckDB 预热 ----


def test_cache_warmup_from_duckdb(env):
    env["service"].tick(now=_bj(TRADING_DAY, "10:00"))

    # 模拟重启：新缓存从 DuckDB 快照预热
    new_cache = QuoteCache(stale_seconds=180)
    with env["factory"]() as s:
        snaps = QuoteSnapshotRepository(s).latest_many(
            ["CN:STOCK:600519", "HK:STOCK:00700", "CN:INDEX:000001"]
        )
        new_cache.warmup(snaps)
    assert new_cache.get("CN:STOCK:600519").quote.price == 1450.12
    assert new_cache.get("CN:INDEX:000001").quote.price == 3990.30


# ---- 手动刷新 ----


def test_manual_refresh_force_overrides_status(env):
    result = env["service"].refresh_all(force=True)
    assert result.updated == 3


def test_manual_refresh_respects_status_by_default(env):
    result = env["service"].refresh_all(force=False)  # 测试注入时间外，这里用真实时间
    # 无论刷新与否，都不应抛异常；市场非 OPEN 时 skipped
    assert set(result.markets.values()) <= {"ok", "skipped", "no_instruments", "partial"}
