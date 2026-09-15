"""认证权限三态集成测试（multi-user-auth tasks 4.2）。

覆盖 rest-api spec「管理与健康接口」与 user-authentication spec
「认证与授权 Dependency」的权限要求：

- ``/api/admin/*``（app/api/admin.py 与 app/api/status.py 两个 Router 均在
  Router 层统一声明 ``require_admin``）：匿名 401、普通用户 403、admin 200；
  普通用户 403 时 SHALL NOT 触发刷新等副作用；
- ``/health`` 匿名可访问（不要求登录，只查应用与数据库，返回版本）；
- 业务 API（``/api/watchlist`` 等）匿名一律 401。

注：app/api/status.py 挂载于 ``/api/admin`` 前缀（路由即 ``/api/admin/status``），
不存在独立的 ``/api/status`` 路由，其权限三态由下方 /api/admin/status 用例覆盖。
"""

from __future__ import annotations

import pytest

from app.schemas import RefreshResult
from app.version import APP_VERSION


class FakeNameProvider:
    """名称识别假件：权限测试不依赖真实标的名称。"""

    def get_name(self, market, asset_type, symbol):
        return None


class FakeRefreshService:
    """记录 refresh_all 调用的假件：验证无权限请求不触发刷新。"""

    def __init__(self):
        self.calls: list[bool] = []

    def refresh_all(self, force: bool = False):
        self.calls.append(force)
        return RefreshResult(success=True, updated=0, failed=0)


# ---- /api/admin/status 三态 ----


def test_admin_status_anonymous_returns_401(client_factory):
    """匿名（无 Session Cookie）访问 /api/admin/status 返回 401。"""
    client = client_factory(FakeNameProvider())
    resp = client.get("/api/admin/status")
    assert resp.status_code == 401


def test_admin_status_normal_user_returns_403(client_factory):
    """普通登录用户（role=user）访问 /api/admin/status 返回 403。"""
    with client_factory(FakeNameProvider(), login_as="alice") as client:
        resp = client.get("/api/admin/status")
        assert resp.status_code == 403


def test_admin_status_admin_returns_200(client_factory):
    """admin 登录用户访问 /api/admin/status 正常返回版本 / Job / Provider 状态。"""
    with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
        resp = client.get("/api/admin/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == APP_VERSION
        assert set(body["jobs"]) == {"quote_refresh", "fundamental_refresh"}
        assert set(body["providers"]) == {"tencent", "akshare", "tushare"}


# ---- 其余 /api/admin/* 接口同样受 Router 层 require_admin 保护 ----


def test_admin_refresh_quotes_anonymous_401_and_user_403_without_refresh(client_factory):
    """POST /api/admin/refresh/quotes：匿名 401；普通用户 403 且不触发刷新。

    匿名写请求无 Cookie，CSRF 中间件放行、由认证依赖返回 401。
    """
    anon = client_factory(FakeNameProvider())
    assert anon.post("/api/admin/refresh/quotes").status_code == 401

    with client_factory(FakeNameProvider(), login_as="alice") as client:
        fake_refresh = FakeRefreshService()
        client.app.state.refresh_service = fake_refresh
        resp = client.post("/api/admin/refresh/quotes")
        assert resp.status_code == 403
        assert fake_refresh.calls == []


# ---- /health 匿名可访问 ----


def test_health_anonymous_returns_200(client_factory):
    """匿名 GET /health：不要求登录，只查应用与数据库，响应含版本。"""
    client = client_factory(FakeNameProvider())
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["version"] == APP_VERSION


# ---- 业务 API 要求登录 ----


@pytest.mark.parametrize(
    "path",
    ["/api/watchlist", "/api/index-watchlist", "/api/tags", "/api/quotes", "/api/auth/me"],
)
def test_business_api_anonymous_returns_401(client_factory, path):
    """匿名访问业务 API 一律 401（认证依赖在业务逻辑之前短路）。"""
    client = client_factory(FakeNameProvider())
    resp = client.get(path)
    assert resp.status_code == 401


# ---- /admin/status 系统状态页（dashboard-ui spec：管理员导航「系统状态」入口） ----


def test_admin_status_page_anonymous_redirects_to_setup(client_factory):
    """空用户库匿名访问 /admin/status：302 跳转 /setup。"""
    client = client_factory(FakeNameProvider())
    resp = client.get("/admin/status", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/setup"


def test_admin_status_page_normal_user_returns_403(client_factory):
    """普通用户访问 /admin/status 返回 403。"""
    with client_factory(FakeNameProvider(), login_as="alice") as client:
        resp = client.get("/admin/status")
        assert resp.status_code == 403


def test_admin_status_page_admin_renders(client_factory):
    """admin 访问 /admin/status：服务端渲染后台任务与数据源指标表格。"""
    with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
        resp = client.get("/admin/status")
        assert resp.status_code == 200
        body = resp.text
        assert "quote_refresh" in body and "fundamental_refresh" in body
        assert "tencent" in body and "akshare" in body and "tushare" in body


# ---- FastAPI 默认文档端点已关闭（匿名可达仅 /login /health /static/*） ----


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_endpoints_disabled(client_factory, path):
    """docs / redoc / openapi.json 不存在（404），不向匿名暴露 API 面。"""
    client = client_factory(FakeNameProvider())
    resp = client.get(path)
    assert resp.status_code == 404
