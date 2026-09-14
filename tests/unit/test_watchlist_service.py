"""WatchlistService 单测：symbol 规范化与识别失败指引。"""

from __future__ import annotations

import pytest

from app.services.watchlist_service import (
    DuplicateItemError,
    IndexWatchlistService,
    InstrumentNotFoundError,
    WatchlistService,
)


class FakeNameProvider:
    def __init__(self):
        self.names = {
            ("CN", "STOCK", "600519"): "贵州茅台",
            ("HK", "INDEX", "HSTECH"): "恒生科技指数",
        }

    def get_name(self, market, asset_type, symbol):
        return self.names.get((market, asset_type, symbol))


@pytest.fixture()
def user_id(user_factory):
    """服务归属用户（multi-user-auth：Service 构造新增 user_id，数据按用户隔离）。"""
    return user_factory("watchlist_svc_owner")["user_id"]


def test_symbol_normalized_strip_and_upper(session, user_id):
    """港股指数字母缩写统一大写；首尾空白去除。"""
    service = IndexWatchlistService(session, FakeNameProvider(), user_id)
    instrument_id = service.add(symbol=" hstech ", market="hk", asset_type="index")
    assert instrument_id == "HK:INDEX:HSTECH"

    inst = service.instrument_repo.get(instrument_id)
    assert inst.symbol == "HSTECH"
    assert inst.name == "恒生科技指数"


def test_symbol_case_insensitive_duplicate(session, user_id):
    """大小写不同视为同一证券，判重返回 409。"""
    service = IndexWatchlistService(session, FakeNameProvider(), user_id)
    service.add(symbol="HSTECH", market="HK", asset_type="INDEX")
    with pytest.raises(DuplicateItemError):
        service.add(symbol="hstech", market="HK", asset_type="INDEX")
    assert len(service.repo.list_ordered()) == 1


def test_stock_symbol_with_spaces(session, user_id):
    service = WatchlistService(session, FakeNameProvider(), user_id)
    assert service.add(symbol=" 600519 ", market="CN", asset_type="STOCK") == "CN:STOCK:600519"


def test_unknown_hk_index_error_includes_hint(session, user_id):
    service = IndexWatchlistService(session, FakeNameProvider(), user_id)
    with pytest.raises(InstrumentNotFoundError) as exc_info:
        service.add(symbol="HS2083", market="HK", asset_type="INDEX")
    detail = str(exc_info.value)
    assert "无法识别证券: HK/INDEX/HS2083" in detail
    assert "HSTECH" in detail


def test_unknown_cn_stock_error_without_hint(session, user_id):
    """非港股指数场景保持原有报错文案，不附指引。"""
    service = WatchlistService(session, FakeNameProvider(), user_id)
    with pytest.raises(InstrumentNotFoundError) as exc_info:
        service.add(symbol="999999", market="CN", asset_type="STOCK")
    assert str(exc_info.value) == "无法识别证券: CN/STOCK/999999"
