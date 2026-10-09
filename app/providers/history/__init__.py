"""历史行情数据源注册表（a-share-historical-data，技术方案 §31.4；
etf-data-module 4.5 按数据集选源改造）。

仿 ``QuoteProviderRegistry``：按 ``config.providers.history`` 选源键
选源、每源单例；全部方法经 ``call_with_metrics`` 统一包装（计时、成功/
错误/超时分类计数，**不做方法级限时**），metrics 统计键为
``{source}_history_{dataset}``（如 ``tushare_history_daily``、
``eastmoney_history_etf_daily``，§31.5——复用 ProviderMetricsRegistry，
非新 metrics 系统）。

选源规则（etf-data-module design D5）：
- 股票数据集（stock_basic/stock_company/namechange/daily/adj_factor/
  daily_basic/moneyflow 等）→ ``market_data`` 键，语义不变；
- ``etf_daily`` → ``providers.history.etf_daily`` 键（默认 eastmoney）；
- ``etf_adj_factor`` → ``providers.history.etf_adj_factor`` 键（默认 tushare）；
- ``etf_basic``（universe 刷新）复用 ``etf_daily`` 键指定的源。

HistorySyncService 只使用本注册表，不直接构造具体 Provider、不 import
tushare/akshare。异常原样传播（重试编排在 Service，Registry 不吞）。
交易日历是例外（§31.6）：经现有 ``TushareTradingCalendarProvider`` 的
strict 模式，不经过本注册表。
"""

from __future__ import annotations

import logging
from datetime import date

from app.config import AppConfig
from app.models.instrument import Instrument
from app.observability.provider_metrics import (
    ProviderMetricsRegistry,
    call_with_metrics,
)
from app.providers.base import (
    AdjFactor,
    DailyBar,
    DailyBasic,
    EtfUniverseRecord,
    HistoricalMarketDataProvider,
    MoneyFlow,
    ProviderBatch,
    StockBasicRecord,
    StockCompanyRecord,
    StockNameChangeRecord,
)
from app.providers.history.eastmoney_etf import EastmoneyEtfHistoryProvider
from app.providers.history.tushare import TushareHistoricalMarketDataProvider

logger = logging.getLogger(__name__)

# 数据源名称 -> Provider 类（新增数据源只需在此登记）
_PROVIDERS: dict[str, type] = {
    "tushare": TushareHistoricalMarketDataProvider,
    "eastmoney": EastmoneyEtfHistoryProvider,
}

# 数据集 -> 选源配置键（config.providers.history.<key>）。
# 未列出的数据集一律回退 market_data（股票数据集语义不变）；
# etf_basic 复用 etf_daily 键（universe 获取，见 get_etf_universe）。
_DATASET_SOURCE_KEYS: dict[str, str] = {
    "stock_basic": "market_data",
    "stock_company": "market_data",
    "namechange": "market_data",
    "daily": "market_data",
    "adj_factor": "market_data",
    "daily_basic": "market_data",
    "moneyflow": "market_data",
    "etf_basic": "etf_daily",
    "etf_daily": "etf_daily",
    "etf_adj_factor": "etf_adj_factor",
}

# 方法名 -> dataset（metrics 统计键后缀；fallback 方法计入同一 dataset）
_METHOD_DATASETS: dict[str, str] = {
    "get_stock_basic": "stock_basic",
    "get_stock_company": "stock_company",
    "get_name_changes": "namechange",
    "get_etf_universe": "etf_basic",
    "get_daily": "daily",
    "get_daily_for_instruments": "daily",
    "get_adj_factors": "adj_factor",
    "get_daily_basic": "daily_basic",
    "get_daily_basic_for_instruments": "daily_basic",
    "get_moneyflow": "moneyflow",
    "get_moneyflow_for_instruments": "moneyflow",
}


