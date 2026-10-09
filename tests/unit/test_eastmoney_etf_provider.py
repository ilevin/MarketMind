"""东方财富 ETF Provider 与 Registry 路由离线单测（etf-data-module 4.6）。

覆盖：
- EastmoneyEtfHistoryProvider：universe 解析、etf_daily 区间拉取、脏值清洗、
  行数上限截断、异常分类、gate 节流；
- HistoryProviderRegistry：按数据集选源（etf_daily→eastmoney、
  etf_adj_factor→tushare、daily→tushare）、metrics key、未知源 ValueError、
  单例复用；
- Tushare fund_adj（etf_adj_factor）：参数透传、AdjFactor 记录、schema 校验。

全部离线：akshare 经 monkeypatch 注入 fake 模块；Tushare 用 FakeTushareClient。
"""

from __future__ import annotations

import sys
import time
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from app.config import (
    AppConfig,
    HistoryProviderConfig,
    ProvidersConfig,
    TushareConfig,
)
from app.models.instrument import Instrument
from app.observability.provider_metrics import ProviderMetricsRegistry
from app.providers.eastmoney_common import (
    ETF_HIST_ROW_CAP,
    EastmoneyProviderError,
    EastmoneyRequestGate,
    EastmoneySchemaMismatchError,
    EastmoneyTimeoutError,
    classify_eastmoney_exception,
)
from app.providers.history import HistoryProviderRegistry
from app.providers.history.eastmoney_etf import (
    SOURCE as EASTMONEY_SOURCE,
    EastmoneyEtfHistoryProvider,
)
from app.providers.history.tushare import (
    ADJ_FACTOR_FIELDS,
    TushareHistorySchemaError,
    TushareHistoricalMarketDataProvider,
)
from app.providers.tushare_common import TushareRequestGate, TushareTransport

# ---- 共用夹具 ----

ETF_GEM = Instrument(
    instrument_id="CN:ETF:159915",
    symbol="159915",
    name="创业板ETF",
    market="CN",
    asset_type="ETF",
    currency="CNY",
    exchange="SZSE",
)

ETF_300 = Instrument(
    instrument_id="CN:ETF:510300",
    symbol="510300",
    name="沪深300ETF",
    market="CN",
    asset_type="ETF",
    currency="CNY",
    exchange="SSE",
)

RANGE_START = date(2024, 1, 2)
RANGE_END = date(2024, 1, 5)


def _eastmoney_config() -> AppConfig:
    """默认 AppConfig（eastmoney ETF + tushare 主源）。"""
    return AppConfig(
        tushare=TushareConfig(token="fake-token"),
        providers=ProvidersConfig(
            history=HistoryProviderConfig(
                market_data="tushare",
                etf_daily="eastmoney",
                etf_adj_factor="tushare",
            )
        ),
    )


# ============================================================
#  1. EastmoneyEtfHistoryProvider —— akshare 延迟 import + fake 注入
# ============================================================


