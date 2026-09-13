"""Provider 统一运行指标与超时包装层（v0.03 技术方案 §29-§33，design D10）。

- ProviderMetrics：单数据源计数与最近状态（进程内存态，重启清零——§31 接受）；
- ProviderMetricsRegistry：按 source 名索引的注册表（挂 app.state，供 /api/admin/status 读取）；
- call_with_metrics：统一包装——monotonic 计时、线程级超时、成功/错误/超时分类。

异常分类（§30）：TimeoutError / httpx.TimeoutException / requests.Timeout
计入 timeout_count（超时——单纯变慢）；其余异常计入 error_count（报错——
HTTP 500 / 连接失败 / 解析失败）。两类均向调用方传播，由既有容错处理
（保留 Last Known Good 行情，下个刷新周期重试）。

超时实现说明：akshare 内部 requests 不受控、无法传参超时，因此统一经
ThreadPoolExecutor 限时执行；tencent（httpx）/ tushare（SDK）另有原生
超时先生效，本层兜底。超时后被放弃的线程不 join（shutdown(wait=False)），
避免拖垮整个刷新周期。
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 视为“超时”的异常类型（其余异常一律计入 error）
_TIMEOUT_EXCS: tuple[type[BaseException], ...] = (TimeoutError, FuturesTimeoutError)

try:  # httpx 为项目直接依赖，requests 为 akshare/tushare 传递依赖；均做保护导入
    import httpx

    _TIMEOUT_EXCS = (*_TIMEOUT_EXCS, httpx.TimeoutException)
except ImportError:  # pragma: no cover
    pass

try:
    import requests

    _TIMEOUT_EXCS = (*_TIMEOUT_EXCS, requests.exceptions.Timeout)
except ImportError:  # pragma: no cover
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_ms(started: float) -> int:
    return max(int((time.monotonic() - started) * 1000), 0)


@dataclass
class ProviderMetrics:
    """单个数据源的运行指标（技术方案 §29 全部字段）。"""

    source: str
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    timeout_count: int = 0
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    last_duration_ms: int | None = None


class ProviderMetricsRegistry:
    """按 source 名索引的内存注册表。"""

    def __init__(self) -> None:
        self._metrics: dict[str, ProviderMetrics] = {}

    def get(self, source: str) -> ProviderMetrics:
        if source not in self._metrics:
            self._metrics[source] = ProviderMetrics(source=source)
        return self._metrics[source]

    def all(self) -> dict[str, ProviderMetrics]:
        return dict(self._metrics)


def _run_with_timeout(fn, args, kwargs, timeout: float | None):
    """线程级限时执行；超时抛 TimeoutError。不 join 被放弃的线程。"""
    if timeout is None:
        return fn(*args, **kwargs)
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        raise TimeoutError(f"请求超过 {timeout} 秒未完成") from None
    finally:
        executor.shutdown(wait=False)


def call_with_metrics(
    registry: ProviderMetricsRegistry,
    source: str,
    fn,
    *args,
    timeout: float | None = None,
    **kwargs,
):
    """执行 fn 并记录指标；成功返回结果，超时/错误分类计数后原样抛出。"""
    metrics = registry.get(source)
    started = time.monotonic()
    metrics.request_count += 1
    try:
        result = _run_with_timeout(fn, args, kwargs, timeout)
    except _TIMEOUT_EXCS as exc:
        metrics.timeout_count += 1
        metrics.last_error_at = _now()
        metrics.last_error = f"timeout: {exc}"
        metrics.last_duration_ms = _elapsed_ms(started)
        logger.warning(
            "Provider %s 超时: timeout=%ss duration=%dms", source, timeout, metrics.last_duration_ms
        )
        raise
    except Exception as exc:
        metrics.error_count += 1
        metrics.last_error_at = _now()
        metrics.last_error = str(exc)
        metrics.last_duration_ms = _elapsed_ms(started)
        logger.warning("Provider %s 失败: %s", source, exc)
        raise
    else:
        metrics.success_count += 1
        metrics.last_success_at = _now()
        metrics.last_duration_ms = _elapsed_ms(started)
        return result


class TimedFundamentalProvider:
    """TushareFundamentalProvider 的方法级指标包装（design D10）。

    业务 Provider 只关注数据获取与转换；监控（计时/超时/计数）由本层负责。
    """

    def __init__(self, inner, registry: ProviderMetricsRegistry, timeout: float):
        self._inner = inner
        self._registry = registry
        self._timeout = timeout

    def get_fundamentals(self, instruments, trade_date=None):
        return call_with_metrics(
            self._registry,
            "tushare",
            self._inner.get_fundamentals,
            instruments,
            trade_date,
            timeout=self._timeout,
        )
