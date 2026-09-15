"""进程内登录限速（multi-user-auth design D14）：IP + username 维度。

单进程部署（uvicorn --workers 1）下进程内状态即可满足防暴力尝试目标，
不引入 Redis；重启清零可接受（目标是缓解而非严格配额）。线程安全：
FastAPI 同步依赖跑在 threadpool 中。
"""

from __future__ import annotations

import threading
import time
from collections import deque

DEFAULT_MAX_FAILURES = 5
DEFAULT_WINDOW_SECONDS = 300
# 追踪 key 上限：防伪造 IP/用户名组合无界撑大 _failures 字典（内存 DoS）
MAX_TRACKED_KEYS = 10_000


def login_rate_key(client_ip: str, username: str) -> str:
    return f"{client_ip}|{username.strip().lower()}"


def setup_rate_key(client_ip: str) -> str:
    """首访初始化按 IP 限速，避免通过更换用户名绕过配额。"""
    return f"setup|{client_ip}"


class LoginRateLimiter:
    def __init__(
        self,
        max_failures: int = DEFAULT_MAX_FAILURES,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
    ):
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        """移除窗口外的失败记录，返回剩余队列。"""
        queue = self._failures.get(key)
        if queue is None:
            return deque()
        while queue and queue[0] <= now - self._window_seconds:
            queue.popleft()
        return queue

    def _evict(self, now: float) -> None:
        """删除已无有效失败记录的 key，并把 key 总数压回上限内。

        过期 key 在任何锁内操作时顺带回收（否则 reset 前只增不删）；
        仍超 MAX_TRACKED_KEYS 时按最近失败时间丢弃最旧一半，保证内存有上界
        （正常登录流量下远达不到上限，此路径仅对抗伪造 key 的 DoS）。
        """
        for key in [k for k, q in self._failures.items() if not q or q[-1] <= now - self._window_seconds]:
            del self._failures[key]
        if len(self._failures) >= MAX_TRACKED_KEYS:
            oldest = sorted(self._failures, key=lambda k: self._failures[k][-1])
            for key in oldest[: len(oldest) // 2 or 1]:
                del self._failures[key]

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            now = time.monotonic()
            self._evict(now)
            return len(self._prune(key, now)) >= self._max_failures

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._evict(now)
            queue = self._failures.setdefault(key, deque())
            while queue and queue[0] <= now - self._window_seconds:
                queue.popleft()
            queue.append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def clear(self) -> None:
        """清空全部计数（测试隔离用；生产重启进程即清零，等价语义）。"""
        with self._lock:
            self._failures.clear()


# 模块级单例：与 write_coordinator 同思路，全部登录路径共用
login_rate_limiter = LoginRateLimiter()
# setup 独立按 IP 计数，避免登录失败影响首次初始化。
setup_rate_limiter = LoginRateLimiter()
