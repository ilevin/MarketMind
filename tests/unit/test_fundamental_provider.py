"""Tushare 估值 Provider 与刷新任务单测（mock，不依赖真实 Token）。"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.config import AppConfig, TushareConfig
from app.jobs.fundamental_refresh import FundamentalRefreshJob
from app.models.fundamental import FundamentalSnapshot
from app.models.instrument import Instrument
from app.providers.fundamental.tushare import TushareFundamentalProvider, to_ts_code
from app.repositories.fundamental import FundamentalRepository
from app.repositories.instrument import InstrumentRepository
from app.repositories.watchlist import WatchlistRepository
from app.services.market_session_service import MarketSessionService

SHANGHAI = ZoneInfo("Asia/Shanghai")
MAOTAI = Instrument(
    instrument_id="CN:STOCK:600519", symbol="600519", name="贵州茅台",
    market="CN", asset_type="STOCK", currency="CNY",
)
TENCENT = Instrument(
    instrument_id="HK:STOCK:00700", symbol="00700", name="腾讯控股",
    market="HK", asset_type="STOCK", currency="HKD",
)


class FakePro:
    """mock tushare pro.daily_basic。"""

    def __init__(self, df):
        self.df = df

    def daily_basic(self, **kwargs):
        if "trade_date" in kwargs:
            return self.df
        # 按 ts_code 查询：返回该代码的行
        code = kwargs.get("ts_code")
        rows = self.df[self.df["ts_code"] == code]
        return rows if len(rows) else pd.DataFrame(columns=self.df.columns)


def _df():
    return pd.DataFrame(
        [
            {"ts_code": "600519.SH", "trade_date": "20260818", "pe_ttm": 21.31, "pb": 7.21, "dv_ttm": 3.12},
            {"ts_code": "000001.SZ", "trade_date": "20260818", "pe_ttm": "-", "pb": "", "dv_ttm": None},
        ]
    )


@pytest.fixture()
def provider_with_token(monkeypatch):
    config = AppConfig(tushare=TushareConfig(token="fake-token-for-test"))
    provider = TushareFundamentalProvider(config)
    monkeypatch.setattr(provider, "_pro", lambda: FakePro(_df()))
    return provider


def test_to_ts_code():
    assert to_ts_code("600519") == "600519.SH"
    assert to_ts_code("000001") == "000001.SZ"
    assert to_ts_code("300750") == "300750.SZ"


def test_fundamentals_parsed_and_cleaned(provider_with_token):
    result = provider_with_token.get_fundamentals(
        [MAOTAI, TENCENT], trade_date=date(2026, 8, 18)
    )
    # 港股不请求、不写估值
    assert "HK:STOCK:00700" not in result
    fund = result["CN:STOCK:600519"]
    assert fund.pe_ttm == 21.31
    assert fund.pb == 7.21
    assert fund.dividend_yield_ttm == 3.12
    assert fund.source == "tushare"
    assert fund.trade_date == date(2026, 8, 18)


def test_dirty_values_become_none(monkeypatch):
    config = AppConfig(tushare=TushareConfig(token="fake-token-for-test"))
    provider = TushareFundamentalProvider(config)
    df = pd.DataFrame(
        [{"ts_code": "000001.SZ", "trade_date": "20260818", "pe_ttm": "-", "pb": "nan", "dv_ttm": None}]
    )
    monkeypatch.setattr(provider, "_pro", lambda: FakePro(df))
    result = provider.get_fundamentals(
        [Instrument(instrument_id="CN:STOCK:000001", symbol="000001", name="平安银行",
                    market="CN", asset_type="STOCK", currency="CNY")],
        trade_date=date(2026, 8, 18),
    )
    fund = result["CN:STOCK:000001"]
    assert fund.pe_ttm is None and fund.pb is None and fund.dividend_yield_ttm is None


def test_no_token_returns_empty():
    config = AppConfig()  # 无 Token
    provider = TushareFundamentalProvider(config)
    assert provider.get_fundamentals([MAOTAI], trade_date=date(2026, 8, 18)) == {}


def test_request_failure_returns_empty(monkeypatch):
    config = AppConfig(tushare=TushareConfig(token="fake-token-for-test"))
    provider = TushareFundamentalProvider(config)

    def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(provider, "_pro", boom)
    assert provider.get_fundamentals([MAOTAI], trade_date=date(2026, 8, 18)) == {}


# ---- FundamentalRefreshJob ----


@pytest.fixture()
def job_env(monkeypatch, session_factory, user_factory):
    # multi-user-auth：自选表按 user_id 隔离，后台 job 经 SystemWatchlistRepository
    # 跨用户聚合，故先建一个真实用户作为自选数据归属
    owner = user_factory("fund_job_owner")
    with session_factory() as s:
        irepo = InstrumentRepository(s)
        irepo.upsert(instrument_id="CN:STOCK:600519", symbol="600519", name="贵州茅台",
                     market="CN", asset_type="STOCK", currency="CNY")
        irepo.upsert(instrument_id="HK:STOCK:00700", symbol="00700", name="腾讯控股",
                     market="HK", asset_type="STOCK", currency="HKD")
        wrepo = WatchlistRepository(s, owner["user_id"])
        wrepo.add("CN:STOCK:600519", 10)
        wrepo.add("HK:STOCK:00700", 20)
        s.commit()

    config = AppConfig(tushare=TushareConfig(token="fake-token-for-test"))
    provider = TushareFundamentalProvider(config)
    monkeypatch.setattr(provider, "_pro", lambda: FakePro(_df()))

    job = FundamentalRefreshJob(
        config, session_factory, provider, MarketSessionService(_AlwaysTrading())
    )
    return {"job": job, "factory": session_factory, "user_id": owner["user_id"]}


class _AlwaysTrading:
    def is_trading_day(self, market, day):
        return True


def test_job_run_once_saves_snapshots(job_env):
    job = job_env["job"]
    result = job.run_once(trade_date=date(2026, 8, 18))
    assert result["updated"] == 1  # 只有 A股股票
    with job_env["factory"]() as s:
        fund = FundamentalRepository(s).latest("CN:STOCK:600519")
        assert fund is not None
        assert float(fund.pe_ttm) == 21.31
        assert float(fund.dividend_yield_ttm) == 3.12
        # 港股无估值
        assert FundamentalRepository(s).latest("HK:STOCK:00700") is None


def test_job_skips_when_data_exists(job_env):
    job = job_env["job"]
    job.run_once(trade_date=date(2026, 8, 18))
    # 再次运行：快照按 (instrument_id, trade_date) 幂等覆盖，不重复
    job.run_once(trade_date=date(2026, 8, 18))
    with job_env["factory"]() as s:
        count = len(s.query(FundamentalSnapshot).all())
        assert count == 1


# ---- 覆盖率判定与补刷（回归：当日已有部分数据时新加自选不补抓） ----


def _add_watchlist_stock(factory, user_id, instrument_id, symbol, name):
    from app.repositories.instrument import InstrumentRepository
    from app.repositories.watchlist import WatchlistRepository

    with factory() as s:
        InstrumentRepository(s).upsert(
            instrument_id=instrument_id, symbol=symbol, name=name,
            market="CN", asset_type="STOCK", currency="CNY",
        )
        WatchlistRepository(s, user_id).add(instrument_id, 30)
        s.commit()


def _spy_provider_calls(job, monkeypatch):
    """记录 get_fundamentals 收到的 instrument_id 列表，返回记录器。"""
    calls = []
    orig = job.provider.get_fundamentals

    def spy(instruments, trade_date=None):
        calls.append(sorted(i.instrument_id for i in instruments))
        return orig(instruments, trade_date)

    monkeypatch.setattr(job.provider, "get_fundamentals", spy)
    return calls


def test_maybe_run_backfills_newly_added_stock(job_env, monkeypatch):
    """当日刷新后盘中新增自选：下一次周期检查补齐缺口（原缺陷场景）。"""
    job = job_env["job"]
    factory = job_env["factory"]
    trade_date = date(2026, 8, 18)
    monkeypatch.setattr(job, "_latest_trade_date", lambda: trade_date)

    job._maybe_run()  # 首次：仅 600519 获得当日估值
    _add_watchlist_stock(factory, job_env["user_id"], "CN:STOCK:000001", "000001", "平安银行")

    job._maybe_run()  # 覆盖率检查发现缺口并补刷
    with factory() as s:
        assert FundamentalRepository(s).latest("CN:STOCK:000001") is not None


def test_maybe_run_skips_when_fully_covered(job_env, monkeypatch):
    job = job_env["job"]
    trade_date = date(2026, 8, 18)
    monkeypatch.setattr(job, "_latest_trade_date", lambda: trade_date)
    job._maybe_run()

    calls = _spy_provider_calls(job, monkeypatch)
    job._maybe_run()
    assert calls == []  # 自选 A 股当日全覆盖，不再请求


def test_attempted_marking_and_manual_reset(job_env, monkeypatch):
    """停牌股（Tushare 当日无记录）标记后不再重试；手动刷新强制重试。"""
    job = job_env["job"]
    factory = job_env["factory"]
    trade_date = date(2026, 8, 18)
    monkeypatch.setattr(job, "_latest_trade_date", lambda: trade_date)

    _add_watchlist_stock(factory, job_env["user_id"], "CN:STOCK:300750", "300750", "宁德时代")  # df 中无该行
    calls = _spy_provider_calls(job, monkeypatch)

    job._maybe_run()
    assert calls == [["CN:STOCK:300750", "CN:STOCK:600519"]]
    calls.clear()

    job._maybe_run()  # 300750 已标记 attempted，600519 已覆盖
    assert calls == []
    calls.clear()

    result = job.run_once(trade_date)  # 手动刷新：清空标记、强制全量
    assert calls == [["CN:STOCK:300750", "CN:STOCK:600519"]]
    assert result["failed"] == 1  # 300750 仍无当日数据
    calls.clear()

    job._maybe_run()  # run_once 重新标记，周期检查仍跳过
    assert calls == []


def test_refresh_instruments_fetches_latest_per_stock(job_env):
    """按 instrument_id 获取最近一期估值（添加自选后即时调用）。"""
    job = job_env["job"]
    factory = job_env["factory"]
    written = job.refresh_instruments(
        ["CN:STOCK:600519", "HK:STOCK:00700", "CN:STOCK:999999"]
    )
    assert written == 1  # 仅 CN/STOCK 且 Tushare 有数据
    with factory() as s:
        fund = FundamentalRepository(s).latest("CN:STOCK:600519")
        assert fund is not None
        assert float(fund.pe_ttm) == 21.31


def test_refresh_instruments_no_token_skips():
    job = FundamentalRefreshJob(AppConfig(), None, None, None)
    assert job.refresh_instruments(["CN:STOCK:600519"]) == 0


def test_per_stock_query_limits_window_and_keeps_latest(monkeypatch):
    """per-stock 查询限定日期窗口；同股多行时保留最新一期（D7）。"""
    config = AppConfig(tushare=TushareConfig(token="fake-token-for-test"))
    provider = TushareFundamentalProvider(config)
    seen = []

    class RecordingPro:
        def daily_basic(self, **kwargs):
            seen.append(kwargs)
            # 两行历史（tushare 降序返回），应保留最新一期
            return pd.DataFrame(
                [
                    {"ts_code": "600519.SH", "trade_date": "20260821", "pe_ttm": 21.0, "pb": 7.0, "dv_ttm": 3.0},
                    {"ts_code": "600519.SH", "trade_date": "20260801", "pe_ttm": 19.0, "pb": 6.0, "dv_ttm": 2.0},
                ]
            )

    monkeypatch.setattr(provider, "_pro", lambda: RecordingPro())
    result = provider.get_fundamentals([MAOTAI])  # 不带 trade_date -> per-stock 模式
    fund = result["CN:STOCK:600519"]
    assert fund.trade_date == date(2026, 8, 21)
    assert fund.pe_ttm == 21.0
    assert "start_date" in seen[0] and "end_date" in seen[0]
    assert seen[0]["start_date"] < seen[0]["end_date"]


def test_latest_trade_date_uses_calendar_provider():
    """启动初期日历仓储为空时经 Provider 判定，不误判为无交易日（D8）。"""
    config = AppConfig(tushare=TushareConfig(token="fake-token-for-test"))
    calls = []

    class Calendar:
        def is_trading_day(self, market, day):
            calls.append((market, day))
            return True

    job = FundamentalRefreshJob(config, None, None, MarketSessionService(Calendar()))
    trade_date = job._latest_trade_date()
    assert trade_date is not None
    assert calls and calls[0][0] == "CN"


def test_all_null_snapshot_counts_as_missing(job_env, monkeypatch):
    """当日行指标全空（数据源延迟）视为未覆盖，继续参与周期补刷。"""
    job = job_env["job"]
    factory = job_env["factory"]
    trade_date = date(2026, 8, 18)
    monkeypatch.setattr(job, "_latest_trade_date", lambda: trade_date)

    _add_watchlist_stock(factory, job_env["user_id"], "CN:STOCK:000001", "000001", "平安银行")
    job._maybe_run()  # 000001 写入全空行（_df 中该行指标均为脏值）

    calls = _spy_provider_calls(job, monkeypatch)
    job._maybe_run()
    # 600519 已覆盖跳过；000001 全空行仍在缺失名单，等待指标回填
    assert calls == [["CN:STOCK:000001"]]