class FakeAkshare:
    """fake akshare 模块：记录调用、按 endpoint 返回预置 DataFrame。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._responses: dict[str, object] = {}
        self._raises: dict[str, BaseException] = {}

    def add(self, func_name: str, response) -> None:
        self._responses[func_name] = response

    def add_raises(self, func_name: str, exc: BaseException) -> None:
        self._raises[func_name] = exc

    def _record(self, name: str, **kwargs):
        self.calls.append((name, kwargs))
        if name in self._raises:
            raise self._raises[name]
        resp = self._responses.get(name)
        if callable(resp):
            return resp(**kwargs)
        return resp

    def __getattr__(self, name: str):
        def call(**kwargs):
            return self._record(name, **kwargs)
        return call

    def calls_for(self, func_name: str) -> list[dict]:
        return [params for n, params in self.calls if n == func_name]


def _inject_fake_akshare(monkeypatch, fake: FakeAkshare) -> None:
    """通过 monkeypatch 把 fake akshare 注入 sys.modules。"""
    monkeypatch.setitem(sys.modules, "akshare", fake)


def _etf_provider(fake: FakeAkshare, gate_interval: float = 0.0) -> EastmoneyEtfHistoryProvider:
    """构造 EastmoneyEtfHistoryProvider，使用自定义 gate（绕开共享 gate）。"""
    cfg = _eastmoney_config()
    gate = EastmoneyRequestGate(min_interval=gate_interval)
    return EastmoneyEtfHistoryProvider(cfg, gate=gate)


# ---- get_etf_universe ----


def test_get_etf_universe_parses_sz_and_sh_prefixes(monkeypatch):
    """sz 前缀 → SZSE/159915.SZ/CN:ETF:159915；sh 前缀 → SSE/510300.SH/CN:ETF:510300。"""
    fake = FakeAkshare()
    fake.add(
        "fund_etf_category_sina",
        pd.DataFrame(
            [
                {"代码": "sz159915", "名称": "创业板ETF"},
                {"代码": "sh510300", "名称": "沪深300ETF"},
            ]
        ),
    )
    _inject_fake_akshare(monkeypatch, fake)
    batch = _etf_provider(fake).get_etf_universe()

    assert batch.source == EASTMONEY_SOURCE
    assert batch.raw_row_count == 2
    assert batch.truncation_risk is False
    assert len(batch.records) == 2

    gem = next(r for r in batch.records if r.symbol == "159915")
    assert gem.ts_code == "159915.SZ"
    assert gem.instrument_id == "CN:ETF:159915"
    assert gem.exchange == "SZSE"
    assert gem.name == "创业板ETF"
    assert gem.list_date is None

    hs300 = next(r for r in batch.records if r.symbol == "510300")
    assert hs300.ts_code == "510300.SH"
    assert hs300.instrument_id == "CN:ETF:510300"
    assert hs300.exchange == "SSE"
    assert hs300.name == "沪深300ETF"


def test_get_etf_universe_empty_dataframe_returns_zero_records(monkeypatch):
    """空 DataFrame → 0 行 records、raw_row_count=0。"""
    fake = FakeAkshare()
    fake.add("fund_etf_category_sina", pd.DataFrame(columns=["代码", "名称"]))
    _inject_fake_akshare(monkeypatch, fake)
    batch = _etf_provider(fake).get_etf_universe()
    assert batch.records == []
    assert batch.raw_row_count == 0


def test_get_etf_universe_missing_columns_raises_schema_mismatch(monkeypatch):
    """缺失必需列 → EastmoneySchemaMismatchError（error_code SCHEMA_MISMATCH）。"""
    fake = FakeAkshare()
    fake.add(
        "fund_etf_category_sina",
        pd.DataFrame([{"代码": "sz159915"}]),  # 缺"名称"列
    )
    _inject_fake_akshare(monkeypatch, fake)
    with pytest.raises(EastmoneySchemaMismatchError) as exc_info:
        _etf_provider(fake).get_etf_universe()
    assert exc_info.value.error_code == "SCHEMA_MISMATCH"


def test_get_etf_universe_skips_malformed_rows(monkeypatch):
    """无前缀/短代码/未知前缀的行跳过，只保留有效行。"""
    fake = FakeAkshare()
    fake.add(
        "fund_etf_category_sina",
        pd.DataFrame(
            [
                {"代码": "sz159915", "名称": "创业板ETF"},  # 有效
                {"代码": "159915", "名称": "无前缀"},       # 无前缀 → 跳过
                {"代码": "sz123", "名称": "短代码"},        # 长度不足 → 跳过
                {"代码": "bj430047", "名称": "北交所"},      # 非 sz/sh → 跳过
                {"代码": "", "名称": "空代码"},             # 空 → 跳过
                {"代码": "sh510300", "名称": "沪深300ETF"}, # 有效
            ]
        ),
    )
    _inject_fake_akshare(monkeypatch, fake)
    batch = _etf_provider(fake).get_etf_universe()
    assert len(batch.records) == 2
    symbols = {r.symbol for r in batch.records}
    assert symbols == {"159915", "510300"}
    assert batch.raw_row_count == 6  # 原始 6 行


def test_get_etf_universe_passes_symbol_param(monkeypatch):
    """调用参数验证：symbol='ETF基金'。"""
    fake = FakeAkshare()
    fake.add("fund_etf_category_sina", pd.DataFrame(columns=["代码", "名称"]))
    _inject_fake_akshare(monkeypatch, fake)
    _etf_provider(fake).get_etf_universe()
    (params,) = fake.calls_for("fund_etf_category_sina")
    assert params["symbol"] == "ETF基金"


# ---- get_history_by_stock('etf_daily') ----


def _etf_daily_row(**overrides) -> dict:
    row = {
        "日期": "2024-01-02",
        "开盘": 2.500,
        "收盘": 2.550,
        "最高": 2.560,
        "最低": 2.490,
        "成交量": 125000,   # 手
        "成交额": 31250000.0,  # 元
        "换手率": 3.25,     # 百分比数值
    }
    row.update(overrides)
    return row


def test_get_history_by_stock_etf_daily_passes_params(monkeypatch):
    """区间参数透传：start_date/end_date 转 %Y%m%d、adjust=''、period='daily'。"""
    fake = FakeAkshare()
    fake.add(
        "fund_etf_hist_em",
        pd.DataFrame([_etf_daily_row()]),
    )
    _inject_fake_akshare(monkeypatch, fake)
    batch = _etf_provider(fake).get_history_by_stock(
        "etf_daily", ETF_GEM, RANGE_START, RANGE_END
    )

    (params,) = fake.calls_for("fund_etf_hist_em")
    assert params["symbol"] == "159915"
    assert params["start_date"] == "20240102"
    assert params["end_date"] == "20240105"
    assert params["period"] == "daily"
    assert params["adjust"] == ""

    assert batch.source == EASTMONEY_SOURCE
    assert batch.raw_row_count == 1
    rec = batch.records[0]
    assert rec.instrument_id == "CN:ETF:159915"
    assert rec.ts_code == "159915.SZ"
    assert rec.trade_date == date(2024, 1, 2)
    assert rec.open == 2.500
    assert rec.close == 2.550
    assert rec.volume == 125000
    assert rec.amount == 31250000.0
    assert rec.turnover_rate == 3.25


def test_get_history_by_stock_sh_etf_uses_sh_suffix(monkeypatch):
    """SSE 交易所 → .SH 后缀。"""
    fake = FakeAkshare()
    fake.add("fund_etf_hist_em", pd.DataFrame([_etf_daily_row()]))
    _inject_fake_akshare(monkeypatch, fake)
    batch = _etf_provider(fake).get_history_by_stock(
        "etf_daily", ETF_300, RANGE_START, RANGE_END
    )
    assert batch.records[0].ts_code == "510300.SH"
    (params,) = fake.calls_for("fund_etf_hist_em")
    assert params["symbol"] == "510300"


def test_get_history_by_stock_missing_columns_raises_schema_mismatch(monkeypatch):
    """列缺失 → EastmoneySchemaMismatchError（error_code SCHEMA_MISMATCH）。"""
    fake = FakeAkshare()
    df = pd.DataFrame([_etf_daily_row()]).drop(columns=["换手率"])
    fake.add("fund_etf_hist_em", df)
    _inject_fake_akshare(monkeypatch, fake)
    with pytest.raises(EastmoneySchemaMismatchError) as exc_info:
        _etf_provider(fake).get_history_by_stock(
            "etf_daily", ETF_GEM, RANGE_START, RANGE_END
        )
    assert exc_info.value.error_code == "SCHEMA_MISMATCH"


def test_get_history_by_stock_unsupported_dataset_raises_value_error(monkeypatch):
    """不支持的数据集（如 'daily'）→ ValueError。"""
    fake = FakeAkshare()
    _inject_fake_akshare(monkeypatch, fake)
    with pytest.raises(ValueError, match="不支持数据集"):
        _etf_provider(fake).get_history_by_stock(
            "daily", ETF_GEM, RANGE_START, RANGE_END
        )


def test_get_history_by_stock_empty_response(monkeypatch):
    """空结果 → 空批次，不抛异常。"""
    fake = FakeAkshare()
    fake.add("fund_etf_hist_em", pd.DataFrame())
    _inject_fake_akshare(monkeypatch, fake)
    batch = _etf_provider(fake).get_history_by_stock(
        "etf_daily", ETF_GEM, RANGE_START, RANGE_END
    )
    assert batch.records == []
    assert batch.raw_row_count == 0
    assert batch.truncation_risk is False


# ---- 脏值清洗 ----


def test_dirty_values_become_none(monkeypatch):
    """'-'/''/NaN → None（open/volume/turnover_rate 各一例）。"""
    fake = FakeAkshare()
    fake.add(
        "fund_etf_hist_em",
        pd.DataFrame(
            [
                _etf_daily_row(
                    开盘="-",          # 字符串 '-'
                    成交量="",          # 空字符串
                    换手率=float("nan"),  # NaN
                )
            ]
        ),
    )
    _inject_fake_akshare(monkeypatch, fake)
    rec = _etf_provider(fake).get_history_by_stock(
        "etf_daily", ETF_GEM, RANGE_START, RANGE_END
    ).records[0]
    assert rec.open is None
    assert rec.volume is None
    assert rec.turnover_rate is None


# ---- 行数上限截断 ----


def test_row_cap_truncation_risk(monkeypatch):
    """raw_row_count >= ETF_HIST_ROW_CAP → truncation_risk=True。"""
    fake = FakeAkshare()
    rows = [_etf_daily_row(日期=f"202001{i+1:02d}") for i in range(ETF_HIST_ROW_CAP)]
    fake.add("fund_etf_hist_em", pd.DataFrame(rows))
    _inject_fake_akshare(monkeypatch, fake)
    batch = _etf_provider(fake).get_history_by_stock(
        "etf_daily", ETF_GEM, date(2020, 1, 1), date(2020, 1, 31)
    )
    assert batch.truncation_risk is True
    assert batch.raw_row_count == ETF_HIST_ROW_CAP


def test_below_cap_no_truncation_risk(monkeypatch):
    """行数远低于上限 → truncation_risk=False。"""
    fake = FakeAkshare()
    fake.add("fund_etf_hist_em", pd.DataFrame([_etf_daily_row() for _ in range(100)]))
    _inject_fake_akshare(monkeypatch, fake)
    batch = _etf_provider(fake).get_history_by_stock(
        "etf_daily", ETF_GEM, RANGE_START, RANGE_END
    )
    assert batch.truncation_risk is False


# ---- 异常分类 ----


def test_timeout_classified_as_eastmoney_timeout(monkeypatch):
    """akshare 抛 TimeoutError → EastmoneyTimeoutError（且是 TimeoutError 子类）。"""
    fake = FakeAkshare()
    fake.add_raises("fund_etf_hist_em", TimeoutError("connection timed out"))
    _inject_fake_akshare(monkeypatch, fake)
    with pytest.raises(EastmoneyTimeoutError) as exc_info:
        _etf_provider(fake).get_history_by_stock(
            "etf_daily", ETF_GEM, RANGE_START, RANGE_END
        )
    assert exc_info.value.error_code == "EASTMONEY_TIMEOUT"
    assert isinstance(exc_info.value, TimeoutError)


def test_generic_exception_classified_as_api_error(monkeypatch):
    """其他异常 → EastmoneyProviderError（error_code EASTMONEY_API_ERROR）。"""
    fake = FakeAkshare()
    fake.add_raises("fund_etf_category_sina", RuntimeError("something broke"))
    _inject_fake_akshare(monkeypatch, fake)
    with pytest.raises(EastmoneyProviderError) as exc_info:
        _etf_provider(fake).get_etf_universe()
    assert exc_info.value.error_code == "EASTMONEY_API_ERROR"
    assert not isinstance(exc_info.value, EastmoneyTimeoutError)


def test_classify_eastmoney_exception_preserves_already_eastmoney_error():
    """已是 EastmoneyProviderError 直接返回（不重复包装）。"""
    err = EastmoneySchemaMismatchError("bad schema")
    result = classify_eastmoney_exception(err)
    assert result is err


# ---- gate 节流 ----


def test_request_gate_enforces_min_interval():
    """EastmoneyRequestGate(0.05) 两次 acquire 间隔 >= 0.05s。"""
    gate = EastmoneyRequestGate(min_interval=0.05)
    t0 = time.monotonic()
    gate.acquire()
    t1 = time.monotonic()
    gate.acquire()
    t2 = time.monotonic()
    # 第一次 acquire 无等待
    assert t1 - t0 < 0.01
    # 第二次 acquire 至少等待 min_interval
    assert t2 - t1 >= 0.049  # 留 1ms 容差


def test_gate_first_acquire_is_immediate():
    """首次 acquire 立即返回（无历史时间戳）。"""
    gate = EastmoneyRequestGate(min_interval=1.0)
    t0 = time.monotonic()
    gate.acquire()
    t1 = time.monotonic()
    assert t1 - t0 < 0.01


def test_etf_provider_uses_injected_gate(monkeypatch):
    """provider 走 gate.acquire()（注入 0.1s gate，两次调用有间隔）。"""
    fake = FakeAkshare()
    fake.add("fund_etf_hist_em", pd.DataFrame([_etf_daily_row()]))
    _inject_fake_akshare(monkeypatch, fake)
    provider = _etf_provider(fake, gate_interval=0.05)

    t0 = time.monotonic()
    provider.get_history_by_stock("etf_daily", ETF_GEM, RANGE_START, RANGE_END)
    provider.get_history_by_stock("etf_daily", ETF_GEM, RANGE_START, RANGE_END)
    t1 = time.monotonic()
    # 两次请求间至少 0.05s 间隔
    assert t1 - t0 >= 0.049
    assert len(fake.calls_for("fund_etf_hist_em")) == 2


# ============================================================
#  2. HistoryProviderRegistry —— 按数据集选源与 metrics key
# ============================================================


class FakeEastmoneyProvider:
    """fake eastmoney provider：记录调用、返回空 batch。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def get_etf_universe(self):
        from app.providers.base import EtfUniverseRecord, ProviderBatch
        self.calls.append(("get_etf_universe", (), {}))
        return ProviderBatch(records=[], source="eastmoney", raw_row_count=0)

    def get_history_by_stock(self, dataset, instrument, start_date, end_date):
        from app.providers.base import EtfDailyBar, ProviderBatch
        self.calls.append(
            ("get_history_by_stock", (dataset, instrument, start_date, end_date), {})
        )
        return ProviderBatch(records=[], source="eastmoney", raw_row_count=0)


