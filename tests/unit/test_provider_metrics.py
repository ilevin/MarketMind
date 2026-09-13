"""ProviderMetrics 包装层单元测试（v0.03 技术方案 §36.5 + design D10）。"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from app.config import AppConfig, ProvidersConfig, TimeoutConfig
from app.observability.provider_metrics import (
    ProviderMetricsRegistry,
    call_with_metrics,
)
from app.providers.quote import QuoteProviderRegistry
from app.services.quote_cache import QuoteCache


def _make_instrument(iid="CN:STOCK:600519"):
    from app.models.instrument import Instrument

    market, asset_type, symbol = iid.split(":")
    return Instrument(
        instrument_id=iid, symbol=symbol, name="测试", market=market,
        asset_type=asset_type, currency="CNY",
    )


def _make_quote(iid="CN:STOCK:600519", price=100.0):
    from app.providers.base import Quote

    return Quote(instrument_id=iid, price=price, change_percent=1.25, source="tencent")


# ---- call_with_metrics 四场景 ----


def test_success_counts_and_duration():
    registry = ProviderMetricsRegistry()
    result = call_with_metrics(registry, "tencent", lambda: "ok")
    assert result == "ok"
    m = registry.get("tencent")
    assert m.request_count == 1
    assert m.success_count == 1
    assert m.error_count == 0
    assert m.timeout_count == 0
    assert m.last_duration_ms is not None and m.last_duration_ms >= 0
    assert m.last_success_at is not None
    assert m.last_error is None


def test_connection_error_counts_as_error_and_propagates():
    registry = ProviderMetricsRegistry()

    def boom():
        raise ConnectionError("network unreachable")

    with pytest.raises(ConnectionError):
        call_with_metrics(registry, "tencent", boom)
    m = registry.get("tencent")
    assert m.error_count == 1
    assert m.timeout_count == 0
    assert "network unreachable" in m.last_error
    assert m.last_success_at is None  # 从未成功


def test_data_error_counts_as_error():
    registry = ProviderMetricsRegistry()

    def bad_data():
        raise ValueError("bad payload")

    with pytest.raises(ValueError):
        call_with_metrics(registry, "akshare", bad_data)
    assert registry.get("akshare").error_count == 1


def test_timeout_counts_as_timeout_and_does_not_block():
    registry = ProviderMetricsRegistry()

    def slow():
        time.sleep(0.5)
        return "late"

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        call_with_metrics(registry, "akshare", slow, timeout=0.05)
    elapsed = time.monotonic() - started

    # 超时即返回，不等待慢调用完成
    assert elapsed < 0.4
    m = registry.get("akshare")
    assert m.timeout_count == 1
    assert m.error_count == 0  # 超时与报错分开统计（§30）
    assert m.last_error is not None
    assert m.last_success_at is None


def test_native_timeout_exception_classified_as_timeout():
    """provider 内部原生超时（httpx.TimeoutException）也归 timeout 类。"""
    import httpx

    registry = ProviderMetricsRegistry()

    def native_timeout():
        raise httpx.ReadTimeout("read timed out")

    with pytest.raises(httpx.ReadTimeout):
        call_with_metrics(registry, "tencent", native_timeout, timeout=1)
    m = registry.get("tencent")
    assert m.timeout_count == 1
    assert m.error_count == 0


def test_last_success_at_updates_across_calls():
    registry = ProviderMetricsRegistry()
    call_with_metrics(registry, "tencent", lambda: 1)
    first = registry.get("tencent").last_success_at
    time.sleep(0.01)
    call_with_metrics(registry, "tencent", lambda: 2)
    assert registry.get("tencent").last_success_at >= first


# ---- Registry 层：分组包装 + 超时不拖垮刷新周期 + 缓存保留 ----


class _SlowProvider:
    def get_quotes(self, instruments):
        time.sleep(1.0)
        return {}


class _QuoteProvider:
    def get_quotes(self, instruments):
        return {inst.instrument_id: _make_quote(inst.instrument_id) for inst in instruments}


def _tiny_timeout_config() -> AppConfig:
    return AppConfig(
        providers=ProvidersConfig(timeout=TimeoutConfig(tencent=0.05, akshare=0.05))
    )


def test_registry_wraps_provider_with_metrics():
    config = AppConfig()
    registry = QuoteProviderRegistry(config)
    registry._registry = {("CN", "STOCK"): _QuoteProvider()}
    registry._source_map = {("CN", "STOCK"): "tencent"}

    result = registry.get_quotes([_make_instrument()])
    assert "CN:STOCK:600519" in result
    m = registry.metrics.get("tencent")
    assert m.request_count == 1
    assert m.success_count == 1


def test_registry_timeout_swallowed_and_quick():
    """Registry 吞掉超时（隔离），刷新周期不长期卡住，timeout 计数 +1。"""
    config = _tiny_timeout_config()
    registry = QuoteProviderRegistry(config)
    registry._registry = {("CN", "STOCK"): _SlowProvider()}
    registry._source_map = {("CN", "STOCK"): "tencent"}

    started = time.monotonic()
    result = registry.get_quotes([_make_instrument()])
    elapsed = time.monotonic() - started

    assert result == {}  # 失败隔离：不抛出、无数据
    assert elapsed < 0.5  # 不等待 1 秒的慢调用
    assert registry.metrics.get("tencent").timeout_count == 1


def test_registry_error_swallowed_and_counted():
    class _Failing:
        def get_quotes(self, instruments):
            raise ConnectionError("simulated")

    config = AppConfig()
    registry = QuoteProviderRegistry(config)
    registry._registry = {("CN", "STOCK"): _Failing()}
    registry._source_map = {("CN", "STOCK"): "tencent"}

    assert registry.get_quotes([_make_instrument()]) == {}
    assert registry.metrics.get("tencent").error_count == 1


def test_failure_keeps_last_known_good_cache():
    """超时 / 报错两类失败后，QuoteCache 保留最后一次成功行情（技术方案 §28）。"""
    cache = QuoteCache(stale_seconds=180)
    fetched_at = datetime.now(timezone.utc)
    cache.update({"CN:STOCK:600519": _make_quote(price=1450.12)}, fetched_at=fetched_at)

    class _TimeoutProvider:
        def get_quotes(self, instruments):
            raise TimeoutError("slow")

    class _ErrorProvider:
        def get_quotes(self, instruments):
            raise ConnectionError("down")

    config = AppConfig()
    for provider_cls in (_TimeoutProvider, _ErrorProvider):
        registry = QuoteProviderRegistry(config)
        registry._registry = {("CN", "STOCK"): provider_cls()}
        registry._source_map = {("CN", "STOCK"): "tencent"}
        registry.get_quotes([_make_instrument()])  # 失败被隔离

    cached = cache.get("CN:STOCK:600519")
    assert cached is not None
    assert cached.quote.price == 1450.12  # 旧行情原样保留
