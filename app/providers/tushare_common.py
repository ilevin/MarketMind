"""Tushare 共享 transport：client 构造 + 进程级请求节奏（技术方案 §30）。

Provider 内部的 transport helper，不是新的 Provider 框架：
- ``create_tushare_pro_client(config)``：统一 client 构造（超时由配置注入，
  替换各 Provider 自行 ``ts.pro_api`` 的散落写法）；
- ``TushareRequestGate``：全部 Tushare 请求共用的线程安全限流——
  ``threading.Lock + time.monotonic() + endpoint 最小间隔``，
  避免 history job / fundamental job / calendar provider 同时撞限流；
- ``TushareTransport``：client + gate 的组合入口，经 ``call(endpoint, **params)``
  发请求（懒初始化 client，import tushare 仅发生在真实调用时——离线测试
  可注入 fake client factory）。

调用层级固定（§30.1）：``call_with_metrics``（方法级指标）→ Provider 方法
（fields/校验/normalize）→ RequestGate（节奏）→ Tushare SDK，三者职责不可
互相替代。

超时归属（v0.3.0 修正）：单请求网络超时固定在**原生 SDK 请求层**
（``ts.pro_api(token, timeout=config.providers.timeout.tushare)``，最终落到
每次 ``requests.post(..., timeout=15)``），方法级不设固定 wall-clock 上限
——一个 Provider 方法可能包含多个受 gate 限流的请求（stock_basic 15 个
分片、按证券逐只补齐上千次），方法级限时既会把“慢”误判为“超时”，又会在
超时后留下仍在发请求的线程与重试重叠。等待 gate 的时间不计入单请求网络
超时（限时只包住 SDK 调用本身）。

错误分类：SDK 抛出的异常按消息特征尽力归一化为带 ``error_code`` 的
``TushareError`` 子类（技术方案 §51.3；错误文本过滤 Token——本模块构造的
异常消息不含 Token）。真实超时归一化为 ``TushareTimeoutError``，它同时是
``TimeoutError`` 子类，因而在 ``call_with_metrics`` 中计入 timeout_count。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from app.config import AppConfig
from app.observability.provider_metrics import is_timeout_error

logger = logging.getLogger(__name__)

# 已知接口行数上限（技术方案 §33）：返回恰等于上限视为潜在截断
STOCK_BASIC_ROW_CAP = 6000
STOCK_COMPANY_ROW_CAP = 4500
DAILY_ROW_CAP = 6000


class TushareError(Exception):
    """Tushare 请求层异常；error_code 为标准化错误码（技术方案 §51.3）。"""

    error_code = "TUSHARE_API_ERROR"

    def __init__(self, message: str, *, error_code: str | None = None):
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code


class TushareTokenMissingError(TushareError):
    error_code = "TUSHARE_TOKEN_MISSING"


class TusharePermissionDeniedError(TushareError):
    error_code = "TUSHARE_PERMISSION_DENIED"


class TushareRateLimitError(TushareError):
    error_code = "TUSHARE_RATE_LIMIT"


class TushareTimeoutError(TushareError, TimeoutError):
    """超时同时是 TimeoutError 子类：沿 call_with_metrics 的超时分类计数。"""

    error_code = "TUSHARE_TIMEOUT"


# 消息特征 -> 异常类（尽力分类；未命中归 TUSHARE_API_ERROR）
_MESSAGE_PATTERNS: tuple[tuple[tuple[str, ...], type[TushareError]], ...] = (
    (("每分钟", "每秒", "频次", "频率", "访问过快", "稍后再试"), TushareRateLimitError),
    (("权限", "积分不足", "没有权限", "未开通"), TusharePermissionDeniedError),
    (("token", "Token", "TOKEN", "令牌"), TushareTokenMissingError),
)


def _is_timeout_exception(exc: BaseException) -> bool:
    """是否超时类异常。

    外层的 ``is_timeout_error`` 认定 TimeoutError/httpx/requests 超时；
    Tushare SDK 经 requests 发出请求且会自行重试，超时可能表现为
    ``requests.exceptions.ConnectionError``，因此额外按异常消息特征识别
    （SDK 自行重试耗尽的提示）。
    """
    if is_timeout_error(exc):
        return True
    message = str(exc).lower()
    return "timed out" in message or "timeout" in message


def classify_tushare_exception(exc: BaseException) -> TushareError:
    """把 SDK/网络异常归一化为带错误码的 TushareError（消息脱敏，不含 Token）。"""
    if isinstance(exc, TushareError):
        return exc
    if _is_timeout_exception(exc):
        return TushareTimeoutError(f"Tushare 请求超时: {type(exc).__name__}")
    message = str(exc)
    for keywords, cls in _MESSAGE_PATTERNS:
        if any(k in message for k in keywords):
            return cls(f"Tushare 请求失败: {type(exc).__name__}")
    return TushareError(f"Tushare 请求失败: {type(exc).__name__}")


def create_tushare_pro_client(config: AppConfig):
    """创建 Tushare pro client，并注入**单请求**网络超时（技术方案 §27）。

    ``ts.pro_api(token, timeout=T)`` 的 T 由 ``DataApi`` 存放在实例上，仅在
    ``requests.post(..., timeout=T)`` 处生效——即每个真实 HTTP 请求 15s，
    而 client 构造本身不发任何请求。这是本 SDK 唯一可用的单请求超时位置：
    ``DataApi.__getattr__`` 返回 ``partial(self.query, api_name)``，因此
    ``pro.daily_basic(timeout=15)`` 会把 timeout 当作**接口参数**塞进请求体
    （而非 HTTP 超时），不能按调用传参。

    Token 未配置时抛 TushareTokenMissingError（调用方决定失败/等待语义）。
    """
    if not config.has_tushare_token:
        raise TushareTokenMissingError(
            "Tushare Token 未配置（config.yaml -> tushare.token）"
        )
    import tushare as ts

    return ts.pro_api(config.tushare.token, timeout=config.providers.timeout.tushare)


class TushareRequestGate:
    """进程级 Tushare 请求节奏（技术方案 §30.2）。

    按 endpoint 最小间隔串行放行：锁内预留时间槽（避免持锁 sleep 阻塞其他
    endpoint），锁外真实等待。默认间隔 0.6s（约 100 次/分）；stock_basic
    文档限额更严格，默认 >= 1.25s。
    """

    def __init__(
        self,
        default_min_interval: float = 0.6,
        endpoint_overrides: dict[str, float] | None = None,
    ):
        self._lock = threading.Lock()
        self._default = default_min_interval
        self._overrides = dict(endpoint_overrides or {})
        self._last: dict[str, float] = {}

    def min_interval(self, endpoint: str) -> float:
        return self._overrides.get(endpoint, self._default)

    def acquire(self, endpoint: str) -> None:
        """预留下一时间槽并等待到该时刻（技术方案 §30.3）。"""
        with self._lock:
            now = time.monotonic()
            interval = self.min_interval(endpoint)
            last = self._last.get(endpoint)
            start = now if last is None else max(now, last + interval)
            self._last[endpoint] = start
            wait = start - now
        if wait > 0:
            time.sleep(wait)


class TushareTransport:
    """client + gate 组合：全部 Tushare 请求经此发出（技术方案 §30.4）。

    client 懒初始化（首次调用才 import tushare）；gate 默认取进程级共享
    单例（全局限流的实现基础），测试可注入零间隔 gate + fake client_factory
    完全离线。SDK 异常归一化为 TushareError 子类后向上传播。
    """

    def __init__(
        self,
        config: AppConfig,
        gate: TushareRequestGate | None = None,
        client_factory: Callable[[AppConfig], Any] | None = None,
        request_timeout: float | None = None,
    ):
        self._config = config
        self._gate = gate
        self._client_factory = client_factory or create_tushare_pro_client
        self._client: Any | None = None
        # 单请求网络超时：默认取 config.providers.timeout.tushare（§27）
        self._request_timeout = (
            config.providers.timeout.tushare if request_timeout is None else request_timeout
        )

    @property
    def request_timeout(self) -> float | None:
        return self._request_timeout

    @property
    def gate(self) -> TushareRequestGate:
        if self._gate is None:
            self._gate = get_shared_gate()
        return self._gate

    def call(self, endpoint: str, **params) -> Any:
        """按 endpoint 节奏调用 Tushare SDK 方法，返回原始 DataFrame。

        超时归属：单请求网络超时由 client 承载（见
        ``create_tushare_pro_client``）——SDK 在 ``DataApi.query`` 内写
        ``requests.post(url, json=..., timeout=self.__timeout)``，且一次调用
        只发这一个 HTTP 请求，因此该超时恰好等于"单请求网络超时"，而不是
        方法级 wall-clock 上限。先等 gate 再发请求，故等待 gate 的时间不
        计入网络超时（gate 只负责节流）。
        """
        if self._client is None:
            self._client = self._client_factory(self._config)
        self.gate.acquire(endpoint)
        method = getattr(self._client, endpoint, None)
        if method is None:
            raise TushareError(f"Tushare 接口不存在: {endpoint}", error_code="INTERNAL_ERROR")
        try:
            return method(**params)
        except Exception as exc:  # SDK/网络异常统一归一化后传播
            raise classify_tushare_exception(exc) from exc


class TushareClientProxy:
    """SDK client 的方法级代理：endpoint 属性访问转发到 transport。

    兼容既有 ``pro.daily_basic(**params)`` 调用形态，使共享 transport 改造
    不破坏 Provider 现有接缝（测试可继续以 fake client 替换 ``_pro``）。
    """

    def __init__(self, transport: TushareTransport):
        self._transport = transport

    def __getattr__(self, endpoint: str) -> Callable[..., Any]:
        def call(**params):
            return self._transport.call(endpoint, **params)

        return call


def build_gate_from_config(config: AppConfig) -> TushareRequestGate:
    """按 config.history 构造请求节奏 gate（技术方案 §30.3、§57）。"""
    history = config.history
    return TushareRequestGate(
        default_min_interval=history.request_min_interval_seconds,
        endpoint_overrides={
            "stock_basic": max(
                history.stock_basic_min_interval_seconds,
                history.request_min_interval_seconds,
            )
        },
    )


_shared_gate: TushareRequestGate | None = None
_shared_gate_lock = threading.Lock()


def get_shared_gate() -> TushareRequestGate:
    """进程级共享 gate：未显式配置时按默认 config 构造（懒初始化）。"""
    global _shared_gate
    with _shared_gate_lock:
        if _shared_gate is None:
            from app.config import load_config

            _shared_gate = build_gate_from_config(load_config())
        return _shared_gate


def configure_shared_gate(gate: TushareRequestGate) -> None:
    """应用启动时以实际配置初始化共享 gate（main.py lifespan 调用一次）。"""
    global _shared_gate
    with _shared_gate_lock:
        _shared_gate = gate
