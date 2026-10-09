"""全站两级导航集成测试（optimize-navigation，site-navigation spec）。

覆盖：
- 两级导航结构：顶栏主导航三分区（行情首页/数据管理/系统设置）+ 分区子导航 + 用户菜单；
- 当前位置标识：主导航分区项与子导航条目的 active / aria-current；
- 角色可见性：数据管理 / 系统设置分区（含子导航条目）仅管理员可见；
- 无分区页面（修改密码）：顶栏渲染但主导航无 active、不渲染子导航栏；
- 匿名页（/login、/setup）无导航；
- 跨页面导航一致性（『导航渲染一致性』）；
- ETF 占位页（admin-data-management spec）：管理员 200 + 「敬请期待」，
  普通用户 403、未登录按初始化状态 302 跳转（空库跳 /setup）。
"""

from __future__ import annotations

import re

import pytest


class FakeNameProvider:
    """名称识别假件：导航测试不依赖真实标的名称。"""

    def get_name(self, market, asset_type, symbol):
        return None


# 主导航三分区：条目文本 -> href（数据管理 / 系统设置仅管理员可见）
MAIN_NAV = (("行情首页", "/"), ("数据管理", "/admin/data"), ("系统设置", "/admin/users"))
# 各分区子导航条目：文本 -> href
SUBNAV = {
    "market": (("行情", "/"), ("自选管理", "/watchlist"), ("标签管理", "/tags")),
    "data": (
        ("股票数据", "/admin/data"),
        ("个股历史", "/admin/data/stocks"),
        ("ETF数据", "/admin/data/etf"),
        ("ETF历史", "/admin/data/etf/history"),
    ),
    "setting": (("用户管理", "/admin/users"), ("系统状态", "/admin/status")),
}


def _active_links(body: str, css_class: str) -> list[tuple[str, str]]:
    """提取处于 active 状态的导航链接 (href, 文本)。"""
    return re.findall(
        rf'<a class="{css_class} active"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', body
    )


def _admin_client(client_factory):
    return client_factory(FakeNameProvider(), login_as="admin", role="admin")


def _hrefs(fragment: str) -> list[str]:
    return re.findall(r'href="([^"]+)"', fragment)


# ---- 两级导航结构（『两级导航结构』『主导航分区与子导航条目』） ----


@pytest.mark.parametrize(
    "path,section",
    [
        ("/", "market"),
        ("/watchlist", "market"),
        ("/tags", "market"),
        ("/admin/data", "data"),
        ("/admin/data/stocks", "data"),
        ("/admin/data/etf", "data"),
        ("/admin/data/etf/history", "data"),
        ("/admin/users", "setting"),
        ("/admin/status", "setting"),
    ],
)
def test_admin_two_level_nav_structure(client_factory, path, section):
    """管理员在各分区页面看到主导航三分区与当前分区的完整子导航。"""
    with _admin_client(client_factory) as client:
        body = client.get(path).text

    nav_main = re.search(r'<nav class="nav-main">(.*?)</nav>', body, re.S)
    assert nav_main, f"{path} 缺少主导航"
    for text, href in MAIN_NAV:
        assert href in _hrefs(nav_main.group(1)), f"{path} 主导航缺少 {text}({href})"

    subbar = re.search(r'<div class="subbar">(.*?)</div>', body, re.S)
    assert subbar, f"{path} 缺少子导航栏"
    for text, href in SUBNAV[section]:
        assert href in _hrefs(subbar.group(1)), f"{path} 子导航缺少 {text}({href})"


def test_nav_consistent_across_pages(client_factory):
    """跨页面导航一致：管理员在不同页面主导航完全一致，仅 active 不同。"""
    with _admin_client(client_factory) as client:
        navs = [
            _hrefs(re.search(r'<nav class="nav-main">(.*?)</nav>', client.get(p).text, re.S).group(1))
            for p in ("/", "/admin/data", "/admin/users")
        ]
    assert navs[0] == navs[1] == navs[2] == ["/", "/admin/data", "/admin/users"]


# ---- 当前位置标识（『当前位置标识』） ----


@pytest.mark.parametrize(
    "path,main_href,main_text,sub_href,sub_text",
    [
        ("/", "/", "行情首页", "/", "行情"),
        ("/watchlist", "/", "行情首页", "/watchlist", "自选管理"),
        ("/tags", "/", "行情首页", "/tags", "标签管理"),
        ("/admin/data", "/admin/data", "数据管理", "/admin/data", "股票数据"),
        ("/admin/data/stocks", "/admin/data", "数据管理", "/admin/data/stocks", "个股历史"),
        ("/admin/data/etf", "/admin/data", "数据管理", "/admin/data/etf", "ETF数据"),
        ("/admin/data/etf/history", "/admin/data", "数据管理", "/admin/data/etf/history", "ETF历史"),
        ("/admin/users", "/admin/users", "系统设置", "/admin/users", "用户管理"),
        ("/admin/status", "/admin/users", "系统设置", "/admin/status", "系统状态"),
    ],
)
def test_active_state(client_factory, path, main_href, main_text, sub_href, sub_text):
    """当前页面：主导航分区项与子导航条目各恰好一个 active 且携带 aria-current。"""
    with _admin_client(client_factory) as client:
        body = client.get(path).text

    assert _active_links(body, "nav-item") == [(main_href, main_text)]
    assert _active_links(body, "sub-item") == [(sub_href, sub_text)]
    assert body.count('aria-current="page"') == 2


def test_change_password_page_has_no_subnav(client_factory):
    """无分区页面：顶栏渲染但主导航无 active、不渲染子导航栏。"""
    with client_factory(FakeNameProvider(), login_as="alice") as client:
        body = client.get("/change-password").text

    assert '<div class="subbar">' not in body
    assert _active_links(body, "nav-item") == []
    assert 'id="change-password-form"' in body