class HistoryProviderRegistry:
    """历史行情数据源注册表：按数据集选源、每源单例 + 方法级 metrics 包装。

    自身实现 ``HistoricalMarketDataProvider`` Protocol，作为 Service 的
    稳定历史数据获取入口。
    """

    def __init__(
        self,
        config: AppConfig,
        metrics: ProviderMetricsRegistry | None = None,
        provider: HistoricalMarketDataProvider | None = None,
    ):
        self._config = config
        self._metrics = metrics if metrics is not None else ProviderMetricsRegistry()
        # 源名称 -> Provider 单例（懒构造）
        self._provider_cache: dict[str, HistoricalMarketDataProvider] = {}
        # 股票主源（market_data 键）：source 属性与此保持向后兼容
        self._source = config.providers.history.market_data.lower()
        if self._source not in _PROVIDERS:
            raise ValueError(
                f"未知的历史数据源: {self._source}（可选: {sorted(_PROVIDERS)}）"
            )
        # provider 显式注入供测试（注入为 market_data 源）；生产路径按配置构造单例
        if provider is not None:
            self._provider_cache[self._source] = provider

    @property
    def metrics(self) -> ProviderMetricsRegistry:
        return self._metrics

    @property
    def source(self) -> str:
        """股票主源名称（market_data 键；向后兼容属性）。"""
        return self._source

    @property
    def provider(self) -> HistoricalMarketDataProvider:
        """market_data 键指定的 Provider 单例（向后兼容属性）。"""
        return self._provider_for_key("market_data")

    # ---- 选源与单例 ----

    def _provider_for_key(self, config_key: str) -> HistoricalMarketDataProvider:
        """按选源配置键取 Provider 单例（懒构造；未知源 ValueError）。"""
        source = getattr(self._config.providers.history, config_key, None)
        if not isinstance(source, str) or not source:
            raise ValueError(f"选源配置键不存在或为空: providers.history.{config_key}")
        source = source.lower()
        if source not in self._provider_cache:
            provider_cls = _PROVIDERS.get(source)
            if provider_cls is None:
                raise ValueError(
                    f"未知的历史数据源: {source}（可选: {sorted(_PROVIDERS)}）"
                )
            self._provider_cache[source] = provider_cls(self._config)
        return self._provider_cache[source]

    def _provider_for_dataset(self, dataset: str) -> HistoricalMarketDataProvider:
        """按数据集取 Provider（未映射数据集回退 market_data 源）。"""
        config_key = _DATASET_SOURCE_KEYS.get(dataset, "market_data")
        return self._provider_for_key(config_key)

    def _source_name_for_dataset(self, dataset: str) -> str:
        """数据集对应源名称（metrics 键前缀）。"""
        config_key = _DATASET_SOURCE_KEYS.get(dataset, "market_data")
        source = getattr(self._config.providers.history, config_key)
        return source.lower()

    def _call(self, method_name: str, *args, **kwargs):
        """经 call_with_metrics 调用底层 Provider 方法（§31.5 metrics key）。

        **不设方法级 wall-clock timeout**：本注册表的方法多含多个远端请求
        （stock_basic 15 个分片、按证券逐只补齐上千次），固定 15s 上限会在
        正常节流下必然误判超时并留下仍在发请求的线程。这里只记录整个方法
        的 success/error/duration；真实超时由 transport 对每个请求限时后
        抛 ``TushareTimeoutError``（``TimeoutError`` 子类），照常计入
        timeout_count。

        历史方法（股票数据集）固定路由 market_data 源。
        """
        dataset = _METHOD_DATASETS.get(method_name, "unknown")
        return call_with_metrics(
            self._metrics,
            f"{self._source}_history_{dataset}",
            getattr(self._provider_for_key("market_data"), method_name),
            *args,
            **kwargs,
        )

    # ---- HistoricalMarketDataProvider Protocol ----

    def get_stock_basic(self) -> ProviderBatch[StockBasicRecord]:
        return self._call("get_stock_basic")

    def get_stock_company(self) -> ProviderBatch[StockCompanyRecord]:
        return self._call("get_stock_company")

    def get_name_changes(
        self,
        *,
        ts_code: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> ProviderBatch[StockNameChangeRecord]:
        return self._call(
            "get_name_changes",
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
        )

    def get_daily(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[DailyBar]:
        return self._call("get_daily", trade_date, instruments)

    def get_daily_for_instruments(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[DailyBar]:
        return self._call("get_daily_for_instruments", trade_date, instruments)

    def get_adj_factors(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[AdjFactor]:
        return self._call("get_adj_factors", trade_date, instruments)

    def get_daily_basic(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[DailyBasic]:
        return self._call("get_daily_basic", trade_date, instruments)

    def get_daily_basic_for_instruments(
        self,
        trade_date: date,
        instruments: list[Instrument],
        *,
        missing_ts_codes: list[str] | None = None,
    ) -> ProviderBatch[DailyBasic]:
        return self._call(
            "get_daily_basic_for_instruments",
            trade_date,
            instruments,
            missing_ts_codes=missing_ts_codes,
        )

    def get_moneyflow(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[MoneyFlow]:
        return self._call("get_moneyflow", trade_date, instruments)

    def get_moneyflow_for_instruments(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[MoneyFlow]:
        return self._call("get_moneyflow_for_instruments", trade_date, instruments)

    def get_etf_universe(self) -> ProviderBatch[EtfUniverseRecord]:
        """ETF universe 列表（etf_basic 数据集；复用 etf_daily 键指定的源，
        etf-data-module design D5）。"""
        return call_with_metrics(
            self._metrics,
            f"{self._source_name_for_dataset('etf_basic')}_history_etf_basic",
            self._provider_for_key("etf_daily").get_etf_universe,
        )

    def get_history_by_stock(
        self,
        dataset: str,
        instrument: Instrument,
        start_date: date,
        end_date: date,
    ) -> ProviderBatch:
        """个股区间拉取：按数据集选源，metrics 键 ``{source}_history_{dataset}``。

        与其他方法不同，这里的 metrics key 不由方法名静态决定，而是
        由 ``dataset`` 参数动态决定——使个股路径与日级路径共享同一套
        统计口径，无第二套统计实现。ETF 数据集路由到各自选源键指定的
        Provider（etf-data-module design D5）。
        """
        return call_with_metrics(
            self._metrics,
            f"{self._source_name_for_dataset(dataset)}_history_{dataset}",
            self._provider_for_dataset(dataset).get_history_by_stock,
            dataset,
            instrument,
            start_date,
            end_date,
        )
