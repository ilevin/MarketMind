"""历史行情数据源注册表（a-share-historical-data，技术方案 §31.4）。

仿 ``QuoteProviderRegistry``：按 ``config.providers.history.market_data``
选源、单源单例；全部方法经 ``call_with_metrics`` 统一包装（计时、成功/错误/
超时分类计数，**不做方法级限时**），metrics 统计键为
``{source}_history_{dataset}``（如 ``tushare_history_daily``，§31.5——复用
ProviderMetricsRegistry，非新 metrics 系统）。

HistorySyncService 只使用本注册表，不直接构造具体 Provider、不 import
tushare。异常原样传播（重试编排在 Service，Registry 不吞）。
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
    HistoricalMarketDataProvider,
    MoneyFlow,
    ProviderBatch,
    StockBasicRecord,
    StockCompanyRecord,
    StockNameChangeRecord,
)
from app.providers.history.tushare import TushareHistoricalMarketDataProvider

logger = logging.getLogger(__name__)

# 数据源名称 -> Provider 类（新增数据源只需在此登记）
_PROVIDERS: dict[str, type] = {
    "tushare": TushareHistoricalMarketDataProvider,
}

# 方法名 -> dataset（metrics 统计键后缀；fallback 方法计入同一 dataset）
_METHOD_DATASETS: dict[str, str] = {
    "get_stock_basic": "stock_basic",
    "get_stock_company": "stock_company",
    "get_name_changes": "namechange",
    "get_daily": "daily",
    "get_daily_for_instruments": "daily",
    "get_adj_factors": "adj_factor",
    "get_daily_basic": "daily_basic",
    "get_daily_basic_for_instruments": "daily_basic",
    "get_moneyflow": "moneyflow",
    "get_moneyflow_for_instruments": "moneyflow",
}


class HistoryProviderRegistry:
    """历史行情数据源注册表：单源单例 + 方法级 metrics 包装。

    自身实现 ``HistoricalMarketDataProvider`` Protocol，作为 Service 的
    稳定历史数据获取入口。
    """

    def __init__(
        self,
        config: AppConfig,
        metrics: ProviderMetricsRegistry | None = None,
        provider: HistoricalMarketDataProvider | None = None,
    ):
        self._metrics = metrics if metrics is not None else ProviderMetricsRegistry()
        source = config.providers.history.market_data.lower()
        provider_cls = _PROVIDERS.get(source)
        if provider_cls is None:
            raise ValueError(
                f"未知的历史数据源: {source}（可选: {sorted(_PROVIDERS)}）"
            )
        self._source = source
        # 单请求超时由 Provider/transport 从 config.providers.timeout 取得
        # （技术方案 §27），注册表不设方法级超时（见 _call）。
        # provider 显式注入供测试；生产路径按配置构造单例
        self._provider: HistoricalMarketDataProvider = (
            provider if provider is not None else provider_cls(config)
        )

    @property
    def metrics(self) -> ProviderMetricsRegistry:
        return self._metrics

    @property
    def source(self) -> str:
        return self._source

    @property
    def provider(self) -> HistoricalMarketDataProvider:
        return self._provider

    def _call(self, method_name: str, *args, **kwargs):
        """经 call_with_metrics 调用底层 Provider 方法（§31.5 metrics key）。

        **不设方法级 wall-clock timeout**：本注册表的方法多含多个远端请求
        （stock_basic 15 个分片、按证券逐只补齐上千次），固定 15s 上限会在
        正常节流下必然误判超时并留下仍在发请求的线程。这里只记录整个方法
        的 success/error/duration；真实超时由 transport 对每个请求限时后
        抛 ``TushareTimeoutError``（``TimeoutError`` 子类），照常计入
        timeout_count。
        """
        dataset = _METHOD_DATASETS.get(method_name, "unknown")
        return call_with_metrics(
            self._metrics,
            f"{self._source}_history_{dataset}",
            getattr(self._provider, method_name),
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
