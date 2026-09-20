"""数据管理页面测试（a-share-historical-data，tasks 8.4）。

沿用现有管理员页面行为（spec admin-data-management"非管理员拒绝"）：
未登录 302 跳转登录页（空库跳 /setup）、普通用户 403、管理员 200；
页面须含 CSRF meta、主操作按钮与运行中禁用所需的 DOM 钩子，
并在服务端渲染时带上"运行中"所需元素（具体状态由 JS 从 API 拉取）。
"""

from __future__ import annotations

import pytest


class FakeNameProvider:
    def get_name(self, market, asset_type, symbol):
        return None


@pytest.fixture()
def admin_client(client_factory):
    with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
        yield client


def test_anonymous_redirects_to_setup_on_empty_db(client_factory):
    """空用户库匿名访问 /admin/data：302 跳转 /setup（与既有管理员页面一致）。"""
    client = client_factory(FakeNameProvider())
    resp = client.get("/admin/data", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/setup"


def test_anonymous_with_initialized_db_redirects_to_login(client_factory, user_factory):
    """已初始化库匿名访问：302 跳转 /login（不是 401——401 仅适用于 API）。"""
    user_factory("someone")
    client = client_factory(FakeNameProvider())
    resp = client.get("/admin/data", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_normal_user_returns_403(client_factory):
    with client_factory(FakeNameProvider(), login_as="alice") as client:
        resp = client.get("/admin/data")
        assert resp.status_code == 403
        assert "数据管理" not in resp.text, "非管理员不得看到页面内容"


def test_admin_page_renders(admin_client):
    resp = admin_client.get("/admin/data")
    assert resp.status_code == 200
    body = resp.text

    # 页面结构：总体卡 / 日级卡容器 / 主档表 / 当前任务 / 最近执行
    assert 'data-page="admin-data"' in body
    for anchor in (
        'id="overall-status"', 'id="history-start-date"', 'id="latest-trade-date"',
        'id="current-task"', 'id="last-run"', 'id="daily-cards"',
        'id="master-table"', 'id="active-run"', 'id="runs-table"',
    ):
        assert anchor in body, f"缺少页面元素 {anchor}"


def test_admin_page_has_single_primary_action(admin_client):
    """主操作是单一"检查并更新数据"按钮；不提供补缺口/增量/重同步模式选择。"""
    body = admin_client.get("/admin/data").text
    assert 'id="sync-button"' in body
    assert "检查并更新数据" in body
    for forbidden in ("补缺口", "增量同步", "重同步", "手动编辑"):
        assert forbidden not in body, f"页面不应出现 {forbidden}"
    # 不得提供历史数据手工编辑或水位输入入口（spec）
    assert "<input" not in body and "<textarea" not in body


def test_admin_page_includes_csrf_meta(admin_client):
    """CSRF meta 必须存在：前端 fetch 写请求依赖它（不许绕过 CSRF）。"""
    body = admin_client.get("/admin/data").text
    assert 'name="csrf-token"' in body


def test_admin_page_nav_present(admin_client):
    """页面加入现有管理员导航（用户管理/系统状态；本页自身不重复链接）。"""
    body = admin_client.get("/admin/data").text
    for href in ('href="/admin/users"', 'href="/admin/status"', 'href="/"'):
        assert href in body


@pytest.mark.parametrize(
    "path", ["/", "/watchlist", "/tags", "/change-password", "/admin/users", "/admin/status"]
)
def test_admin_nav_entry_added_everywhere(client_factory, path):
    """现有导航为各模板内联：管理员在每个页面都能看到数据管理入口。"""
    with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
        body = client.get(path).text
    assert 'href="/admin/data"' in body, f"{path} 缺少数据管理导航入口"


def test_admin_nav_entry_hidden_for_normal_user(client_factory):
    with client_factory(FakeNameProvider(), login_as="alice") as client:
        body = client.get("/").text
    assert 'href="/admin/data"' not in body


def test_page_js_loaded(admin_client):
    """页面加载现有原生 JS（无新前端构建系统）。"""
    body = admin_client.get("/admin/data").text
    assert "/static/app.js" in body
    assert "/static/style.css" in body
