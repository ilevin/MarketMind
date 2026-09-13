"""行情 Provider 注册表：按 (market, asset_type) 从配置选择数据源实现。

v0.03：每组 Provider 调用经 call_with_metrics 统一包装（计时、线程级超时、
成功/错误/超时分类计数）；tencent 的 httpx 超时改由配置注入。
"""

from __future__ import annotations

import logging

from app.config import AppConfig
from app.models.instrument import Instrument
from app.observability.provider_metrics import (
    ProviderMetricsRegistry,
    call_with_metrics,
)
from app.providers.base import QuoteProvider
from app.providers.quote.akshare import AkshareQuoteProvider
from app.providers.quote.tencent import TencentQuoteClient, TencentQuoteProvider

logger = logging.getLogger(__name__)

# 数据源名称 -> Provider 类（新增数据源只需在此登记）
_PROVIDERS: dict[str, type] = {
    "akshare": AkshareQuoteProvider,
    "tencent": TencentQuoteProvider,
}


class QuoteProviderRegistry:
    """按市场+资产类型分派到配置声明的 Provider。"""

    def __init__(self, config: AppConfig, metrics: ProviderMetricsRegistry | None = None):
        self._metrics = metrics if metrics is not None else ProviderMetricsRegistry()
        self._single: dict[str, QuoteProvider] = {}
        self._timeouts: dict[str, float] = {}
        quote_cfg = config.providers.quote
        for field_name in (
            "cn_stock", "cn_etf", "hk_stock", "hk_etf", "cn_index", "hk_index",
        ):
            source = getattr(quote_cfg, field_name).lower()
            provider_cls = _PROVIDERS.get(source)
            if provider_cls is None:
                raise ValueError(f"未知的行情数据源: {source}（可选: {sorted(_PROVIDERS)}）")
            if source not in self._single:
                if source not in self._timeouts:
                    self._timeouts[source] = getattr(config.providers.timeout, source)
                if provider_cls is TencentQuoteProvider:
                    # 超时由配置注入（替换硬编码 10 秒，技术方案 §27）
                    self._single[source] = TencentQuoteProvider(
                        client=TencentQuoteClient(timeout=self._timeouts[source])
                    )
                else:
                    self._single[source] = provider_cls()
        self._registry = {}  # (market, asset_type) -> provider 实例
        self._source_map = {}  # (market, asset_type) -> 数据源名（metrics 统计键）
        for (market, asset_type), field_name in {
            ("CN", "STOCK"): "cn_stock",
            ("CN", "ETF"): "cn_etf",
            ("HK", "STOCK"): "hk_stock",
            ("HK", "ETF"): "hk_etf",
            ("CN", "INDEX"): "cn_index",
            ("HK", "INDEX"): "hk_index",
        }.items():
            source = getattr(quote_cfg, field_name).lower()
            self._registry[(market, asset_type)] = self._single[source]
            self._source_map[(market, asset_type)] = source

    @property
    def metrics(self) -> ProviderMetricsRegistry:
        return self._metrics

    def provider_for(self, market: str, asset_type: str) -> QuoteProvider | None:
        return self._registry.get((market, asset_type))

    def get_quotes(self, instruments: list[Instrument]) -> dict:
        """按 (market, asset_type) 分组调用对应 Provider 并合并结果。

        每组调用经 metrics 包装：超时/报错分类计数后抛出，由本方法捕获隔离
        （保留已有缓存，下个刷新周期重试——Last Known Good 不变）。
        """
        result = {}
        groups: dict[tuple[str, str], list[Instrument]] = {}
        for inst in instruments:
            groups.setdefault((inst.market, inst.asset_type), []).append(inst)
        for key, group in groups.items():
            provider = self.provider_for(*key)
            if provider is None:
                continue
            source = self._source_map.get(key, "unknown")
            try:
                quotes = call_with_metrics(
                    self._metrics,
                    source,
                    provider.get_quotes,
                    group,
                    timeout=self._timeouts.get(source),
                )
                result.update(quotes)
            except Exception:
                # 单个 Provider 失败（含超时）不影响其他数据源，保留已有缓存
                logger.exception("行情 Provider 失败: market=%s asset_type=%s", *key)
        return result
