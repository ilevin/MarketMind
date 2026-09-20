"""RetryPolicy 离线单测（tasks 5.9，技术方案 §29/§69.3）。

纯策略对象无 I/O：``sleep`` / ``random_fn`` 全部注入，测试不真实等待。
覆盖退避序列与上限（§29.1：5/10/20/40/80/160/300/300/300）、抖动区间
（0.8~1.2 由 jitter_ratio 配置）、以及配置类错误的快速失败分类（§29.3）。
"""

from __future__ import annotations

import pytest

from app.config import AppConfig
from app.services.history.retry import RetryPolicy, is_config_error


def make_policy(**config_overrides) -> tuple[RetryPolicy, list[float]]:
    """构造注入式策略：sleep 只记录时长，random_fn 固定返回中点（抖动系数 1.0）。"""
    config = AppConfig()
    for key, value in config_overrides.items():
        setattr(config.history, key, value)
    slept: list[float] = []
    policy = RetryPolicy(config, sleep=slept.append, random_fn=lambda: 0.5)
    return policy, slept


class TestDelaySequence:
    def test_exponential_backoff_sequence_matches_spec(self) -> None:
        """§29.1：min(5 * 2^(n-1), 300)，随机中点时无抖动偏差。"""
        policy, _ = make_policy()
        delays = [policy.delay_seconds(attempt) for attempt in range(1, 10)]
        assert delays == pytest.approx([5, 10, 20, 40, 80, 160, 300, 300, 300])

    def test_delay_capped_at_configured_maximum(self) -> None:
        """第 7 次起封顶；更后面的尝试不继续增长。"""
        policy, _ = make_policy()
        assert policy.delay_seconds(7) == pytest.approx(300)
        assert policy.delay_seconds(20) == pytest.approx(300)

    def test_custom_config_changes_sequence(self) -> None:
        policy, _ = make_policy(
            backoff_initial_seconds=2, backoff_max_seconds=10, jitter_ratio=0.0
        )
        delays = [policy.delay_seconds(attempt) for attempt in range(1, 5)]
        assert delays == pytest.approx([2, 4, 8, 10])


class TestJitter:
    def test_jitter_stays_within_configured_ratio(self) -> None:
        """抖动系数落在 [1-ratio, 1+ratio]；取 random 端点验证上下界。"""
        config = AppConfig()
        low = RetryPolicy(config, sleep=lambda _: None, random_fn=lambda: 0.0)
        high = RetryPolicy(config, sleep=lambda _: None, random_fn=lambda: 1.0)
        base = config.history.backoff_initial_seconds
        ratio = config.history.jitter_ratio
        assert low.delay_seconds(1) == pytest.approx(base * (1 - ratio))
        assert high.delay_seconds(1) == pytest.approx(base * (1 + ratio))

    def test_zero_jitter_ratio_disables_jitter(self) -> None:
        policy, _ = make_policy(jitter_ratio=0.0)
        assert policy.delay_seconds(1) == pytest.approx(5)
        assert policy.delay_seconds(3) == pytest.approx(20)


class TestSleepInjection:
    def test_sleep_before_retry_uses_injected_sleep_only(self) -> None:
        """不真实等待：sleep 被替换为记录器，时长等于 delay_seconds。"""
        policy, slept = make_policy()
        for attempt in (1, 2, 3):
            policy.sleep_before_retry(attempt)
        assert slept == pytest.approx([5, 10, 20])

    def test_max_attempts_read_from_config(self) -> None:
        policy, _ = make_policy(max_attempts=3)
        assert policy.max_attempts == 3
        default_policy, _ = make_policy()
        assert default_policy.max_attempts == 10


class TestConfigErrorClassification:
    def test_config_errors_are_identified(self) -> None:
        """§29.3：Token 缺失/无权限/schema 不匹配/字段映射错误不盲目重试。"""
        for code in (
            "TUSHARE_TOKEN_MISSING",
            "TUSHARE_PERMISSION_DENIED",
            "SCHEMA_MISMATCH",
            "UNKNOWN_INSTRUMENT",
        ):
            assert is_config_error(code) is True

    def test_retryable_errors_are_not_config_errors(self) -> None:
        for code in (
            "TUSHARE_TIMEOUT",
            "TUSHARE_RATE_LIMIT",
            "TUSHARE_API_ERROR",
            "EMPTY_RESULT",
            "TRUNCATION_RISK",
            "DUPLICATE_KEY",
            "TRADE_DATE_MISMATCH",
            "INVALID_VALUE",
            "CALENDAR_UNAVAILABLE",
            "DATABASE_ERROR",
            "INTERNAL_ERROR",
        ):
            assert is_config_error(code) is False

    def test_unknown_error_code_is_retryable(self) -> None:
        assert is_config_error("SOMETHING_NEW") is False
