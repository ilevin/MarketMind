"""数据隔离集成测试（multi-user-auth tasks 5.5 / user-data-isolation spec）。

A/B 双用户（附加 admin 用例）共享同一 DuckDB，经 client_factory 各自真实登录
（独立 app 实例 / Cookie / CSRF），覆盖用户私有数据边界：
- 自选 / 标签列表互不可见；多用户关注同一证券时 instrument 全局仅一份；
- 后台刷新聚合（SystemWatchlistRepository DISTINCT）含双方自选；
- 同名标签命名空间按用户隔离，usage_count 按用户统计；
- 跨用户 tag_id 的读 / 改 / 删 / 绑定均 404（不泄露存在性）；
- 排序互不影响；删除自选 / 删除标签不影响其他用户；
- admin 不因角色绕过隔离。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models.instrument import Instrument
from app.repositories.watchlist import SystemWatchlistRepository
from app.services.market_session_service import MarketStatus


class FakeNameProvider:
    """可配置的名称识别假件。"""

    def __init__(self):
        self.names = {
            ("CN", "STOCK", "600519"): "贵州茅台",
            ("HK", "STOCK", "00700"): "腾讯控股",
            ("CN", "ETF", "510300"): "沪深300ETF",
            ("CN", "INDEX", "000001"): "上证指数",
        }

    def get_name(self, market, asset_type, symbol):
        return self.names.get((market, asset_type, symbol))


class FakeRefreshService:
    """记录即时刷新调用的假件（避免添加自选触发真实外部行情请求）。"""

    def __init__(self):
        self.calls: list[list[str]] = []

    def refresh_instruments_now(self, instrument_ids):
        self.calls.append(list(instrument_ids))


class FakeFundamentalJob:
    """记录即时估值获取调用的假件。"""

    def __init__(self):
        self.calls: list[list[str]] = []

    def refresh_instruments(self, instrument_ids):
        self.calls.append(list(instrument_ids))


class ClosedSessionService:
    """市场恒为 CLOSED（添加路径本就不依赖市场状态，仅为与存量测试一致）。"""

    def status(self, market, now=None):
        return MarketStatus.CLOSED

    def should_refresh(self, market, now=None):
        return False


def _stub_external_services(client):
    """覆盖 lifespan 挂载的真实刷新 / 估值 / 市场状态服务，隔离外部请求。"""
    client.app.state.refresh_service = FakeRefreshService()
    client.app.state.fundamental_refresh = FakeFundamentalJob()
    client.app.state.session_service = ClosedSessionService()


@pytest.fixture()
def provider():
    return FakeNameProvider()


@pytest.fixture()
def alice(client_factory, provider):
    # 多用户共享同一 DuckDB：client_factory 每次调用建独立 app + 独立登录态
    with client_factory(provider, login_as="alice") as c:
        _stub_external_services(c)
        yield c


@pytest.fixture()
def bob(client_factory, provider):
    with client_factory(provider, login_as="bob") as c:
        _stub_external_services(c)
        yield c


@pytest.fixture()
def admin(client_factory, provider):
    with client_factory(provider, login_as="boss", role="admin") as c:
        _stub_external_services(c)
        yield c


# ---- 复用的小工具（调用形态对齐 test_tags_api.py）----


def _add(client, symbol, market="CN", asset_type="STOCK") -> str:
    resp = client.post(
        "/api/watchlist", json={"symbol": symbol, "market": market, "asset_type": asset_type}
    )
    assert resp.status_code == 201
    return resp.json()["instrument_id"]


def _create_tag(client, name) -> int:
    resp = client.post("/api/tags", json={"name": name})
    assert resp.status_code == 201
    return resp.json()["id"]


def _set_tags(client, instrument_id, tag_ids):
    return client.put(f"/api/watchlist/{instrument_id}/tags", json={"tag_ids": tag_ids})


def _watchlist_items(client) -> list[dict]:
    return client.get("/api/watchlist").json()["items"]


def _tag_items(client) -> list[dict]:
    return client.get("/api/tags").json()["items"]


# ---- 1. 列表互不可见 ----


def test_watchlist_invisible_across_users(alice, bob):
    """A 添加自选后 B 的列表为空；双方各自添加后互不可见对方数据。"""
    iid_a = _add(alice, "600519")
    assert iid_a == "CN:STOCK:600519"

    # B 的列表为空，不含 A 的证券
    assert _watchlist_items(bob) == []

    iid_b = _add(bob, "00700", market="HK")
    assert [i["instrument_id"] for i in _watchlist_items(alice)] == [iid_a]
    assert [i["instrument_id"] for i in _watchlist_items(bob)] == [iid_b]


# ---- 2. 同时关注同一证券 + 全局共享 + 刷新聚合 ----


def test_same_instrument_shared_by_both_users(alice, bob, session_factory):
    """A、B 同时关注同一证券：均 201、各自列表各一行；instrument 全局一份。"""
    iid = "CN:STOCK:600519"
    assert _add(alice, "600519") == iid
    assert _add(bob, "600519") == iid  # 同一证券双方均成功，互不冲突

    # 各自列表各有一行
    assert [i["instrument_id"] for i in _watchlist_items(alice)] == [iid]
    assert [i["instrument_id"] for i in _watchlist_items(bob)] == [iid]

    # instrument 主数据全局共享：多用户复用同一行（user-data-isolation spec）
    with session_factory() as s:
        count = s.scalar(
            select(func.count()).select_from(Instrument).where(Instrument.instrument_id == iid)
        )
        assert count == 1

    # B 另配置一条指数：后台刷新聚合（System 作用域 DISTINCT）含双方、两类列表
    resp = bob.post(
        "/api/index-watchlist", json={"symbol": "000001", "market": "CN", "asset_type": "INDEX"}
    )
    assert resp.status_code == 201
    with session_factory() as s:
        ids = SystemWatchlistRepository(s).all_instrument_ids()
    # 双方关注的同一证券仅聚合一次（UNION 去重），指数配置同样计入
    assert ids.count(iid) == 1
    assert set(ids) == {iid, "CN:INDEX:000001"}


# ---- 3. 同名标签命名空间隔离 ----


def test_same_name_tags_independent_namespaces(alice, bob):
    """A、B 建同名标签均成功：id 不同、列表互不可见、usage_count 按用户统计。"""
    iid = "CN:STOCK:600519"
    _add(alice, "600519")
    _add(bob, "600519")

    tag_a = _create_tag(alice, "科技")
    tag_b = _create_tag(bob, "科技")
    assert tag_a != tag_b

    # 各自标签列表互不可见（仅含自己的 id）
    assert _tag_items(alice) == [{"id": tag_a, "name": "科技", "usage_count": 0}]
    assert _tag_items(bob) == [{"id": tag_b, "name": "科技", "usage_count": 0}]

    # 双方各自绑定到自己的同证券条目：关联计数按用户计算（各为 1，而非 2）
    assert _set_tags(alice, iid, [tag_a]).status_code == 200
    assert _set_tags(bob, iid, [tag_b]).status_code == 200
    assert _tag_items(alice)[0]["usage_count"] == 1
    assert _tag_items(bob)[0]["usage_count"] == 1


# ---- 4. 跨用户 tag_id 访问 ----


def test_cross_user_tag_access_returns_404(alice, bob):
    """A 读 / 改 / 删 / 绑定 B 的 tag 均视同不存在返回 404，B 的标签不受影响。"""
    tag_b = _create_tag(bob, "港股")

    # A 用 B 的 tag_id 读改删：与不存在的 tag 行为一致（不泄露存在性）
    assert alice.patch(f"/api/tags/{tag_b}", json={"name": "改名"}).status_code == 404
    assert alice.delete(f"/api/tags/{tag_b}").status_code == 404

    # B 的标签不受影响：仍存在且名称未变
    assert _tag_items(bob) == [{"id": tag_b, "name": "港股", "usage_count": 0}]

    # A 用 B 的 tag_id 绑定自己的自选条目 -> 404，不创建关联
    iid = _add(alice, "600519")
    assert _set_tags(alice, iid, [tag_b]).status_code == 404
    item = [i for i in _watchlist_items(alice) if i["instrument_id"] == iid][0]
    assert item["tags"] == []

    # B 的标签引用计数不受影响
    assert _tag_items(bob)[0]["usage_count"] == 0


# ---- 5. 排序互不影响 ----


def test_reorder_isolated_per_user(alice, bob):
    """A 调整自己的自选排序后，B 的顺序与 sort_order 均不变。"""
    iid_a, iid_b = "CN:STOCK:600519", "HK:STOCK:00700"
    for client in (alice, bob):
        _add(client, "600519")
        _add(client, "00700", market="HK")

    # 初始双方顺序一致（首条 sort_order=10，次条=20）
    for client in (alice, bob):
        assert [(i["instrument_id"], i["sort_order"]) for i in _watchlist_items(client)] == [
            (iid_a, 10),
            (iid_b, 20),
        ]

    # A 调整排序
    resp = alice.put(
        "/api/watchlist/order",
        json={"items": [
            {"instrument_id": iid_b, "sort_order": 5},
            {"instrument_id": iid_a, "sort_order": 15},
        ]},
    )
    assert resp.status_code == 200

    # A 按新顺序返回；B 的排序与 sort_order 完全不变
    assert [i["instrument_id"] for i in _watchlist_items(alice)] == [iid_b, iid_a]
    assert [(i["instrument_id"], i["sort_order"]) for i in _watchlist_items(bob)] == [
        (iid_a, 10),
        (iid_b, 20),
    ]


# ---- 6. 删除自选不影响其他用户 ----


def test_delete_watchlist_keeps_other_user_data(alice, bob, session_factory):
    """A 删除自选不影响 B 的同证券条目、标签关联与 instrument 全局数据。"""
    iid = "CN:STOCK:600519"
    _add(alice, "600519")
    _add(bob, "600519")
    tag_a = _create_tag(alice, "科技")
    tag_b = _create_tag(bob, "科技")
    assert _set_tags(alice, iid, [tag_a]).status_code == 200
    assert _set_tags(bob, iid, [tag_b]).status_code == 200

    # A 删除自选
    assert alice.delete(f"/api/watchlist/{iid}").status_code == 204
    assert _watchlist_items(alice) == []

    # B 的同证券条目与标签关联保留，写路径仍可用
    assert [
        (i["instrument_id"], i["tags"]) for i in _watchlist_items(bob)
    ] == [(iid, [{"id": tag_b, "name": "科技"}])]
    assert _set_tags(bob, iid, [tag_b]).status_code == 200

    # instrument 全局数据保留（B 仍关注该证券）
    with session_factory() as s:
        assert s.get(Instrument, iid) is not None

    # A 的标签关联随删除一并清理（usage_count 归零），B 的计数不变
    assert _tag_items(alice) == [{"id": tag_a, "name": "科技", "usage_count": 0}]
    assert _tag_items(bob)[0]["usage_count"] == 1


# ---- 7. admin 同样被隔离 ----


def test_admin_data_isolated_from_regular_users(alice, bob, admin):
    """admin 不因角色获得读取他人私有数据的能力，个人数据与普通用户同隔离。"""
    iid_a = _add(alice, "600519")
    tag_a = _create_tag(alice, "科技")
    iid_b = _add(bob, "00700", market="HK")
    tag_b = _create_tag(bob, "港股")

    # admin 的列表不含 A/B 数据
    assert _watchlist_items(admin) == []
    assert _tag_items(admin) == []

    # admin 以 A/B 的 tag_id 读改删：不因 role=admin 放行
    assert admin.patch(f"/api/tags/{tag_a}", json={"name": "x"}).status_code == 404
    assert admin.delete(f"/api/tags/{tag_b}").status_code == 404
    assert _tag_items(alice) == [{"id": tag_a, "name": "科技", "usage_count": 0}]
    assert _tag_items(bob) == [{"id": tag_b, "name": "港股", "usage_count": 0}]

    # admin 自己的私有数据走相同的 user_id 过滤：A/B 互不可见
    iid_m = _add(admin, "510300", asset_type="ETF")
    assert [i["instrument_id"] for i in _watchlist_items(admin)] == [iid_m]
    assert [i["instrument_id"] for i in _watchlist_items(alice)] == [iid_a]
    assert [i["instrument_id"] for i in _watchlist_items(bob)] == [iid_b]


# ---- 8. 删除标签不影响他人同名标签 ----


def test_delete_tag_keeps_other_user_same_name_tag(alice, bob):
    """B 删除自己的同名标签不影响 A 的同名标签及其关联。"""
    iid = "CN:STOCK:600519"
    _add(alice, "600519")
    tag_a = _create_tag(alice, "科技")
    assert _set_tags(alice, iid, [tag_a]).status_code == 200

    tag_b = _create_tag(bob, "科技")  # B 的同名标签未被引用，可删除

    # B 删除自己的标签
    assert bob.delete(f"/api/tags/{tag_b}").status_code == 204
    assert _tag_items(bob) == []

    # A 的同名标签不受影响：仍存在、关联保留、计数不变
    assert _tag_items(alice) == [{"id": tag_a, "name": "科技", "usage_count": 1}]
    item = [i for i in _watchlist_items(alice) if i["instrument_id"] == iid][0]
    assert item["tags"] == [{"id": tag_a, "name": "科技"}]