class FakeTushareProvider:
    """fake tushare provider：记录调用、返回空 batch。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def get_history_by_stock(self, dataset, instrument, start_date, end_date):
        from app.providers.base import DailyBar, ProviderBatch
        self.calls.append(
            ("get_history_by_stock", (dataset, instrument, start_date, end_date), {})
        )
        return ProviderBatch(records=[], source="tushare", raw_row_count=0)

    # 兼容 Registry 可能访问的其他方法（market_data 源需要 daily 等方法也在）
    def get_daily(self, trade_date, instruments):
        from app.providers.base import DailyBar, ProviderBatch
        return ProviderBatch(records=[], source="tushare", raw_row_count=0)

    def get_stock_basic(self):
        from app.providers.base import ProviderBatch, StockBasicRecord
        return ProviderBatch(records=[], source="tushare", raw_row_count=0)


def _registry_with_fakes(
    fake_tushare: FakeTushareProvider,
    fake_eastmoney: FakeEastmoneyProvider,
) -> HistoryProviderRegistry:
    """构造 Registry 并注入两个 fake provider 到 _provider_cache。"""
    cfg = _eastmoney_config()
    registry = HistoryProviderRegistry(cfg, provider=fake_tushare)
    # eastmoney 源直接注入缓存（绕开真实构造）
    registry._provider_cache["eastmoney"] = fake_eastmoney
    return registry


def test_registry_etf_daily_routes_eastmoney(monkeypatch):
    """get_history_by_stock('etf_daily') → eastmoney 源、metrics key eastmoney_history_etf_daily。"""
    ft = FakeTushareProvider()
    fe = FakeEastmoneyProvider()
    registry = _registry_with_fakes(ft, fe)

    registry.get_history_by_stock("etf_daily", ETF_GEM, RANGE_START, RANGE_END)

    # eastmoney 被调用
    assert len(fe.calls) == 1
    assert fe.calls[0][0] == "get_history_by_stock"
    # tushare 未被调用
    assert len(ft.calls) == 0
    # metrics key
    assert registry.metrics.get("eastmoney_history_etf_daily").request_count == 1
    assert registry.metrics.get("eastmoney_history_etf_daily").success_count == 1


def test_registry_etf_adj_factor_routes_tushare(monkeypatch):
    """get_history_by_stock('etf_adj_factor') → tushare 源、metrics key tushare_history_etf_adj_factor。"""
    ft = FakeTushareProvider()
    fe = FakeEastmoneyProvider()
    registry = _registry_with_fakes(ft, fe)

    registry.get_history_by_stock("etf_adj_factor", ETF_GEM, RANGE_START, RANGE_END)

    # tushare 被调用
    assert len(ft.calls) == 1
    assert ft.calls[0][0] == "get_history_by_stock"
    # eastmoney 未被调用
    assert len(fe.calls) == 0
    # metrics key
    assert registry.metrics.get("tushare_history_etf_adj_factor").request_count == 1


def test_registry_daily_routes_tushare(monkeypatch):
    """get_history_by_stock('daily') → 回退 market_data(tushare) 源、metrics key tushare_history_daily。"""
    ft = FakeTushareProvider()
    fe = FakeEastmoneyProvider()
    registry = _registry_with_fakes(ft, fe)

    # 用 stock instrument（daily 是股票数据集）
    stock = Instrument(
        instrument_id="CN:STOCK:600519",
        symbol="600519",
        name="贵州茅台",
        market="CN",
        asset_type="STOCK",
        currency="CNY",
        exchange="SSE",
    )
    registry.get_history_by_stock("daily", stock, RANGE_START, RANGE_END)

    assert len(ft.calls) == 1
    assert len(fe.calls) == 0
    assert registry.metrics.get("tushare_history_daily").request_count == 1


def test_registry_get_etf_universe_routes_eastmoney(monkeypatch):
    """get_etf_universe() → eastmoney 源、metrics key eastmoney_history_etf_basic。"""
    ft = FakeTushareProvider()
    fe = FakeEastmoneyProvider()
    registry = _registry_with_fakes(ft, fe)

    registry.get_etf_universe()

    assert len(fe.calls) == 1
    assert fe.calls[0][0] == "get_etf_universe"
    assert len(ft.calls) == 0
    assert registry.metrics.get("eastmoney_history_etf_basic").request_count == 1


def test_registry_unknown_source_raises_value_error():
    """etf_daily 配置为未知源 → ValueError。"""
    cfg = AppConfig(
        tushare=TushareConfig(token="fake-token"),
        providers=ProvidersConfig(
            history=HistoryProviderConfig(
                market_data="tushare",
                etf_daily="unknown_source",
                etf_adj_factor="tushare",
            )
        ),
    )
    registry = HistoryProviderRegistry(cfg)
    with pytest.raises(ValueError, match="未知的历史数据源"):
        registry._provider_for_key("etf_daily")


def test_registry_same_source_reuses_singleton():
    """同一 registry 实例对同一源复用单例（_provider_cache 不重复构造）。"""
    ft = FakeTushareProvider()
    fe = FakeEastmoneyProvider()
    registry = _registry_with_fakes(ft, fe)

    # 两次 etf_daily 请求，应该复用同一个 eastmoney provider
    registry.get_history_by_stock("etf_daily", ETF_GEM, RANGE_START, RANGE_END)
    registry.get_history_by_stock("etf_daily", ETF_300, RANGE_START, RANGE_END)
    registry.get_etf_universe()

    # cache 中只有 tushare 和 eastmoney 两个条目
    assert set(registry._provider_cache.keys()) == {"tushare", "eastmoney"}
    # eastmoney provider 是同一个对象
    eastmoney_p = registry._provider_cache["eastmoney"]
    assert eastmoney_p is fe
    # 共 3 次调用（2 次 etf_daily + 1 次 etf_universe）
    assert len(fe.calls) == 3


# ============================================================
#  3. Tushare fund_adj（etf_adj_factor）
# ============================================================


class FakeTushareClient:
    """按 endpoint 预置响应的 fake SDK client（同 test_history_provider 模式）。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._responses: dict[str, object] = {}

    def add(self, endpoint: str, response) -> None:
        self._responses[endpoint] = response

    def __getattr__(self, endpoint: str):
        def call(**params):
            self.calls.append((endpoint, params))
            resp = self._responses.get(endpoint)
            if callable(resp):
                return resp(**params)
            return resp
        return call

    def calls_for(self, endpoint: str) -> list[dict]:
        return [params for name, params in self.calls if name == endpoint]


