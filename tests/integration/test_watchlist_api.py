"""Watchlist / Index Watchlist API 集成测试（DuckDB 临时文件库 + 假名称 Provider）。"""

from __future__ import annotations

import pytest

from app.services.market_session_service import MarketStatus


class FakeNameProvider:
    """可配置的名称识别假件。"""

    def __init__(self):
        self.names = {
            ("CN", "STOCK", "600519"): "贵州茅台",
            ("CN", "ETF", "510300"): "沪深300ETF",
            ("HK", "STOCK", "00700"): "腾讯控股",
            ("CN", "INDEX", "000001"): "上证指数",
            ("CN", "INDEX", "000300"): "沪深300",
            ("HK", "INDEX", "HSI"): "恒生指数",
        }

    def get_name(self, market, asset_type, symbol):
        return self.names.get((market, asset_type, symbol))


class FakeRefreshService:
    """记录即时刷新调用的假件；exc 非 None 时模拟 Provider 故障。"""

    def __init__(self, exc: Exception | None = None):
        self.calls: list[list[str]] = []
        self.exc = exc

    def refresh_instruments_now(self, instrument_ids):
        self.calls.append(list(instrument_ids))
        if self.exc:
            raise self.exc


class ClosedSessionService:
    """市场恒为 CLOSED，用于验证添加后刷新不依赖市场状态。"""

    def status(self, market, now=None):
        return MarketStatus.CLOSED

    def should_refresh(self, market, now=None):
        return False


@pytest.fixture()
def client(client_factory):
    with client_factory(FakeNameProvider()) as c:
        c.app.state.refresh_service = FakeRefreshService()
        c.app.state.session_service = ClosedSessionService()
        yield c


def test_add_list_delete_reorder_watchlist(client):
    # 添加成功 201
    resp = client.post("/api/watchlist", json={"symbol": "600519", "market": "CN", "asset_type": "STOCK"})
    assert resp.status_code == 201
    assert resp.json()["name"] == "贵州茅台"

    resp = client.post("/api/watchlist", json={"symbol": "00700", "market": "HK", "asset_type": "STOCK"})
    assert resp.status_code == 201

    # 重复添加 409
    resp = client.post("/api/watchlist", json={"symbol": "600519", "market": "CN", "asset_type": "STOCK"})
    assert resp.status_code == 409

    # 列表
    items = client.get("/api/watchlist").json()["items"]
    assert [i["instrument_id"] for i in items] == ["CN:STOCK:600519", "HK:STOCK:00700"]

    # 排序
    resp = client.put(
        "/api/watchlist/order",
        json={"items": [
            {"instrument_id": "HK:STOCK:00700", "sort_order": 10},
            {"instrument_id": "CN:STOCK:600519", "sort_order": 20},
        ]},
    )
    ids = [i["instrument_id"] for i in resp.json()["items"]]
    assert ids == ["HK:STOCK:00700", "CN:STOCK:600519"]

    # 删除 204；删除后 404
    assert client.delete("/api/watchlist/CN:STOCK:600519").status_code == 204
    assert client.delete("/api/watchlist/CN:STOCK:600519").status_code == 404


def test_add_unknown_symbol_returns_404(client):
    resp = client.post("/api/watchlist", json={"symbol": "999999", "market": "CN", "asset_type": "STOCK"})
    assert resp.status_code == 404
    assert "无法识别" in resp.json()["detail"]


def test_watchlist_rejects_index_type(client):
    resp = client.post("/api/watchlist", json={"symbol": "000001", "market": "CN", "asset_type": "INDEX"})
    assert resp.status_code == 422


def test_index_watchlist_crud(client):
    resp = client.post("/api/index-watchlist", json={"symbol": "000001", "market": "CN", "asset_type": "INDEX"})
    assert resp.status_code == 201
    assert resp.json()["name"] == "上证指数"

    # 重复 409
    assert client.post("/api/index-watchlist", json={"symbol": "000001", "market": "CN", "asset_type": "INDEX"}).status_code == 409

    # 禁止 STOCK / ETF
    assert client.post("/api/index-watchlist", json={"symbol": "600519", "market": "CN", "asset_type": "STOCK"}).status_code == 422
    assert client.post("/api/index-watchlist", json={"symbol": "510300", "market": "CN", "asset_type": "ETF"}).status_code == 422

    # 指数与股票/ETF 自选彼此独立
    wl_ids = [i["instrument_id"] for i in client.get("/api/watchlist").json()["items"]]
    assert "CN:INDEX:000001" not in wl_ids

    # 排序 + 删除
    client.post("/api/index-watchlist", json={"symbol": "000300", "market": "CN", "asset_type": "INDEX"})
    resp = client.put(
        "/api/index-watchlist/order",
        json={"items": [
            {"instrument_id": "CN:INDEX:000300", "sort_order": 10},
            {"instrument_id": "CN:INDEX:000001", "sort_order": 20},
        ]},
    )
    assert [i["instrument_id"] for i in resp.json()["items"]] == ["CN:INDEX:000300", "CN:INDEX:000001"]

    assert client.delete("/api/index-watchlist/CN:INDEX:000001").status_code == 204


