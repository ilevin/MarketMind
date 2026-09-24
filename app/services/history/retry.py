"""单数据集×单交易日重试编排（a-share-historical-data，design.md 第 8 节；
per-stock-history-sync D13）。

纯策略对象，不做 I/O：``sleep``/``random`` 可注入以支持测试不真实等待。
指数退避 ``min(initial * 2^(attempt-1), cap) * jitter``，
``jitter`` 在 ``[1-jitter_ratio, 1+jitter_ratio]`` 均匀采样。

默认总尝试次数 = max_retries + 1（默认 max_retries=3，总尝试 4 次）。
退避序列约 5/10/20 秒（第 1/2/3 次重试前等待）。
"""

from __future__ import annotations

import random as _random_module
import time as _time_module
from collections.abc import Callable

from app.config import AppConfig

# 配置类错误：不会随重试自愈，快速失败不睡满所有重试轮（design.md 第 8 节）。
# Token 缺失/权限拒绝（账户配置问题）、schema 不匹配/字段映射错误（代码或
# 上游接口变更问题）、别名冲突（同一证券同一天的两种取值互相矛盾，必须人工
# 用权威来源判定）——均需人工介入，重试无意义。
CONFIG_ERROR_CODES: frozenset[str] = frozenset(
    {
        "TUSHARE_TOKEN_MISSING",
        "TUSHARE_PERMISSION_DENIED",
        "SCHEMA_MISMATCH",
        "UNKNOWN_INSTRUMENT",
        "ALIAS_CONFLICT",
    }
)


def is_config_error(error_code: str) -> bool:
    """True 表示配置类错误：立即判定该次尝试为终态，不继续重试等待。"""
    return error_code in CONFIG_ERROR_CODES


class RetryPolicy:
    """退避序列计算 + 可注入 sleep（不做重试次数循环——由调用方 Service 驱动）。

    属性：
        max_retries: 额外重试次数，总尝试次数 = max_retries + 1。
        max_attempts: 总尝试次数（向后兼容属性，等价于 max_retries + 1）。
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        sleep: Callable[[float], None] | None = None,
        random_fn: Callable[[], float] | None = None,
    ):
        self.max_retries = config.history.max_retries
        self.backoff_initial_seconds = config.history.backoff_initial_seconds
        self.backoff_max_seconds = config.history.backoff_max_seconds
        self.jitter_ratio = config.history.jitter_ratio
        self._sleep = sleep or _time_module.sleep
        self._random = random_fn or _random_module.random

    @property
    def max_attempts(self) -> int:
        """总尝试次数（向后兼容属性：max_retries + 1）。"""
        return self.max_retries + 1

    @max_attempts.setter
    def max_attempts(self, value: int) -> None:
        """设置总尝试次数 → 换算为 max_retries（向后兼容）。"""
        self.max_retries = max(0, value - 1)

    def delay_seconds(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后（attempt 从 1 起）的退避时长，含抖动。

        attempt 为失败后的第几次重试等待：第 1 次失败后等待 ~5 秒，
        第 2 次失败后等待 ~10 秒，第 3 次失败后等待 ~20 秒。
        """
        base = min(
            self.backoff_initial_seconds * (2 ** (attempt - 1)),
            self.backoff_max_seconds,
        )
        jitter = 1 - self.jitter_ratio + 2 * self.jitter_ratio * self._random()
        return base * jitter

    def sleep_before_retry(self, attempt: int) -> None:
        self._sleep(self.delay_seconds(attempt))
