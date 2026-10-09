"""东方财富（Eastmoney）共享 transport：请求节奏 gate + 异常体系（etf-data-module）。

Provider 内部的 transport helper，对齐 tushare_common.py 模式：
- ``EastmoneyRequestGate``：进程级 Lock + monotonic 最小间隔，读
  ``config.history.etf_request_min_interval_seconds``，覆盖全部东财 ETF
  接口（fund_etf_hist_em / fund_etf_category_sina 等）；
- ``EastmoneyProviderError``：异常体系与错误码（EASTMONEY_TIMEOUT /
  EASTMONEY_API_ERROR / SCHEMA_MISMATCH / UNKNOWN_INSTRUMENT），
  EASTMONEY_TIMEOUT 同时是 TimeoutError 子类（沿 call_with_metrics 超时计数）。

调用层级：``call_with_metrics``（方法级指标）→ Provider 方法（fields/校验/
normalize）→ RequestGate（节奏）→ akshare（延迟 import），职责不可互相替代。
"""

from __future__ import annotations

import logging
import threading
import time

from app.config import AppConfig
from app.observability.provider_metrics import is_timeout_error

logger = logging.getLogger(__name__)

# 已知接口行数上限（etf-data-module design D6）：返回恰等于上限视为潜在截断
ETF_HIST_ROW_CAP = 10000


class EastmoneyProviderError(Exception):
    """东财 Provider 异常；error_code 为标准化错误码。"""

    error_code = "EASTMONEY_API_ERROR"

    def __init__(self, message: str, *, error_code: str | None = None):
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code


class EastmoneyTimeoutError(EastmoneyProviderError, TimeoutError):
    """超时同时是 TimeoutError 子类：沿 call_with_metrics 的超时分类计数。"""

    error_code = "EASTMONEY_TIMEOUT"


class EastmoneySchemaMismatchError(EastmoneyProviderError):
    """接口返回 schema 与预期不符（列名缺失/类型错误），快速失败。"""

    error_code = "SCHEMA_MISMATCH"


class EastmoneyUnknownInstrumentError(EastmoneyProviderError):
    """未知证券代码（接口返回空或错误提示），快速失败。"""

    error_code = "UNKNOWN_INSTRUMENT"


def _is_timeout_exception(exc: BaseException) -> bool:
    """是否超时类异常。

    东财接口经 akshare 发出请求（requests 或 aiohttp），超时可能表现为
    TimeoutError / requests.exceptions.Timeout / httpx.TimeoutException 等。
    """
    if is_timeout_error(exc):
        return True
    message = str(exc).lower()
    return "timed out" in message or "timeout" in message


def classify_eastmoney_exception(exc: BaseException) -> EastmoneyProviderError:
    """把 akshare/网络异常归一化为带错误码的 EastmoneyProviderError。"""
    if isinstance(exc, EastmoneyProviderError):
        return exc
    if _is_timeout_exception(exc):
        return EastmoneyTimeoutError(f"东财请求超时: {type(exc).__name__}")
    return EastmoneyProviderError(f"东财请求失败: {type(exc).__name__}")


class EastmoneyRequestGate:
    """进程级东财 ETF 接口请求节奏（etf-data-module，对齐 TushareRequestGate 模式）。

    按最小间隔串行放行：锁内预留时间槽（避免持锁 sleep 阻塞其他调用），锁外
    真实等待。默认间隔由 config.history.etf_request_min_interval_seconds 读取
    （缺省 0.5s）。
    """

    def __init__(self, min_interval: float = 0.5):
        self._lock = threading.Lock()
        self._min_interval = min_interval
        self._last: float | None = None

    def acquire(self) -> None:
        """预留下一时间槽并等待到该时刻。"""
        with self._lock:
            now = time.monotonic()
            if self._last is None:
                start = now
            else:
                start = max(now, self._last + self._min_interval)
            self._last = start
            wait = start - now
        if wait > 0:
            time.sleep(wait)


_shared_gate: EastmoneyRequestGate | None = None
_shared_gate_lock = threading.Lock()


def get_shared_gate() -> EastmoneyRequestGate:
    """进程级共享 gate：未显式配置时按默认 config 构造（懒初始化）。"""
    global _shared_gate
    with _shared_gate_lock:
        if _shared_gate is None:
            from app.config import load_config

            config = load_config()
            _shared_gate = EastmoneyRequestGate(
                min_interval=config.history.etf_request_min_interval_seconds
            )
        return _shared_gate


def configure_shared_gate(gate: EastmoneyRequestGate) -> None:
    """应用启动时以实际配置初始化共享 gate（main.py lifespan 调用一次）。"""
    global _shared_gate
    with _shared_gate_lock:
        _shared_gate = gate
