"""标签 API 集成测试（v0.03 技术方案 §9/§10/§36.1）。

页面渲染用例见 test_tags_page_renders（标签管理页任务完成后）。
"""

from __future__ import annotations

import pytest

from app.services.market_session_service import MarketStatus


class FakeNameProvider:
    def __init__(self):
        self.names = {
            ("CN", "STOCK", "600519"): "贵州茅台",
            ("CN", "STOCK", "601318"): "中国平安",
            ("CN", "ETF", "510300"): "沪深300ETF",
            ("HK", "STOCK", "00700"): "腾讯控股",
            ("CN", "INDEX", "000001"): "上证指数",
        }

    def get_name(self, market, asset_type, symbol):
        return self.names.get((market, asset_type, symbol))


class FakeRefreshService:
    def __init__(self):
        self.calls: list[list[str]] = []

    def refresh_instruments_now(self, instrument_ids):
        self.calls.append(list(instrument_ids))


class ClosedSessionService:
    def status(self, market, now=None):
        return MarketStatus.CLOSED


@pytest.fixture()
def client(client_factory):
    # multi-user-auth：业务 API 均要求登录，统一以 alice 视角访问
    # （AuthedClient 写请求自动注入 X-CSRF-Token，测试体调用形态不变）。
    with client_factory(FakeNameProvider(), login_as="alice") as c:
        c.app.state.refresh_service = FakeRefreshService()
        c.app.state.session_service = ClosedSessionService()
        yield c


# ---- 标签 CRUD ----