def test_watchlist_page_renders(client):
    resp = client.get("/watchlist")
    assert resp.status_code == 200
    assert "股票 / ETF 自选" in resp.text


def test_add_watchlist_triggers_refresh_when_market_closed(client):
    """休市时段添加自选仍触发即时行情刷新（不依赖市场状态）。"""
    resp = client.post(
        "/api/watchlist", json={"symbol": "600519", "market": "CN", "asset_type": "STOCK"}
    )
    assert resp.status_code == 201
    assert client.app.state.refresh_service.calls == [["CN:STOCK:600519"]]


def test_add_index_watchlist_triggers_refresh(client):
    """休市时段添加指数同样触发即时行情刷新。"""
    resp = client.post(
        "/api/index-watchlist", json={"symbol": "000001", "market": "CN", "asset_type": "INDEX"}
    )
    assert resp.status_code == 201
    assert client.app.state.refresh_service.calls == [["CN:INDEX:000001"]]


def test_add_returns_201_when_refresh_fails(client):
    """即时刷新抛异常不影响添加结果。"""
    client.app.state.refresh_service = FakeRefreshService(exc=RuntimeError("provider down"))
    resp = client.post(
        "/api/watchlist", json={"symbol": "600519", "market": "CN", "asset_type": "STOCK"}
    )
    assert resp.status_code == 201


class FakeFundamentalJob:
    """记录即时估值获取调用的假件；exc 非 None 时模拟 Provider 故障。"""

    def __init__(self, exc: Exception | None = None):
        self.calls: list[list[str]] = []
        self.exc = exc

    def refresh_instruments(self, instrument_ids):
        self.calls.append(list(instrument_ids))
        if self.exc:
            raise self.exc


def test_add_cn_stock_triggers_fundamental_refresh(client):
    """添加 A 股股票后立即获取该股估值。"""
    client.app.state.fundamental_refresh = FakeFundamentalJob()
    resp = client.post("/api/watchlist", json={"symbol": "600519", "market": "CN", "asset_type": "STOCK"})
    assert resp.status_code == 201
    assert client.app.state.fundamental_refresh.calls == [["CN:STOCK:600519"]]


def test_add_etf_or_hk_stock_skips_fundamental_refresh(client):
    """添加 ETF / 港股不触发估值获取。"""
    client.app.state.fundamental_refresh = FakeFundamentalJob()
    assert client.post("/api/watchlist", json={"symbol": "510300", "market": "CN", "asset_type": "ETF"}).status_code == 201
    assert client.post("/api/watchlist", json={"symbol": "00700", "market": "HK", "asset_type": "STOCK"}).status_code == 201
    assert client.app.state.fundamental_refresh.calls == []


def test_add_returns_201_when_fundamental_refresh_fails(client):
    """估值获取失败不影响添加结果。"""
    client.app.state.fundamental_refresh = FakeFundamentalJob(exc=RuntimeError("tushare down"))
    resp = client.post("/api/watchlist", json={"symbol": "600519", "market": "CN", "asset_type": "STOCK"})
    assert resp.status_code == 201


def test_add_hk_index_symbol_case_insensitive(client):
    """港股指数代码大小写不敏感：hstech -> HSTECH，重复添加 409。"""
    client.app.state.name_provider.names[("HK", "INDEX", "HSTECH")] = "恒生科技指数"
    resp = client.post("/api/index-watchlist", json={"symbol": "hstech", "market": "HK", "asset_type": "INDEX"})
    assert resp.status_code == 201
    assert resp.json()["instrument_id"] == "HK:INDEX:HSTECH"
    assert resp.json()["symbol"] == "HSTECH"

    resp = client.post("/api/index-watchlist", json={"symbol": "HSTECH", "market": "HK", "asset_type": "INDEX"})
    assert resp.status_code == 409


def test_add_unknown_hk_index_error_includes_hint(client):
    """港股指数识别失败时错误信息附带常见代码指引。"""
    resp = client.post("/api/index-watchlist", json={"symbol": "HS2083", "market": "HK", "asset_type": "INDEX"})
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "无法识别" in detail
    assert "HSTECH" in detail
