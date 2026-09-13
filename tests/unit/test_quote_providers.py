"""Provider 转换单测：DataFrame / 腾讯行情串 -> 内部 Quote（PRD 第 30 节）。"""

from __future__ import annotations

import pytest

from app.models.instrument import Instrument
from app.providers.quote.akshare import AkshareQuoteProvider
from app.providers.quote.tencent import (
    TencentQuoteClient,
    TencentQuoteProvider,
    quote_from_fields,
    to_tencent_code,
)

MAOTAI = Instrument(
    instrument_id="CN:STOCK:600519", symbol="600519", name="贵州茅台",
    market="CN", asset_type="STOCK", currency="CNY",
)


def _inst(iid, symbol, name, market, asset_type):
    return Instrument(
        instrument_id=iid, symbol=symbol, name=name,
        market=market, asset_type=asset_type, currency="CNY" if market == "CN" else "HKD",
    )


# ---- 腾讯代码映射 ----


def test_to_tencent_code():
    assert to_tencent_code("CN", "STOCK", "600519") == "sh600519"
    assert to_tencent_code("CN", "STOCK", "000001") == "sz000001"
    assert to_tencent_code("CN", "ETF", "510300") == "sh510300"
    assert to_tencent_code("CN", "ETF", "159915") == "sz159915"
    assert to_tencent_code("CN", "INDEX", "000001") == "sh000001"
    assert to_tencent_code("CN", "INDEX", "399300") == "sz399300"
    assert to_tencent_code("HK", "STOCK", "00700") == "hk00700"
    assert to_tencent_code("HK", "INDEX", "HSI") == "r_hkHSI"
    assert to_tencent_code("US", "STOCK", "AAPL") is None


# ---- 腾讯行情串 -> Quote ----


def _tx_fields(name="贵州茅台", price="1297.99", prev="1293.09", ts="20260818161449", chg="0.38"):
    fields = [""] * 40
    fields[1], fields[3], fields[4], fields[31], fields[32], fields[33] = name, price, prev, ts, "4.90", chg
    return fields


def test_quote_from_fields_cn_stock():
    quote = quote_from_fields("CN:STOCK:600519", _tx_fields(), delayed=False)
    assert quote.price == 1297.99
    assert quote.change_percent == 0.38
    assert quote.previous_close == 1293.09
    assert quote.source == "tencent"
    assert quote.source_timestamp is not None
    assert quote.source_timestamp.tzinfo is not None  # 带时区
    assert quote.delayed is False


def test_quote_from_fields_hk_delayed_and_dirty_values():
    fields = _tx_fields(name="腾讯控股", price="-", chg="nan", ts="2026/08/18 16:08:08")
    quote = quote_from_fields("HK:STOCK:00700", fields, delayed=True)
    assert quote.price is None  # "-" -> None
    assert quote.change_percent is None  # nan -> None
    assert quote.delayed is True  # 港股标记延时


# ---- TencentQuoteProvider：mock 客户端 ----


class FakeClient(TencentQuoteClient):
    def __init__(self, data):
        super().__init__()
        self.data = data
        self.calls = 0

    def fetch(self, codes):
        self.calls += 1
        return {c: self.data[c] for c in codes if c in self.data}


def test_tencent_provider_filters_unknown_and_maps():
    client = FakeClient(
        {
            "sh510300": _tx_fields(name="沪深300ETF", price="4.787", chg="-0.29"),
            "r_hkHSI": _tx_fields(name="恒生指数", price="25471.15", chg="0.07", ts="2026/08/18 18:31:05"),
        }
    )
    provider = TencentQuoteProvider(client)
    quotes = provider.get_quotes(
        [
            _inst("CN:ETF:510300", "510300", "沪深300ETF", "CN", "ETF"),
            _inst("HK:INDEX:HSI", "HSI", "恒生指数", "HK", "INDEX"),
            _inst("CN:ETF:999999", "999999", "未知", "CN", "ETF"),  # 数据源不认识 -> 丢弃
        ]
    )
    assert set(quotes) == {"CN:ETF:510300", "HK:INDEX:HSI"}
    assert quotes["HK:INDEX:HSI"].delayed is True
    assert quotes["CN:ETF:510300"].delayed is False


def test_tencent_provider_batching():
    client = FakeClient({})
    provider = TencentQuoteProvider(client)
    instruments = [
        _inst(f"HK:STOCK:{i:05d}", f"{i:05d}", "x", "HK", "STOCK") for i in range(125)
    ]
    provider.get_quotes(instruments)
    assert client.calls == 3  # 60 + 60 + 5


# ---- AKShare Provider：mock 全市场 DataFrame ----


def test_akshare_provider_filters_watchlist():
    import pandas as pd

    df = pd.DataFrame(
        [
            {"code": "sh600519", "name": "贵州茅台", "zxj": 1450.12, "zdf": 1.25, "lb": 0.86, "pe_ttm": 21.31},
            {"code": "sz000001", "name": "平安银行", "zxj": 11.05, "zdf": -0.45, "lb": 1.2, "pe_ttm": 4.5},
            {"code": "sh601398", "name": "工商银行", "zxj": "-", "zdf": "nan", "lb": "", "pe_ttm": None},
        ]
    )
    provider = AkshareQuoteProvider()
    provider._fetch_all = lambda: df  # mock 全市场接口

    quotes = provider.get_quotes(
        [
            MAOTAI,
            _inst("CN:STOCK:000001", "000001", "平安银行", "CN", "STOCK"),
        ]
    )
    # 只返回自选两只，工商银行被过滤
    assert set(quotes) == {"CN:STOCK:600519", "CN:STOCK:000001"}
    assert quotes["CN:STOCK:600519"].price == 1450.12
    assert quotes["CN:STOCK:600519"].volume_ratio == 0.86
    assert quotes["CN:STOCK:600519"].source == "akshare"


def test_akshare_provider_dirty_values_to_none():
    import pandas as pd

    df = pd.DataFrame([{"code": "sh601398", "name": "工商银行", "zxj": "-", "zdf": "nan", "lb": "", "pe_ttm": None}])
    provider = AkshareQuoteProvider()
    provider._fetch_all = lambda: df

    quotes = provider.get_quotes([_inst("CN:STOCK:601398", "601398", "工商银行", "CN", "STOCK")])
    quote = quotes["CN:STOCK:601398"]
    assert quote.price is None and quote.change_percent is None and quote.volume_ratio is None


def test_akshare_provider_failure_returns_empty(monkeypatch):
    def boom():
        raise RuntimeError("network down")

    provider = AkshareQuoteProvider()
    provider._fetch_all = boom
    quotes = provider.get_quotes([MAOTAI])
    assert quotes == {}  # 失败不清缓存（这里无缓存），不抛异常


# ---- 在线冒烟（默认跳过：pytest -m online） ----

@pytest.mark.online
def test_live_tencent_fetch():
    pytest.importorskip("httpx")
    client = TencentQuoteClient()
    data = client.fetch(["sh600519", "sh000001"])
    assert "sh600519" in data and "sh000001" in data
    assert data["sh600519"][1] == "贵州茅台"