# ---- 角色可见性（『主导航分区与子导航条目』）----


def test_normal_user_nav_without_admin_sections(client_factory):
    """普通用户：主导航仅「行情首页」，无任何 /admin/* 入口；行情子导航仍可用。"""
    with client_factory(FakeNameProvider(), login_as="alice") as client:
        body = client.get("/watchlist").text

    nav_main = re.search(r'<nav class="nav-main">(.*?)</nav>', body, re.S).group(1)
    assert _hrefs(nav_main) == ["/"], "普通用户主导航仅「行情首页」"
    assert "数据管理" not in nav_main and "系统设置" not in nav_main

    subbar = re.search(r'<div class="subbar">(.*?)</div>', body, re.S).group(1)
    for text, href in SUBNAV["market"]:
        assert href in _hrefs(subbar)


def test_normal_user_no_admin_hrefs_anywhere(client_factory):
    """普通用户页面上不存在任何 /admin/* 链接。"""
    with client_factory(FakeNameProvider(), login_as="alice") as client:
        for path in ("/", "/watchlist", "/tags", "/change-password"):
            body = client.get(path).text
            assert not re.search(r'href="/admin/', body), f"{path} 泄露管理入口"


# ---- 用户菜单（『用户菜单』）----


def test_user_menu_admin_vs_normal(client_factory):
    """用户菜单：按钮显示用户名（管理员附加「管理员」标注），下拉含修改密码/退出登录。"""
    with _admin_client(client_factory) as client:
        admin_body = client.get("/").text
    with client_factory(FakeNameProvider(), login_as="alice") as client:
        user_body = client.get("/").text

    for body, username in ((admin_body, "admin"), (user_body, "alice")):
        assert username in body
        assert 'href="/change-password"' in body
        assert 'id="logout-link"' in body
        assert "修改密码" in body and "退出登录" in body
    assert "（管理员）" in admin_body
    assert "（管理员）" not in user_body


# ---- 匿名页无导航（『两级导航结构』：/login、/setup 不渲染导航） ----


def test_login_page_has_no_nav(client_factory, user_factory):
    user_factory("someone")  # 已初始化的库 /login 才返回 200
    with client_factory(FakeNameProvider()) as client:
        body = client.get("/login").text
    assert 'class="topbar"' not in body
    assert '<div class="subbar">' not in body


def test_setup_page_has_no_nav(client_factory):
    with client_factory(FakeNameProvider()) as client:  # 空库 /setup 直接 200
        body = client.get("/setup").text
    assert 'class="topbar"' not in body
    assert '<div class="subbar">' not in body


# ---- ETF 数据页（etf-data-module，tasks 8.5；原占位页测试改写为真实页面） ----


def test_etf_overview_page_renders(client_factory):
    """管理员访问 /admin/data/etf：200、总览壳完整（数据由 JS 拉 summary 填充）。"""
    with _admin_client(client_factory) as client:
        resp = client.get("/admin/data/etf")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-page="admin-data-etf"' in body
    for anchor in (
        'id="overall-status"',          # 整体状态徽章
        'id="etf-active-count"',        # universe 概况：活跃 ETF
        'id="etf-total-count"',         # universe 概况：全部 ETF
        'id="etf-universe-refreshed"',  # universe 最近刷新
        'id="history-start-date"',
        'id="sync-button"',             # 手动同步（复用 POST /sync）
        'id="etf-cards"',               # etf_basic 主档卡 + 两个日级卡容器
        'id="active-run"',              # 当前任务进度
        'id="etf-disabled-panel"',      # 未启用说明态
    ):
        assert anchor in body, f"缺少页面元素 {anchor}"
    assert "敬请期待" not in body, "占位文案应已移除"
    # 总览页无任何数据编辑入口（spec：只读展示 + 单一同步按钮）
    assert "<input" not in body and "<textarea" not in body


def test_etf_history_page_renders(client_factory):
    """管理员访问 /admin/data/etf/history：200、结构复刻个股历史页。"""
    with _admin_client(client_factory) as client:
        resp = client.get("/admin/data/etf/history")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-page="admin-data-etf-history"' in body
    for anchor in (
        'id="dataset-chips"',   # 数据集 chip（etf_daily/etf_adj_factor，JS 渲染）
        'id="status-filter"',
        'id="search-input"',
        'id="stocks-table"',
        'id="prev-page"',
        'id="next-page"',
        'id="page-info"',
        'id="error-modal"',
    ):
        assert anchor in body, f"缺少页面元素 {anchor}"
    assert 'id="error-modal-title"' in body
    assert "没有匹配的 ETF" in body, "空状态文案应为 ETF 口径"


def test_etf_pages_normal_user_403(client_factory):
    with client_factory(FakeNameProvider(), login_as="alice") as client:
        for path in ("/admin/data/etf", "/admin/data/etf/history"):
            assert client.get(path).status_code == 403, path


def test_etf_pages_anonymous_redirects_to_login(client_factory, user_factory):
    user_factory("someone")
    with client_factory(FakeNameProvider()) as client:
        for path in ("/admin/data/etf", "/admin/data/etf/history"):
            resp = client.get(path, follow_redirects=False)
            assert resp.status_code == 302, path
            assert resp.headers["location"] == "/login"


def test_etf_pages_anonymous_empty_db_redirects_to_setup(client_factory):
    with client_factory(FakeNameProvider()) as client:
        for path in ("/admin/data/etf", "/admin/data/etf/history"):
            resp = client.get(path, follow_redirects=False)
            assert resp.status_code == 302, path
            assert resp.headers["location"] == "/setup"