def test_tag_crud_flow(client):
    # 创建 201
    resp = client.post("/api/tags", json={"name": " 高股息 "})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "高股息"  # trim
    assert body["usage_count"] == 0

    # 列表含 usage_count
    resp = client.get("/api/tags")
    assert resp.status_code == 200
    assert resp.json()["items"] == [{"id": body["id"], "name": "高股息", "usage_count": 0}]

    # 改名 200
    resp = client.patch(f"/api/tags/{body['id']}", json={"name": "红利策略"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "红利策略"

    # 未使用可删除 204
    resp = client.delete(f"/api/tags/{body['id']}")
    assert resp.status_code == 204
    assert client.get("/api/tags").json()["items"] == []


def test_tag_create_validation(client):
    assert client.post("/api/tags", json={"name": "   "}).status_code == 422
    assert client.post("/api/tags", json={"name": "标" * 51}).status_code == 422
    assert client.post("/api/tags", json={"name": "科技"}).status_code == 201
    # 重复名称 409
    resp = client.post("/api/tags", json={"name": "科技"})
    assert resp.status_code == 409


def test_tag_missing_returns_404(client):
    assert client.patch("/api/tags/999", json={"name": "x"}).status_code == 404
    assert client.delete("/api/tags/999").status_code == 404


# ---- 自选条目标签关联（v0.03b 多对多）----


def _add_watchlist_item(client, symbol="600519", market="CN", asset_type="STOCK"):
    resp = client.post(
        "/api/watchlist", json={"symbol": symbol, "market": market, "asset_type": asset_type}
    )
    assert resp.status_code == 201
    return resp.json()["instrument_id"]


def _create_tag(client, name):
    resp = client.post("/api/tags", json={"name": name})
    assert resp.status_code == 201
    return resp.json()["id"]


def _set_tags(client, iid, tag_ids):
    return client.put(f"/api/watchlist/{iid}/tags", json={"tag_ids": tag_ids})


def test_set_multiple_tags_for_stock(client):
    iid = _add_watchlist_item(client)
    tag_a = _create_tag(client, "高股息")
    tag_b = _create_tag(client, "科技")

    resp = _set_tags(client, iid, [tag_a, tag_b])
    assert resp.status_code == 200
    assert resp.json()["tags"] == [
        {"id": tag_a, "name": "高股息"},
        {"id": tag_b, "name": "科技"},
    ]

    # 列表响应含 tags 数组
    items = client.get("/api/watchlist").json()["items"]
    assert items[0]["tags"] == [{"id": tag_a, "name": "高股息"}, {"id": tag_b, "name": "科技"}]


def test_set_tag_for_etf(client):
    iid = _add_watchlist_item(client, symbol="510300", asset_type="ETF")
    tag_id = _create_tag(client, "指数ETF")

    resp = _set_tags(client, iid, [tag_id])
    assert resp.status_code == 200
    assert resp.json()["tags"][0]["name"] == "指数ETF"


def test_set_tags_full_replacement(client):
    """全量替换语义：PUT 的数组即该条目的完整标签集合。"""
    iid = _add_watchlist_item(client)
    tag_a = _create_tag(client, "科技")
    tag_b = _create_tag(client, "消费")
    tag_c = _create_tag(client, "高股息")
    assert _set_tags(client, iid, [tag_a, tag_b]).status_code == 200

    resp = _set_tags(client, iid, [tag_b, tag_c])
    assert resp.status_code == 200
    assert {t["id"] for t in resp.json()["tags"]} == {tag_b, tag_c}


def test_unset_all_tags(client):
    iid = _add_watchlist_item(client)
    tag_id = _create_tag(client, "消费")
    assert _set_tags(client, iid, [tag_id]).status_code == 200

    resp = _set_tags(client, iid, [])
    assert resp.status_code == 200
    assert resp.json()["tags"] == []


def test_set_tags_deduplicates(client):
    iid = _add_watchlist_item(client)
    tag_id = _create_tag(client, "科技")

    resp = _set_tags(client, iid, [tag_id, tag_id])
    assert resp.status_code == 200
    assert len(resp.json()["tags"]) == 1


def test_index_cannot_set_tags(client):
    # 经指数配置 API 添加指数后尝试打标签 -> 400
    resp = client.post(
        "/api/index-watchlist", json={"symbol": "000001", "market": "CN", "asset_type": "INDEX"}
    )
    assert resp.status_code == 201
    iid = resp.json()["instrument_id"]
    tag_id = _create_tag(client, "消费")

    resp = _set_tags(client, iid, [tag_id])
    assert resp.status_code == 400
    assert "指数" in resp.json()["detail"]


def test_set_tags_missing_tag_or_entry(client):
    iid = _add_watchlist_item(client)
    # 标签不存在 404
    assert _set_tags(client, iid, [999]).status_code == 404
    # 自选条目不存在 404
    assert _set_tags(client, "CN:STOCK:999999", []).status_code == 404


def test_delete_in_use_tag_returns_409_with_count(client):
    iid = _add_watchlist_item(client)
    tag_id = _create_tag(client, "高股息")
    assert _set_tags(client, iid, [tag_id]).status_code == 200

    resp = client.delete(f"/api/tags/{tag_id}")
    assert resp.status_code == 409
    assert "1 个证券" in resp.json()["detail"]


def test_usage_count_counts_multiple_associations(client):
    """同一标签被多个条目引用 + 一个条目多标签时 usage_count 正确。"""
    iid_a = _add_watchlist_item(client, symbol="600519")
    iid_b = _add_watchlist_item(client, symbol="601318")
    tag_hi = _create_tag(client, "高股息")
    tag_tech = _create_tag(client, "科技")
    _set_tags(client, iid_a, [tag_hi, tag_tech])
    _set_tags(client, iid_b, [tag_hi])

    usage = {t["name"]: t["usage_count"] for t in client.get("/api/tags").json()["items"]}
    assert usage["高股息"] == 2
    assert usage["科技"] == 1


def test_usage_count_decreases_after_watchlist_delete(client):
    iid = _add_watchlist_item(client)
    tag_id = _create_tag(client, "高股息")
    _set_tags(client, iid, [tag_id])
    assert client.get("/api/tags").json()["items"][0]["usage_count"] == 1

    # 删除自选后关联级联清理，引用计数递减，标签变为可删除
    assert client.delete(f"/api/watchlist/{iid}").status_code == 204
    assert client.get("/api/tags").json()["items"][0]["usage_count"] == 0
    assert client.delete(f"/api/tags/{tag_id}").status_code == 204


def test_tags_page_renders(client):
    """标签管理页（/tags）渲染：表格与新增表单（v0.03 技术方案 §6）。"""
    resp = client.get("/tags")
    assert resp.status_code == 200
    body = resp.text
    assert "标签管理" in body
    assert "add-tag-form" in body
    assert "tags-table" in body
