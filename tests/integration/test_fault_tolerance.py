"""容错集成测试：Provider 抛异常时页面/接口仍返回最后缓存数据（PRD 第 15 节）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.instrument import Instrument
from app.models.quote import QuoteSnapshot


class FakeNameProvider:
    def get_name(self, market, asset_type, symbol):
        return f"{market}-{symbol}"


class FailingRegistry:
    """所有 Provider 调用都抛异常（模拟 AKShare/腾讯全部不可达）。"""

    def get_quotes(self, instruments):
        raise ConnectionError("simulated network failure")


@pytest.fixture()
def client_with_cached_data(client_factory):
    with client_factory(FakeNameProvider()) as client:
        # 预写缓存数据：Instrument + Watchlist + QuoteSnapshot
        factory = client.app.state.session_factory
        with factory() as s:
            s.add(
                Instrument(
                    instrument_id="CN:STOCK:600519", symbol="600519", name="贵州茅台",
                    market="CN", asset_type="STOCK", currency="CNY",
                )
            )
            from app.models.watchlist import Watchlist

            s.add(Watchlist(instrument_id="CN:STOCK:600519", sort_order=10))
            s.add(
                QuoteSnapshot(
                    instrument_id="CN:STOCK:600519",
                    price=1450.12,
                    change_percent=1.25,
                    volume_ratio=0.86,
                    source="akshare",
                    fetched_at=datetime.now(UTC),
                )
            )
            s.commit()

        yield client, client.app


def test_quotes_returns_cached_data_when_provider_fails(client_with_cached_data):
    client, app = client_with_cached_data

    # 将行情 Provider 全部换成必然失败的实现（模拟断网）
    app.state.refresh_service.quote_providers = FailingRegistry()

    # 手动刷新：不 500、不崩溃，返回失败统计
    resp = client.post("/api/admin/refresh/quotes?force=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert body["failed"] >= 1

    # /api/quotes 仍返回 DuckDB 最后成功数据，HTTP 200
    resp = client.get("/api/quotes")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["price"] == 1450.12
    assert items[0]["change_percent"] == 1.25
    assert items[0]["volume_ratio"] == 0.86


def test_provider_failure_does_not_clear_cache(client_with_cached_data):
    client, app = client_with_cached_data

    before = client.get("/api/quotes").json()["items"][0]["price"]
    app.state.refresh_service.quote_providers = FailingRegistry()
    client.post("/api/admin/refresh/quotes?force=true")
    after = client.get("/api/quotes").json()["items"][0]["price"]
    assert before == after  # 失败不清空缓存


def test_homepage_renders_on_provider_failure(client_with_cached_data):
    client, app = client_with_cached_data
    app.state.refresh_service.quote_providers = FailingRegistry()
    resp = client.get("/")
    assert resp.status_code == 200


def test_health_ok_on_provider_failure(client_with_cached_data):
    client, app = client_with_cached_data
    app.state.refresh_service.quote_providers = FailingRegistry()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok", "version": "v0.1.0"}
