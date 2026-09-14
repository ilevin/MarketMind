"""登录限速集成测试（multi-user-auth tasks 9.4，user-authentication spec「登录限速」）。

进程内 LoginRateLimiter 按 IP + username 维度计数（design D14，单进程部署）：

- 同 IP+username 连续 5 次失败后，第 6 次尝试 429（即使密码正确也不创建 Session）；
- 不同 username 计数互不影响（A 被限速时 B 正常登录）；
- 登录成功重置该 key 计数；
- 窗口外恢复（单元级 monkeypatch time.monotonic 验证时间窗滑出）。

说明：conftest 的 autouse fixture 已按测试清零全局 login_rate_limiter；
API 级用例统一走匿名 TestClient（其 client IP 恒为 "testclient"，
天然构成「同 IP、不同 username」的对照场景），登录接口豁免 CSRF。
"""

from __future__ import annotations

import time

from app.auth.rate_limit import LoginRateLimiter, login_rate_key


class FakeNameProvider:
    """名称识别假件：登录路径不依赖真实标的名称。"""

    def get_name(self, market, asset_type, symbol):
        return None


def _login(client, username: str, password: str):
    """POST /api/auth/login 薄封装。"""
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_sixth_attempt_rejected_even_with_correct_password(client_factory, user_factory):
    """同 IP+username 连续 5 次密码错误后，第 6 次（即使密码正确）返回 429。"""
    user_factory("alice")
    client = client_factory(FakeNameProvider())

    for _ in range(5):
        resp = _login(client, "alice", "totally-wrong")
        assert resp.status_code == 401

    resp = _login(client, "alice", "password123")
    assert resp.status_code == 429
    # 429 响应体带明确限速提示，且不创建 Session
    assert "频繁" in resp.json()["detail"]
    assert "marketmind_session" not in resp.cookies


def test_rate_limit_isolated_per_username(client_factory, user_factory):
    """A 被限速时，同 IP 的 B 正常登录不受影响（计数按 username 隔离）。"""
    user_factory("alice")
    user_factory("bob")
    client = client_factory(FakeNameProvider())

    # alice 连续 5 次失败进入限速
    for _ in range(5):
        assert _login(client, "alice", "totally-wrong").status_code == 401
    assert _login(client, "alice", "password123").status_code == 429

    # bob：正常登录 200 并下发 Session Cookie
    resp = _login(client, "bob", "password123")
    assert resp.status_code == 200
    assert "marketmind_session" in resp.cookies

    # bob 的失败计数独立：错一次密码仍是 401 而非 429
    resp = _login(client, "bob", "totally-wrong")
    assert resp.status_code == 401


def test_successful_login_resets_failure_count(client_factory, user_factory):
    """登录成功重置该 key 计数：4 次失败 -> 成功 -> 再失败 1 次仍可正常登录。

    若成功登录不重置计数，4+1=5 次失败会使下一次尝试 429；
    重置生效时计数仅累计 1 次，随后正确密码登录应返回 200。
    """
    user_factory("alice")
    client = client_factory(FakeNameProvider())

    for _ in range(4):
        assert _login(client, "alice", "totally-wrong").status_code == 401

    # 第 5 次尝试密码正确：未达阈值，登录成功并重置计数
    assert _login(client, "alice", "password123").status_code == 200

    # 再失败 1 次（若未重置则此处累计达 5 次）
    assert _login(client, "alice", "totally-wrong").status_code == 401

    # 重置生效：正确密码登录不被 429 拒绝
    assert _login(client, "alice", "password123").status_code == 200


def test_window_slide_unblocks_after_expiry(monkeypatch):
    """单元级：窗口外的失败记录滑出后 is_blocked 恢复 False，计数从零开始。"""
    limiter = LoginRateLimiter(max_failures=5, window_seconds=60.0)
    key = login_rate_key("203.0.113.7", "alice")

    for _ in range(5):
        limiter.record_failure(key)
    assert limiter.is_blocked(key) is True

    # 时间推进到窗口之外（窗口内失败记录全部滑出）
    now = time.monotonic()
    monkeypatch.setattr("app.auth.rate_limit.time.monotonic", lambda: now + 60.001)
    assert limiter.is_blocked(key) is False

    # 窗口外新失败从零计数：单次失败不会再次触发封锁
    limiter.record_failure(key)
    assert limiter.is_blocked(key) is False


def test_tracked_keys_capped(monkeypatch):
    """单元级：伪造 key 不能无界撑大计数字典（内存 DoS 防护）。

    伪造超过 MAX_TRACKED_KEYS 个 key 后：最旧一半被淘汰、字典总量压回
    上限之下，且活跃 key（窗口内新失败）的封锁判定不受影响。
    """
    limiter = LoginRateLimiter(max_failures=5, window_seconds=60.0)
    from app.auth.rate_limit import MAX_TRACKED_KEYS

    now = time.monotonic()

    # 第一批：旧时间戳的失败（被后续超限淘汰策略视为最旧）
    for i in range(MAX_TRACKED_KEYS):
        limiter.record_failure(f"10.0.0.{i}|victim{i}")
    assert len(limiter._failures) <= MAX_TRACKED_KEYS

    # 第二批：再压入新 key，触发"仍超限 → 丢最旧一半"
    for i in range(1000):
        limiter.record_failure(f"10.1.0.{i}|attacker{i}")
    assert len(limiter._failures) < MAX_TRACKED_KEYS

    # 活跃 key 的封锁判定仍然正确
    hot = login_rate_key("10.1.0.9", "attacker9")
    for _ in range(5):
        limiter.record_failure(hot)
    assert limiter.is_blocked(hot) is True
