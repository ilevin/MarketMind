"""Provider 内部标准模型与接口（见 PRD 第 8、23 节）。

第三方字段名只允许存在于具体 Provider 实现内部。

a-share-historical-data（技术方案 §31.1）：增加历史数据内部标准模型
（主档 3 + 日级事实 4）、``ProviderBatch[T]`` 与 ``HistoricalMarketDataProvider``
Protocol。Tushare DataFrame / SDK 对象不越过具体 Provider 边界；原始单位
保持 Tushare 官方口径（daily.vol 手 / daily.amount 千元 / total_mv 万元），
上游 NULL 原样保留为 None。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Generic, Protocol, TypeVar, runtime_checkable

from app.models.instrument import Instrument


@dataclass(frozen=True)
class Quote:
    """统一行情模型（股票 / ETF / 指数共用）。"""

    instrument_id: str
    price: float | None
    change_percent: float | None
    volume_ratio: float | None = None
    previous_close: float | None = None
    source: str = ""
    source_timestamp: datetime | None = None
    delayed: bool = False


@dataclass(frozen=True)
class Fundamental:
    """统一估值模型（主要 A 股股票）。"""

    instrument_id: str
    trade_date: date
    pe_ttm: float | None = None
    pb: float | None = None
    dividend_yield_ttm: float | None = None
    source: str = "tushare"


# ---- 历史数据内部标准模型（技术方案 §31.1） ----


@dataclass(frozen=True)
class StockBasicRecord:
    """Tushare stock_basic 主档记录（17 个业务字段，技术方案 §8.2）。"""

    ts_code: str
    symbol: str
    instrument_id: str
    name: str | None = None
    area: str | None = None
    industry: str | None = None
    fullname: str | None = None
    enname: str | None = None
    cnspell: str | None = None
    market: str | None = None
    exchange: str | None = None
    curr_type: str | None = None
    list_status: str | None = None
    list_date: date | None = None
    delist_date: date | None = None
    is_hs: str | None = None
    act_name: str | None = None
    act_ent_type: str | None = None


@dataclass(frozen=True)
class StockCompanyRecord:
    """Tushare stock_company 公司资料（技术方案 §9.1）。"""

    ts_code: str
    instrument_id: str
    com_name: str | None = None
    com_id: str | None = None
    exchange: str | None = None
    chairman: str | None = None
    manager: str | None = None
    secretary: str | None = None
    reg_capital: float | None = None
    setup_date: date | None = None
    province: str | None = None
    city: str | None = None
    introduction: str | None = None
    website: str | None = None
    email: str | None = None
    office: str | None = None
    employees: int | None = None
    main_business: str | None = None
    business_scope: str | None = None


@dataclass(frozen=True)
class StockNameChangeRecord:
    """Tushare namechange 历史名称事件（技术方案 §10.1）。

    event_key（SHA-256 稳定键）由持久层计算，Provider 只交付原始事件。
    """

    ts_code: str
    instrument_id: str
    name: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    ann_date: date | None = None
    change_reason: str | None = None


@dataclass(frozen=True)
class DailyBar:
    """日线行情（原始未复权；vol 单位手、amount 单位千元，技术方案 §13）。"""

    instrument_id: str
    ts_code: str
    trade_date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    pre_close: float | None = None
    change: float | None = None
    pct_chg: float | None = None
    vol: float | None = None
    amount: float | None = None
    ah_vol: float | None = None
    ah_amount: float | None = None


@dataclass(frozen=True)
class AdjFactor:
    """复权因子（必须 > 0，校验层保证，技术方案 §14）。"""

    instrument_id: str
    ts_code: str
    trade_date: date
    adj_factor: float


@dataclass(frozen=True)
class DailyBasic:
    """每日指标（NULL 保留原义：亏损 PE、无股息率等，技术方案 §15）。"""

    instrument_id: str
    ts_code: str
    trade_date: date
    close: float | None = None
    turnover_rate: float | None = None
    turnover_rate_f: float | None = None
    volume_ratio: float | None = None
    pe: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    ps: float | None = None
    ps_ttm: float | None = None
    dv_ratio: float | None = None
    dv_ttm: float | None = None
    total_share: float | None = None
    float_share: float | None = None
    free_share: float | None = None
    total_mv: float | None = None  # 万元
    circ_mv: float | None = None  # 万元
    limit_status: int | None = None


@dataclass(frozen=True)
class MoneyFlow:
    """个股资金流（*_vol 股数、*_amount 万元；net 可负，技术方案 §16）。"""

    instrument_id: str
    ts_code: str
    trade_date: date
    buy_sm_vol: int | None = None
    buy_sm_amount: float | None = None
    sell_sm_vol: int | None = None
    sell_sm_amount: float | None = None
    buy_md_vol: int | None = None
    buy_md_amount: float | None = None
    sell_md_vol: int | None = None
    sell_md_amount: float | None = None
    buy_lg_vol: int | None = None
    buy_lg_amount: float | None = None
    sell_lg_vol: int | None = None
    sell_lg_amount: float | None = None
    buy_elg_vol: int | None = None
    buy_elg_amount: float | None = None
    sell_elg_vol: int | None = None
    sell_elg_amount: float | None = None
    net_mf_vol: int | None = None
    net_mf_amount: float | None = None


T = TypeVar("T")


@dataclass(frozen=True)
class ProviderBatch(Generic[T]):
    """历史 Provider 批量返回（技术方案 §31.2）。

    records 为内部标准模型列表；raw_row_count 为上游原始行数
    （去重合并前）；truncation_risk=True 表示可能被接口行数上限截断，
    不能直接提交。不含 Tushare SDK 对象或原始 DataFrame。
    """

    records: list[T]
    source: str
    raw_row_count: int
    truncation_risk: bool = False


@runtime_checkable
class QuoteProvider(Protocol):
    def get_quotes(self, instruments: list[Instrument]) -> dict[str, Quote]: ...


@runtime_checkable
class FundamentalProvider(Protocol):
    def get_fundamentals(self, instruments: list[Instrument]) -> dict[str, Fundamental]: ...


@runtime_checkable
class TradingCalendarProvider(Protocol):
    def is_trading_day(self, market: str, date: date) -> bool: ...


@runtime_checkable
class InstrumentNameProvider(Protocol):
    """证券名称识别：添加自选时自动获取名称，无法识别则返回 None。"""

    def get_name(self, market: str, asset_type: str, symbol: str) -> str | None: ...


@runtime_checkable
class HistoricalMarketDataProvider(Protocol):
    """A股历史数据 Provider（技术方案 §31.1；交易日历除外——见现有
    TushareTradingCalendarProvider 的 strict 模式扩展）。

    日级方法传入 instruments（来自 cn_stock_basic 主档），Provider 以
    ts_code 代码部分匹配 symbol 完成映射；返回未知 ts_code 时抛
    UNKNOWN_INSTRUMENT 类异常。异常必须向 Service 传播（禁止吞掉返回
    空集合——历史连续性依赖异常可见）。``*_for_instruments`` 为截断
    fallback 用的逐证券细粒度请求（技术方案 §33.2）。
    """

    def get_stock_basic(self) -> ProviderBatch[StockBasicRecord]: ...
    def get_stock_company(self) -> ProviderBatch[StockCompanyRecord]: ...
    def get_name_changes(
        self,
        *,
        ts_code: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> ProviderBatch[StockNameChangeRecord]: ...

    def get_daily(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[DailyBar]: ...
    def get_daily_for_instruments(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[DailyBar]: ...
    def get_adj_factors(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[AdjFactor]: ...
    def get_daily_basic(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[DailyBasic]: ...
    def get_daily_basic_for_instruments(
        self,
        trade_date: date,
        instruments: list[Instrument],
        *,
        missing_ts_codes: list[str] | None = None,
    ) -> ProviderBatch[DailyBasic]:
        """截断 fallback：只补齐 ``missing_ts_codes`` 指定的证券。

        daily_basic 不支持多代码参数（逗号分隔会静默返回空，已在线验证），
        因此按 ``候选集 - 已返回代码`` 逐只补齐；``missing_ts_codes=None``
        表示按全部证券逐只查询（保留接口语义）。
        """
        ...
    def get_moneyflow(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[MoneyFlow]: ...
    def get_moneyflow_for_instruments(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[MoneyFlow]: ...
