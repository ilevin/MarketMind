"""认证 API 集成测试（multi-user-auth tasks 3.5）：登录链路全场景。

覆盖 user-authentication spec：
- 登录成功（Set-Cookie 安全属性）与 /api/auth/me；
- 密码错误 / 用户名不存在统一 401（不区分失败原因，不下发 Cookie）；
- 用户名大小写不敏感；禁用账户 403 且不创建 Session；
- Session 过期与撤销（logout）后原 Cookie 401；
- change-password 校验（旧密码错 401 / 弱密码 422）与全量 Session 失效；
- 匿名访问业务 API 401、页面 302 → /login；user → admin 路由 403。

直接改库（禁用用户、Session 过期）走 ORM + write_coordinator（单写者模型，
与 app 写路径一致，避免 DuckDB 并发写冲突）。
"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import select, update

from app.auth.session import SESSION_COOKIE_NAME
from app.db import write_coordinator
from app.models import AppUser, UserSession
from app.models.user import utcnow


class FakeNameProvider:
    """认证链路不涉及名称识别，恒返回 None 即可。"""

    def get_name(self, market, asset_type, symbol):
        return None


def _session_cookie_headers(response) -> list[str]:
    """响应中 marketmind_session 的全部 Set-Cookie 原始串。"""
    return [
        c
        for c in response.headers.get_list("set-cookie")
        if c.lower().startswith(f"{SESSION_COOKIE_NAME}=")
    ]


# ---------------------------------------------------------------------------
# 登录成功 / 失败
# ---------------------------------------------------------------------------


def test_login_success_sets_session_cookie_and_me(client_factory, user_factory):
    """正确凭据登录：200，Set-Cookie 带 HttpOnly / SameSite=Lax / Path=/，/api/auth/me 正确。"""
    user = user_factory("bob")
    with client_factory(FakeNameProvider()) as client:
        resp = client.post(
            "/api/auth/login", json={"username": "bob", "password": "password123"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"username": "bob", "role": "user"}

        # Set-Cookie：仅一个 marketmind_session，属性符合 Session Cookie 安全要求
        cookies = _session_cookie_headers(resp)
        assert len(cookies) == 1
        lower = cookies[0].lower()
        assert "httponly" in lower
        assert "samesite=lax" in lower
        assert "path=/" in lower

        # Cookie 值为不透明随机 token（base64url），不携带 user_id / 角色 / 用户名
        token = cookies[0].split(";", 1)[0].split("=", 1)[1]
        assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", token)

        # TestClient 已持有该 Cookie：/api/auth/me 解析为同一用户
        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json() == {
            "user_id": user["user_id"],
            "username": "bob",
            "role": "user",
        }


def test_login_failures_do_not_reveal_reason(client_factory, user_factory):
    """密码错误与用户名不存在均 401：文案一致、不下发 Session Cookie。"""
    user_factory("carol")
    with client_factory(FakeNameProvider()) as client:
        wrong_pw = client.post(
            "/api/auth/login", json={"username": "carol", "password": "totally-wrong-9"}
        )
        assert wrong_pw.status_code == 401

        unknown = client.post(
            "/api/auth/login", json={"username": "ghost_user", "password": "password123"}
        )
        assert unknown.status_code == 401

        # 提示不区分"用户不存在"与"密码错误"（spec：不存在的用户登录失败）
        assert wrong_pw.json()["detail"] == unknown.json()["detail"] == "用户名或密码错误"
        # 失败不下发 Session Cookie（spec：错误密码登录失败）
        assert _session_cookie_headers(wrong_pw) == []
        assert _session_cookie_headers(unknown) == []


def test_login_username_case_insensitive(client_factory, user_factory):
    """用户名匹配大小写不敏感：建号 Alice 后 alice / ALICE 均登录同一账户。"""
    user = user_factory("Alice")
    with client_factory(FakeNameProvider()) as client:
        for variant in ("alice", "ALICE"):
            resp = client.post(
                "/api/auth/login",
                json={"username": variant, "password": "password123"},
            )
            assert resp.status_code == 200, variant
            # 响应返回库内规范用户名（建号时的写法）
            assert resp.json()["username"] == "Alice"

        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["user_id"] == user["user_id"]
        assert me.json()["username"] == "Alice"


# ---------------------------------------------------------------------------
# 禁用账户 / Session 生命周期
# ---------------------------------------------------------------------------


def test_login_disabled_user_rejected(client_factory, user_factory, session_factory):
    """禁用账户（is_active=false）登录返回 403，且不创建 Session。"""
    user = user_factory("dave")
    with session_factory() as s:
        with write_coordinator.write():
            s.execute(
                update(AppUser)
                .where(AppUser.user_id == user["user_id"])
                .values(is_active=False)
            )
            s.commit()

    with client_factory(FakeNameProvider()) as client:
        resp = client.post(
            "/api/auth/login", json={"username": "dave", "password": "password123"}
        )
        assert resp.status_code == 403
        assert "禁用" in resp.json()["detail"]

    # 不创建 Session（spec：禁用用户不能登录）
    with session_factory() as s:
        rows = s.execute(
            select(UserSession.user_id).where(UserSession.user_id == user["user_id"])
        ).all()
        assert rows == []


def test_expired_session_rejected(client_factory, session_factory):
    """Session 过期：expires_at 置为过去后，原 Cookie 访问 /api/auth/me 401。"""
    with client_factory(FakeNameProvider(), login_as="erin") as client:
        # 前置：登录态有效
        assert client.get("/api/auth/me").status_code == 200

        # 直接改库把该用户全部有效 Session 的过期时间置为过去
        with session_factory() as s:
            user_id = s.execute(
                select(AppUser.user_id).where(AppUser.username == "erin")
            ).scalar_one()
            with write_coordinator.write():
                s.execute(
                    update(UserSession)
                    .where(
                        UserSession.user_id == user_id,
                        UserSession.revoked_at.is_(None),
                    )
                    .values(expires_at=utcnow() - timedelta(hours=1))
                )
                s.commit()

        assert client.get("/api/auth/me").status_code == 401


def test_logout_revokes_session_and_clears_cookie(client_factory):
    """logout：撤销当前 Session、响应清除 Cookie；原 Cookie 再访问 401。"""
    with client_factory(FakeNameProvider(), login_as="frank") as client:
        old_token = client.cookies.get(SESSION_COOKIE_NAME)
        assert old_token

        resp = client.post("/api/auth/logout")
        assert resp.status_code == 200
        assert resp.json() == {"success": True}

        # 响应清除 Cookie（delete_cookie → Max-Age=0 / Expires 置于过去）
        cleared = _session_cookie_headers(resp)
        assert len(cleared) == 1
        assert "max-age=0" in cleared[0].lower()

        # 显式携带旧 Cookie（排除客户端 jar 已被清空的干扰）→ 401
        me = client.get(
            "/api/auth/me", headers={"Cookie": f"{SESSION_COOKIE_NAME}={old_token}"}
        )
        assert me.status_code == 401


# ---------------------------------------------------------------------------
# 修改密码
# ---------------------------------------------------------------------------


def test_change_password_validation_and_revocation(client_factory):
    """change-password：旧密码错 401、弱密码 422；成功后全部 Session 失效、新密码可登录。"""
    with client_factory(FakeNameProvider(), login_as="grace") as client:
        first_token = client.cookies.get(SESSION_COOKIE_NAME)
        assert first_token

        # 旧密码错误 → 401
        resp = client.post(
            "/api/auth/change-password",
            json={"old_password": "wrong-old-99", "new_password": "new-pass-456"},
        )
        assert resp.status_code == 401

        # 密码未变更：原密码仍可登录（用独立匿名客户端探测，不干扰当前 Cookie/CSRF）
        probe = client_factory(FakeNameProvider())
        probe_resp = probe.post(
            "/api/auth/login", json={"username": "grace", "password": "password123"}
        )
        assert probe_resp.status_code == 200
        probe_token = probe.cookies.get(SESSION_COOKIE_NAME)

        # 新密码不足 8 位 → 422
        resp = client.post(
            "/api/auth/change-password",
            json={"old_password": "password123", "new_password": "short12"},
        )
        assert resp.status_code == 422

        # 正确旧密码 + 合规新密码 → 200
        resp = client.post(
            "/api/auth/change-password",
            json={"old_password": "password123", "new_password": "new-pass-456"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"success": True}

        # 该用户全部 Session 失效：首次登录与探测登录的 Cookie 均 401
        me = client.get(
            "/api/auth/me", headers={"Cookie": f"{SESSION_COOKIE_NAME}={first_token}"}
        )
        assert me.status_code == 401
        assert probe.get("/api/auth/me").status_code == 401
        assert probe_token

        # 旧密码不能再登录，新密码可以
        resp = client.post(
            "/api/auth/login", json={"username": "grace", "password": "password123"}
        )
        assert resp.status_code == 401
        resp = client.post(
            "/api/auth/login", json={"username": "grace", "password": "new-pass-456"}
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 匿名访问与 admin 权限
# ---------------------------------------------------------------------------


def test_anonymous_api_401_and_pages_redirect_to_login(client_factory):
    """匿名访问：业务 API 401（含匿名写请求不触发 CSRF）；页面 302 → /login。"""
    with client_factory(FakeNameProvider()) as client:
        # 业务 / admin API：未登录 401
        for url in (
            "/api/auth/me",
            "/api/quotes",
            "/api/watchlist",
            "/api/tags",
            "/api/admin/status",
        ):
            assert client.get(url).status_code == 401, url

        # 匿名写请求（无 Cookie）不触发 CSRF，由认证依赖返回 401
        resp = client.post(
            "/api/watchlist",
            json={"symbol": "600519", "market": "CN", "asset_type": "STOCK"},
        )
        assert resp.status_code == 401

        # 页面：未登录 302 → /login（须关闭重定向跟随才能断言 302）
        for url in ("/", "/watchlist", "/tags", "/change-password", "/admin/users"):
            resp = client.get(url, follow_redirects=False)
            assert resp.status_code == 302, url
            assert resp.headers["location"] == "/login"

        # 匿名可达：登录页与健康检查
        assert client.get("/login").status_code == 200
        assert client.get("/health").status_code == 200


def test_normal_user_forbidden_from_admin_routes(client_factory):
    """普通登录用户：/api/admin/status 403、/admin/users 页面 403（spec：非 admin 拒绝）。"""
    with client_factory(FakeNameProvider(), login_as="hank") as client:
        resp = client.get("/api/admin/status")
        assert resp.status_code == 403
        assert "管理员" in resp.json()["detail"]

        # user-management spec：role=user 访问 /admin/users 返回 403（或无权限提示页）
        resp = client.get("/admin/users", follow_redirects=False)
        assert resp.status_code == 403


def test_admin_can_access_admin_routes(client_factory):
    """admin 登录后 /api/admin/status 与 /admin/users 正常返回（spec 场景）。"""
    with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
        resp = client.get("/api/admin/status")
        assert resp.status_code == 200
        assert "version" in resp.json()

        resp = client.get("/admin/users")
        assert resp.status_code == 200
