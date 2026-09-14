"""CSRF 防护集成测试（multi-user-auth tasks 9.3）。

覆盖 user-authentication spec「CSRF 防护」Requirement：
- 登录态写请求缺失 X-CSRF-Token → 403（detail 含 CSRF，不执行业务逻辑）；
- X-CSRF-Token 与当前 Session 绑定的 Token 不一致 → 403；
- 携带正确 Token（从 user_session 表取当前 Session 的 csrf_token）→ 业务正常 201；
- GET 安全方法不校验；
- /api/auth/login 豁免（登录前无 Session）；
- 匿名写请求（无 Cookie）不触发 CSRF，由认证依赖返回 401（认证优先语义）；
- 重新登录换新 Session 后，旧 CSRF Token 对当前 Session 失效 → 403。

"有 Cookie 但不带 CSRF 头"的场景统一用裸 TestClient 手动 POST /api/auth/login
取 Cookie 再显式控制请求头（AuthedClient 会自动注入 X-CSRF-Token，不适用）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth.session import SESSION_COOKIE_NAME, hash_token
from app.models import UserSession

ADD_PAYLOAD = {"symbol": "600519", "market": "CN", "asset_type": "STOCK"}


class FakeNameProvider:
    """可识别 600519 的名称假件（POST /api/watchlist 成功路径需要）。"""

    def get_name(self, market, asset_type, symbol):
        return {("CN", "STOCK", "600519"): "贵州茅台"}.get((market, asset_type, symbol))


class NullRefreshService:
    """空刷新假件：添加自选不触发真实行情 / 估值 Provider 调用。"""

    def refresh_instruments_now(self, instrument_ids):
        pass

    def refresh_instruments(self, instrument_ids):
        pass


@pytest.fixture()
def client(client_factory):
    """裸（匿名）TestClient：登录与 CSRF 请求头全部由测试体手动控制。"""
    with client_factory(FakeNameProvider()) as c:
        c.app.state.refresh_service = NullRefreshService()
        c.app.state.fundamental_refresh = NullRefreshService()
        yield c


def _login(client, username: str) -> str:
    """手动登录并返回写入 Cookie jar 的 Session Token（登录接口豁免 CSRF）。"""
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": "password123"}
    )
    assert resp.status_code == 200, f"测试登录失败: {resp.status_code} {resp.text}"
    token = client.cookies.get(SESSION_COOKIE_NAME)
    assert token
    return token


def _csrf_of(session_factory, cookie_token: str) -> str:
    """从 user_session 表取该 Cookie 对应 Session 的 csrf_token（主键精确匹配）。"""
    with session_factory() as s:
        return s.execute(
            select(UserSession.csrf_token).where(
                UserSession.session_token_hash == hash_token(cookie_token)
            )
        ).scalar_one()


def _watchlist_ids(client) -> list[str]:
    return [i["instrument_id"] for i in client.get("/api/watchlist").json()["items"]]


# ---------------------------------------------------------------------------
# 缺失 / 错误 / 正确 Token
# ---------------------------------------------------------------------------


def test_write_without_csrf_header_rejected(client, user_factory):
    """登录态写请求缺 X-CSRF-Token → 403，且业务逻辑未执行（spec：缺失 Token）。"""
    user_factory("alice")
    _login(client, "alice")

    resp = client.post("/api/watchlist", json=ADD_PAYLOAD)
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]

    # 未执行业务逻辑：自选列表仍为空
    assert _watchlist_ids(client) == []


def test_write_with_wrong_csrf_token_rejected(client, user_factory):
    """携带错误的 X-CSRF-Token → 403（spec：Token 错误）。"""
    user_factory("bravo")
    _login(client, "bravo")

    resp = client.post(
        "/api/watchlist", json=ADD_PAYLOAD, headers={"X-CSRF-Token": "forged-token-123"}
    )
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]
    assert _watchlist_ids(client) == []


def test_write_with_valid_csrf_token_accepted(client, user_factory, session_factory):
    """携带 user_session 表中当前 Session 的 csrf_token → 正常处理（spec：正确 Token）。"""
    user_factory("carol")
    cookie_token = _login(client, "carol")
    csrf_token = _csrf_of(session_factory, cookie_token)

    resp = client.post(
        "/api/watchlist", json=ADD_PAYLOAD, headers={"X-CSRF-Token": csrf_token}
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "贵州茅台"
    # 业务真实生效
    assert _watchlist_ids(client) == ["CN:STOCK:600519"]


# ---------------------------------------------------------------------------
# 安全方法与豁免路径
# ---------------------------------------------------------------------------


def test_get_requests_do_not_require_csrf_token(client, user_factory):
    """GET 安全方法不校验：登录态不带 X-CSRF-Token 仍正常返回（spec 场景）。"""
    user_factory("dave")
    _login(client, "dave")

    resp = client.get("/api/watchlist")
    assert resp.status_code == 200
    assert resp.json()["items"] == []

    # /api/auth/me 同为 GET：不带 Token 正常解析当前用户
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "dave"


def test_login_endpoint_exempt_from_csrf(client, user_factory):
    """登录接口豁免：不带 X-CSRF-Token，正确凭据 200、错误凭据 401（均非 403）。"""
    user_factory("erin")

    ok = client.post(
        "/api/auth/login", json={"username": "erin", "password": "password123"}
    )
    assert ok.status_code == 200

    bad = client.post(
        "/api/auth/login", json={"username": "erin", "password": "totally-wrong-9"}
    )
    assert bad.status_code == 401


# ---------------------------------------------------------------------------
# 匿名写请求与 Session 轮换
# ---------------------------------------------------------------------------


def test_anonymous_write_gets_401_not_403(client):
    """匿名写请求（无 Cookie）不触发 CSRF：由认证依赖返回 401（认证优先语义）。"""
    resp = client.post("/api/watchlist", json=ADD_PAYLOAD)
    assert resp.status_code == 401

    # 携带无法解析的 Session Cookie（伪造 / 已失效 Token）同样交由认证依赖 → 401
    resp = client.post(
        "/api/watchlist",
        json=ADD_PAYLOAD,
        headers={"Cookie": f"{SESSION_COOKIE_NAME}=forged-session-token"},
    )
    assert resp.status_code == 401


def test_csrf_token_invalidated_by_relogin(client, user_factory, session_factory):
    """重新登录换新 Session：旧 CSRF Token 对当前 Session 失效 → 403。"""
    user_factory("frank")
    first_cookie = _login(client, "frank")
    old_csrf = _csrf_of(session_factory, first_cookie)

    # 再次登录：Cookie 被新 Session 覆盖，csrf_token 随之换新
    second_cookie = _login(client, "frank")
    assert second_cookie != first_cookie
    new_csrf = _csrf_of(session_factory, second_cookie)
    assert new_csrf != old_csrf

    # 旧 Token 配当前 Cookie（如停留在旧页面的前端）→ 403
    resp = client.post(
        "/api/watchlist", json=ADD_PAYLOAD, headers={"X-CSRF-Token": old_csrf}
    )
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]

    # 新 Session 的 Token 正常
    resp = client.post(
        "/api/watchlist", json=ADD_PAYLOAD, headers={"X-CSRF-Token": new_csrf}
    )
    assert resp.status_code == 201