def _tushare_provider(client: FakeTushareClient) -> TushareHistoricalMarketDataProvider:
    cfg = AppConfig(tushare=TushareConfig(token="fake-token"))
    transport = TushareTransport(
        cfg, gate=TushareRequestGate(0), client_factory=lambda c: client
    )
    return TushareHistoricalMarketDataProvider(cfg, transport=transport)


def test_etf_adj_factor_passes_params_to_fund_adj_endpoint():
    """get_history_by_stock('etf_adj_factor') 透传 ts_code/start_date/end_date 给 fund_adj。"""
    client = FakeTushareClient()
    client.add(
        "fund_adj",
        pd.DataFrame(
            [
                {"ts_code": "159915.SZ", "trade_date": "20240102", "adj_factor": 1.05},
                {"ts_code": "159915.SZ", "trade_date": "20240103", "adj_factor": 1.06},
            ]
        ),
    )
    batch = _tushare_provider(client).get_history_by_stock(
        "etf_adj_factor", ETF_GEM, RANGE_START, RANGE_END
    )

    (params,) = client.calls_for("fund_adj")
    assert params["ts_code"] == "159915.SZ"
    assert params["start_date"] == "20240102"
    assert params["end_date"] == "20240105"
    # fields 含 ts_code/trade_date/adj_factor
    fields = params["fields"].split(",")
    assert "ts_code" in fields
    assert "trade_date" in fields
    assert "adj_factor" in fields

    assert batch.source == "tushare"
    assert batch.raw_row_count == 2
    assert len(batch.records) == 2
    rec = batch.records[0]
    assert rec.instrument_id == "CN:ETF:159915"
    assert rec.ts_code == "159915.SZ"
    assert rec.trade_date == date(2024, 1, 2)
    assert rec.adj_factor == 1.05


