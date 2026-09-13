"""行情标签筛选 API 集成测试（v0.03 技术方案 §12 / §36.2）。"""

from __future__ import annotations

import pytest

from app.services.market_session_service import MarketStatus


class FakeNameProvider:
    def __init__(self):
        self.names = {
            ("CN", "STOCK", "600519"): "贵州茅台",
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

    def all_status(self):
        return {"CN": MarketStatus.CLOSED, "HK": MarketStatus.CLOSED}


class SpyRegistry:
    """计数型假 Registry：验证筛选请求不触发任何 Provider 调用。"""

    def __init__(self):
        self.calls = 0

    def get_quotes(self, instruments):
        self.calls += 1
        return {}


@pytest.fixture()
def client(client_factory):
    with client_factory(FakeNameProvider()) as c:
        c.app.state.refresh_service = FakeRefreshService()
        c.app.state.session_service = ClosedSessionService()
        yield c


@pytest.fixture()
def seeded(client):
    """三个自选：600519(高股息+科技 双标签)、510300(无标签)、00700(科技)；一个指数。"""
    for symbol, market, asset_type in [
        ("600519", "CN", "STOCK"),
        ("510300", "CN", "ETF"),
        ("00700", "HK", "STOCK"),
    ]:
        resp = client.post(
            "/api/watchlist", json={"symbol": symbol, "market": market, "asset_type": asset_type}
        )
        assert resp.status_code == 201

    resp = client.post(
        "/api/index-watchlist",
        json={"symbol": "000001", "market": "CN", "asset_type": "INDEX"},
    )
    assert resp.status_code == 201

    tag_ids = {}
    for name in ("高股息", "科技"):
        resp = client.post("/api/tags", json={"name": name})
        assert resp.status_code == 201
        tag_ids[name] = resp.json()["id"]

    # 600519 同时关联两个标签（多对多）；00700 仅科技
    assert (
        client.put(
            "/api/watchlist/CN:STOCK:600519/tags",
            json={"tag_ids": [tag_ids["高股息"], tag_ids["科技"]]},
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/api/watchlist/HK:STOCK:00700/tags", json={"tag_ids": [tag_ids["科技"]]}
        ).status_code
        == 200
    )
    return tag_ids


def test_quotes_returns_all_with_tags_field(client, seeded):
    resp = client.get("/api/quotes")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3

    by_symbol = {item["symbol"]: item for item in items}
    assert by_symbol["600519"]["tags"] == [
        {"id": seeded["高股息"], "name": "高股息"},
        {"id": seeded["科技"], "name": "科技"},
    ]
    assert by_symbol["510300"]["tags"] == []
    assert by_symbol["00700"]["tags"] == [{"id": seeded["科技"], "name": "科技"}]


def test_quotes_filter_by_tag_hits_multi_tag_items(client, seeded):
    """筛选命中「包含该标签」的全部条目（含多标签条目）。"""
    resp = client.get(f"/api/quotes?tag_id={seeded['高股息']}")
    assert resp.status_code == 200
    assert [i["symbol"] for i in resp.json()["items"]] == ["600519"]

    # 科技：600519（双标签之一）与 00700（单标签）均命中
    resp = client.get(f"/api/quotes?tag_id={seeded['科技']}")
    assert {i["symbol"] for i in resp.json()["items"]} == {"600519", "00700"}


def test_quotes_filter_untagged(client, seeded):
    resp = client.get("/api/quotes?untagged=true")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [i["symbol"] for i in items] == ["510300"]
    assert items[0]["tags"] == []


def test_quotes_filter_params_mutually_exclusive(client, seeded):
    resp = client.get(f"/api/quotes?tag_id={seeded['高股息']}&untagged=true")
    assert resp.status_code == 422


def test_filtering_never_triggers_provider_calls(client, seeded):
    """切换筛选只读缓存，不产生任何 Provider 调用（技术方案 §12.2）。"""
    spy = SpyRegistry()
    client.app.state.refresh_service.quote_providers = spy

    client.get("/api/quotes")
    client.get(f"/api/quotes?tag_id={seeded['高股息']}")
    client.get("/api/quotes?untagged=true")
    client.get(f"/api/quotes?tag_id={seeded['科技']}")

    assert spy.calls == 0


def test_indices_unaffected_by_tag_feature(client, seeded):
    """/api/indices 条目不含 tag 字段，行为不变。"""
    resp = client.get("/api/indices")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert "tag" not in items[0]
