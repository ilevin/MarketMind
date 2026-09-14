"""AuthService 单元测试（multi-user-auth 2.5）：登录、Session 生命周期、限速。

Session 生命周期覆盖：过期、撤销、禁用失效、密码重置失效（user-authentication spec）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.auth.rate_limit import LoginRateLimiter, login_rate_key
from app.auth.session import hash_token
from app.models.user import utcnow
from app.services.auth_service import (
    AccountDisabledError,
    AuthService,
    InvalidCredentialsError,
    RateLimitedError,
)
from app.services.user_service import UserService

KEY = "127.0.0.1|alice"


@pytest.fixture()
def limiter():
    return LoginRateLimiter()


@pytest.fixture()
def auth(session):
    return AuthService(session)


def _make_user(session, username="alice", password="password123", role="user", active=True):
    user = UserService(session).create_user(username=username, password=password, role=role)
    if not active:
        UserService(session).set_active(user.user_id, False)
    return user


# ---- 登录 ----


def test_login_success_returns_token_and_user(session, auth, limiter):
    _make_user(session)
    result = auth.login(username="alice", password="password123", client_key=KEY, limiter=limiter)
    assert result.token
    assert result.user.username == "alice"
    assert result.user.role == "user"
    assert result.user.session_token_hash == hash_token(result.token)


def test_login_username_case_insensitive(session, auth, limiter):
    _make_user(session, username="Alice")
    result = auth.login(username="ALICE", password="password123", client_key=KEY, limiter=limiter)
    assert result.user.username == "Alice"


def test_login_wrong_password_unified_error(session, auth, limiter):
    _make_user(session)
    with pytest.raises(InvalidCredentialsError):
        auth.login(username="alice", password="wrongpass1", client_key=KEY, limiter=limiter)


def test_login_unknown_user_unified_error(session, auth, limiter):
    """与密码错误同文案，不泄露账户存在性。"""
    with pytest.raises(InvalidCredentialsError) as ei:
        auth.login(username="nobody", password="whatever12", client_key=KEY, limiter=limiter)
    assert str(ei.value) == "用户名或密码错误"


def test_login_disabled_user_rejected(session, auth, limiter):
    _make_user(session, active=False)
    with pytest.raises(AccountDisabledError):
        auth.login(username="alice", password="password123", client_key=KEY, limiter=limiter)


def test_login_updates_last_login(session, auth, limiter):
    user = _make_user(session)
    assert user.last_login_at is None
    auth.login(username="alice", password="password123", client_key=KEY, limiter=limiter)
    assert user.last_login_at is not None


# ---- Session 校验链 ----


def test_resolve_session_roundtrip(session, auth, limiter):
    _make_user(session)
    result = auth.login(username="alice", password="password123", client_key=KEY, limiter=limiter)
    current = auth.resolve_session(result.token)
    assert current is not None and current.user_id == result.user.user_id


def test_resolve_session_invalid_token(session, auth):
    assert auth.resolve_session("no-such-token") is None
    assert auth.resolve_session(None) is None
    assert auth.resolve_session("") is None


def test_resolve_session_expired(session, auth, limiter):
    _make_user(session)
    result = auth.login(username="alice", password="password123", client_key=KEY, limiter=limiter)
    # 直接把过期时间改到过去（单写者测试环境，无并发窗口）
    from app.models.user_session import UserSession

    row = session.get(UserSession, hash_token(result.token))
    row.expires_at = utcnow() - timedelta(seconds=1)
    session.commit()
    assert auth.resolve_session(result.token) is None


def test_logout_revokes_session(session, auth, limiter):
    _make_user(session)
    result = auth.login(username="alice", password="password123", client_key=KEY, limiter=limiter)
    auth.logout(result.user)
    assert auth.resolve_session(result.token) is None


def test_disabled_user_session_invalid(session, auth, limiter):
    """禁用用户后既有 Session 立即失效（resolve 链 is_active 检查）。"""
    user = _make_user(session)
    result = auth.login(username="alice", password="password123", client_key=KEY, limiter=limiter)
    UserService(session).set_active(user.user_id, False)
    assert auth.resolve_session(result.token) is None


def test_password_reset_revokes_all_sessions(session, auth, limiter):
    user = _make_user(session)
    r1 = auth.login(username="alice", password="password123", client_key=KEY, limiter=limiter)
    r2 = auth.login(username="alice", password="password123", client_key=KEY, limiter=limiter)
    UserService(session).reset_password(user.user_id, "newpassword456")
    assert auth.resolve_session(r1.token) is None
    assert auth.resolve_session(r2.token) is None
    # 新密码可登录
    r3 = auth.login(username="alice", password="newpassword456", client_key=KEY, limiter=limiter)
    assert auth.resolve_session(r3.token) is not None


# ---- 修改自己的密码 ----


def test_change_password_revokes_current_session(session, auth, limiter):
    _make_user(session)
    result = auth.login(username="alice", password="password123", client_key=KEY, limiter=limiter)
    auth.change_password(
        result.user, old_password="password123", new_password="newpassword456"
    )
    assert auth.resolve_session(result.token) is None
    r2 = auth.login(username="alice", password="newpassword456", client_key=KEY, limiter=limiter)
    assert r2.user.user_id == result.user.user_id


def test_change_password_wrong_old_password(session, auth, limiter):
    _make_user(session)
    result = auth.login(username="alice", password="password123", client_key=KEY, limiter=limiter)
    with pytest.raises(InvalidCredentialsError):
        auth.change_password(
            result.user, old_password="wrongpass00", new_password="newpassword456"
        )


# ---- 登录限速 ----


def test_rate_limit_blocks_after_failures(session, auth, limiter):
    _make_user(session)
    for _ in range(5):
        with pytest.raises(InvalidCredentialsError):
            auth.login(username="alice", password="wrongpass1", client_key=KEY, limiter=limiter)
    # 第 6 次：即使密码正确也拒绝
    with pytest.raises(RateLimitedError):
        auth.login(username="alice", password="password123", client_key=KEY, limiter=limiter)


def test_rate_limit_isolated_by_key(session, auth, limiter):
    _make_user(session)
    other_key = login_rate_key("127.0.0.1", "alice")
    for _ in range(5):
        with pytest.raises(InvalidCredentialsError):
            auth.login(
                username="alice", password="wrongpass1", client_key=other_key, limiter=limiter
            )
    # 不同 IP 不受影响
    result = auth.login(
        username="alice",
        password="password123",
        client_key=login_rate_key("10.0.0.1", "alice"),
        limiter=limiter,
    )
    assert result.user.username == "alice"


def test_rate_limit_success_resets_counter(session, auth, limiter):
    _make_user(session)
    for _ in range(4):
        with pytest.raises(InvalidCredentialsError):
            auth.login(username="alice", password="wrongpass1", client_key=KEY, limiter=limiter)
    # 第 5 次成功：计数清零
    auth.login(username="alice", password="password123", client_key=KEY, limiter=limiter)
    # 再失败 4 次仍未达阈值
    for _ in range(4):
        with pytest.raises(InvalidCredentialsError):
            auth.login(username="alice", password="wrongpass1", client_key=KEY, limiter=limiter)
    auth.login(username="alice", password="password123", client_key=KEY, limiter=limiter)