def test_etf_adj_factor_sh_etf():
    """上交所 ETF → fund_adj 使用 .SH 后缀。"""
    client = FakeTushareClient()
    client.add(
        "fund_adj",
        pd.DataFrame(
            [{"ts_code": "510300.SH", "trade_date": "20240102", "adj_factor": 2.0}]
        ),
    )
    batch = _tushare_provider(client).get_history_by_stock(
        "etf_adj_factor", ETF_300, RANGE_START, RANGE_END
    )
    (params,) = client.calls_for("fund_adj")
    assert params["ts_code"] == "510300.SH"
    assert batch.records[0].instrument_id == "CN:ETF:510300"


def test_etf_adj_factor_missing_required_column_raises_schema_mismatch():
    """fund_adj 缺失 adj_factor 列 → TushareHistorySchemaError（SCHEMA_MISMATCH）。"""
    client = FakeTushareClient()
    df = pd.DataFrame(
        [{"ts_code": "159915.SZ", "trade_date": "20240102"}]  # 缺 adj_factor
    )
    client.add("fund_adj", df)
    with pytest.raises(TushareHistorySchemaError) as exc_info:
        _tushare_provider(client).get_history_by_stock(
            "etf_adj_factor", ETF_GEM, RANGE_START, RANGE_END
        )
    assert exc_info.value.error_code == "SCHEMA_MISMATCH"


def test_etf_adj_factor_empty_response():
    """空结果 → 空批次。"""
    client = FakeTushareClient()
    client.add("fund_adj", pd.DataFrame())
    batch = _tushare_provider(client).get_history_by_stock(
        "etf_adj_factor", ETF_GEM, RANGE_START, RANGE_END
    )
    assert batch.records == []
    assert batch.raw_row_count == 0
