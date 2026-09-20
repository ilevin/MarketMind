"""单数据集×单交易日重试编排（a-share-historical-data，design.md 第 8 节、
spec.md"失败日期绝不跳过"）。

纯策略对象，不做 I/O：``sleep``/``random`` 可注入以支持测试不真实等待
（任务 5.5/5.9）。指数退避 ``min(initial * 2^(attempt-1), cap) * jitter``，
``jitter`` 在 ``[1-jitter_ratio, 1+jitter_ratio]`` 均匀采样。
"""

from __future__ import annotations

import random as _random_module
import time as _time_module
from collections.abc import Callable

from app.config import AppConfig

# 配置类错误：不会随重试自愈，快速失败不睡满 max_attempts 轮（design.md 第 8 节）。
# Token 缺失/权限拒绝（账户配置问题）、schema 不匹配/字段映射错误（代码或
# 上游接口变更问题）——均需人工介入，重试无意义。
CONFIG_ERROR_CODES: frozenset[str] = frozenset(
    {
        "TUSHARE_TOKEN_MISSING",
        "TUSHARE_PERMISSION_DENIED",
        "SCHEMA_MISMATCH",
        "UNKNOWN_INSTRUMENT",
    }
)


def is_config_error(error_code: str) -> bool:
    """True 表示配置类错误：立即判定该次尝试为终态，不继续重试等待。"""
    return error_code in CONFIG_ERROR_CODES


class RetryPolicy:
    """退避序列计算 + 可注入 sleep（不做重试次数循环——由调用方 Service 驱动）。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        sleep: Callable[[float], None] | None = None,
        random_fn: Callable[[], float] | None = None,
    ):
        self.max_attempts = config.history.max_attempts
        self.backoff_initial_seconds = config.history.backoff_initial_seconds
        self.backoff_max_seconds = config.history.backoff_max_seconds
        self.jitter_ratio = config.history.jitter_ratio
        self._sleep = sleep or _time_module.sleep
        self._random = random_fn or _random_module.random

    def delay_seconds(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后（attempt 从 1 起）的退避时长，含抖动。"""
        base = min(
            self.backoff_initial_seconds * (2 ** (attempt - 1)),
            self.backoff_max_seconds,
        )
        jitter = 1 - self.jitter_ratio + 2 * self.jitter_ratio * self._random()
        return base * jitter

    def sleep_before_retry(self, attempt: int) -> None:
        self._sleep(self.delay_seconds(attempt))
