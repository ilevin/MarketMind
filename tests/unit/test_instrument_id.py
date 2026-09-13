"""instrument_id 生成规则单测（PRD 第 30 节）。"""

import pytest

from app.services.instrument_id import (
    InvalidInstrumentError,
    build_instrument_id,
    parse_instrument_id,
)


def test_a_stock():
    assert build_instrument_id("CN", "STOCK", "600519") == "CN:STOCK:600519"


def test_a_etf():
    assert build_instrument_id("CN", "ETF", "510300") == "CN:ETF:510300"


def test_hk_stock():
    assert build_instrument_id("HK", "STOCK", "00700") == "HK:STOCK:00700"


def test_index_same_code_as_stock():
    # 上证指数 000001 与平安银行 000001 同码，靠 asset_type 区分
    assert build_instrument_id("CN", "INDEX", "000001") == "CN:INDEX:000001"
    assert build_instrument_id("CN", "INDEX", "000001") != build_instrument_id(
        "CN", "STOCK", "000001"
    )


def test_invalid_market():
    with pytest.raises(InvalidInstrumentError):
        build_instrument_id("US", "STOCK", "AAPL")


def test_invalid_asset_type():
    with pytest.raises(InvalidInstrumentError):
        build_instrument_id("CN", "BOND", "123456")


def test_empty_symbol():
    with pytest.raises(InvalidInstrumentError):
        build_instrument_id("CN", "STOCK", "  ")


def test_parse_roundtrip():
    market, asset_type, symbol = parse_instrument_id("HK:INDEX:HSI")
    assert (market, asset_type, symbol) == ("HK", "INDEX", "HSI")
