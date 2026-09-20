"""Tushare 历史 Provider 离线单测（mock client，不依赖真实 Token/网络）。

覆盖（tasks 3.9）：字段常量口径（§8.2/§9.1/§10.1/§13~§16/§32）、normalize
字段映射、截断标志（§33）、未知 ts_code（§35.1）、分片参数（§8.3/§9.2）、
fallback 逐证券（§33.2）、Registry metrics key（§31.5）。strict 日历见
test_trading_calendar_strict.py。
"""

from __future__ import annotations

import time
from datetime import date

import pandas as pd
import pytest

from app.config import (
    AppConfig,
    HistoryProviderConfig,
    ProvidersConfig,
    TimeoutConfig,
    TushareConfig,
)
from app.models.instrument import Instrument
from app.observability.provider_metrics import ProviderMetricsRegistry
from app.providers.history import HistoryProviderRegistry
from app.providers.history.tushare import (
    ADJ_FACTOR_FIELDS,
    DAILY_BASIC_FIELDS,
    DAILY_FIELDS,
    MONEYFLOW_FIELDS,
    NAMECHANGE_FIELDS,
    STOCK_BASIC_FIELDS,
    STOCK_COMPANY_FIELDS,
    TushareHistoricalMarketDataProvider,
    TushareHistorySchemaError,
    UnknownInstrumentError,
)
from app.providers.tushare_common import (
    TushareRequestGate,
    TushareTimeoutError,
    TushareTransport,
)

TRADE_DAY = date(2026, 9, 16)

MAOTAI = Instrument(
    instrument_id="CN:STOCK:600519", symbol="600519", name="贵州茅台",
    market="CN", asset_type="STOCK", currency="CNY", exchange="SSE",
)
PINGAN = Instrument(
    instrument_id="CN:STOCK:000001", symbol="000001", name="平安银行",
    market="CN", asset_type="STOCK", currency="CNY", exchange="SZSE",
)
BSE_STOCK = Instrument(
    instrument_id="CN:STOCK:430047", symbol="430047", name="诺思兰德",
    market="CN", asset_type="STOCK", currency="CNY", exchange="BSE",
)
# 日级方法传入的 instruments 应只取 CN/STOCK 生效，其余不参与映射
HK_STOCK = Instrument(
    instrument_id="HK:STOCK:00700", symbol="00700", name="腾讯控股",
    market="HK", asset_type="STOCK", currency="HKD", exchange="HKEX",
)


class FakeTushareClient:
    """按 endpoint 预置响应的 fake SDK client；记录全部调用参数。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._responses: dict[str, object] = {}

    def add(self, endpoint: str, response) -> None:
        self._responses[endpoint] = response

    def __getattr__(self, endpoint: str):
        def call(**params):
            self.calls.append((endpoint, params))
            resp = self._responses.get(endpoint)
            if callable(resp):
                return resp(**params)
            return resp

        return call

    def calls_for(self, endpoint: str) -> list[dict]:
        return [params for name, params in self.calls if name == endpoint]


def _provider(client: FakeTushareClient):
    cfg = AppConfig(tushare=TushareConfig(token="fake-token"))
    transport = TushareTransport(
        cfg, gate=TushareRequestGate(0), client_factory=lambda c: client
    )
    return TushareHistoricalMarketDataProvider(cfg, transport=transport)


def _daily_row(ts_code: str = "600519.SH", **overrides) -> dict:
    row = {
        "ts_code": ts_code,
        "trade_date": "20260916",
        "open": 1500.0, "high": 1510.0, "low": 1495.0, "close": 1505.0,
        "pre_close": 1498.0, "change": 7.0, "pct_chg": 0.47,
        "vol": 25000.0, "amount": 376000.0,
        "ah_vol": None, "ah_amount": None,
    }
    row.update(overrides)
    return row


# ---- 字段常量口径（技术方案 §8.2/§9.1/§10.1/§13~§16/§32） ----


@pytest.mark.parametrize(
    ("fields", "required", "expected_len"),
    [
        (STOCK_BASIC_FIELDS, ("ts_code", "symbol"), 17),
        (STOCK_COMPANY_FIELDS, ("ts_code",), 18),
        (NAMECHANGE_FIELDS, ("ts_code",), 6),
        (DAILY_FIELDS, ("ts_code", "trade_date"), 13),
        (ADJ_FACTOR_FIELDS, ("ts_code", "trade_date", "adj_factor"), 3),
        (DAILY_BASIC_FIELDS, ("ts_code", "trade_date"), 19),
        (MONEYFLOW_FIELDS, ("ts_code", "trade_date"), 20),
    ],
)
def test_field_constants_match_design(fields, required, expected_len):
    assert len(fields) == expected_len
    assert len(set(fields)) == expected_len  # 无重复
    assert all(name in fields for name in required)  # REQUIRED ⊆ EXPECTED


# ---- daily ----


def test_daily_normalizes_fields_and_passes_explicit_fields():
    client = FakeTushareClient()
    client.add("daily", pd.DataFrame([_daily_row()]))
    batch = _provider(client).get_daily(TRADE_DAY, [MAOTAI, HK_STOCK])

    assert batch.source == "tushare"
    assert batch.raw_row_count == 1
    assert batch.truncation_risk is False
    record = batch.records[0]
    assert record.instrument_id == "CN:STOCK:600519"
    assert record.ts_code == "600519.SH"
    assert record.trade_date == TRADE_DAY
    assert record.open == 1500.0 and record.high == 1510.0
    assert record.close == 1505.0 and record.pre_close == 1498.0
    assert record.change == 7.0 and record.pct_chg == 0.47
    assert record.vol == 25000.0 and record.amount == 376000.0
    assert record.ah_vol is None and record.ah_amount is None

    (params,) = client.calls_for("daily")
    assert params["trade_date"] == "20260916"
    assert params["fields"] == ",".join(DAILY_FIELDS)


def test_daily_zero_rows_returns_empty_batch():
    client = FakeTushareClient()
    client.add("daily", pd.DataFrame())
    batch = _provider(client).get_daily(TRADE_DAY, [MAOTAI])
    assert batch.records == []
    assert batch.raw_row_count == 0
    assert batch.truncation_risk is False


def test_daily_missing_required_column_raises_schema_mismatch():
    client = FakeTushareClient()
    df = pd.DataFrame([_daily_row()]).drop(columns=["trade_date"])
    client.add("daily", df)
    with pytest.raises(TushareHistorySchemaError) as exc_info:
        _provider(client).get_daily(TRADE_DAY, [MAOTAI])
    assert exc_info.value.error_code == "SCHEMA_MISMATCH"


def test_daily_unparseable_trade_date_raises_schema_mismatch():
    client = FakeTushareClient()
    client.add("daily", pd.DataFrame([_daily_row(trade_date="2026/09/16")]))
    with pytest.raises(TushareHistorySchemaError):
        _provider(client).get_daily(TRADE_DAY, [MAOTAI])


def test_daily_unknown_ts_code_raises_unknown_instrument():
    client = FakeTushareClient()
    client.add("daily", pd.DataFrame([_daily_row(ts_code="999999.SH")]))
    with pytest.raises(UnknownInstrumentError) as exc_info:
        _provider(client).get_daily(TRADE_DAY, [MAOTAI])
    assert exc_info.value.error_code == "UNKNOWN_INSTRUMENT"


def test_daily_at_row_cap_marks_truncation_risk():
    client = FakeTushareClient()
    client.add("daily", pd.DataFrame([_daily_row() for _ in range(6000)]))
    batch = _provider(client).get_daily(TRADE_DAY, [MAOTAI])
    assert batch.truncation_risk is True
    assert batch.raw_row_count == 6000
    assert len(batch.records) == 6000  # 标志归标志，记录仍完整交付由上层决策


def test_daily_dirty_numeric_values_become_none():
    client = FakeTushareClient()
    client.add("daily", pd.DataFrame([_daily_row(open="-", close="nan")]))
    record = _provider(client).get_daily(TRADE_DAY, [MAOTAI]).records[0]
    assert record.open is None and record.close is None


# ---- adj_factor ----


def test_adj_factor_requires_positive_parsable_value():
    client = FakeTushareClient()
    client.add(
        "adj_factor",
        pd.DataFrame(
            [{"ts_code": "600519.SH", "trade_date": "20260916", "adj_factor": 12.34}]
        ),
    )
    record = _provider(client).get_adj_factors(TRADE_DAY, [MAOTAI]).records[0]
    assert record.adj_factor == 12.34
    assert record.instrument_id == "CN:STOCK:600519"

    client2 = FakeTushareClient()
    client2.add(
        "adj_factor",
        pd.DataFrame(
            [{"ts_code": "600519.SH", "trade_date": "20260916", "adj_factor": None}]
        ),
    )
    with pytest.raises(TushareHistorySchemaError):
        _provider(client2).get_adj_factors(TRADE_DAY, [MAOTAI])


# ---- daily_basic / moneyflow ----


def test_daily_basic_preserves_nulls_and_int_limit_status():
    client = FakeTushareClient()
    client.add(
        "daily_basic",
        pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ", "trade_date": "20260916",
                    "close": 10.5, "turnover_rate": 0.8, "turnover_rate_f": 1.2,
                    "volume_ratio": 0.9, "pe": None, "pe_ttm": None, "pb": 0.6,
                    "ps": 1.5, "ps_ttm": 1.6, "dv_ratio": None, "dv_ttm": None,
                    "total_share": 1940000.0, "float_share": 1940000.0,
                    "free_share": 1940000.0, "total_mv": 2040000.0,
                    "circ_mv": 2040000.0, "limit_status": 1,
                }
            ]
        ),
    )
    record = _provider(client).get_daily_basic(TRADE_DAY, [PINGAN]).records[0]
    assert record.instrument_id == "CN:STOCK:000001"
    assert record.close == 10.5 and record.pb == 0.6
    assert record.pe is None and record.pe_ttm is None  # 亏损 NULL 保留原义
    assert record.dv_ratio is None
    assert record.limit_status == 1  # SMALLINT 走 int


def test_moneyflow_converts_int_vols_and_allows_negative_net():
    client = FakeTushareClient()
    client.add(
        "moneyflow",
        pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ", "trade_date": "20260916",
                    "buy_sm_vol": 1000.0, "buy_sm_amount": 2000.0,
                    "sell_sm_vol": 1100.0, "sell_sm_amount": 2100.0,
                    "buy_md_vol": 3000.0, "buy_md_amount": 6000.0,
                    "sell_md_vol": None, "sell_md_amount": None,
                    "buy_lg_vol": 5000.0, "buy_lg_amount": 10000.0,
                    "sell_lg_vol": 4000.0, "sell_lg_amount": 8000.0,
                    "buy_elg_vol": 7000.0, "buy_elg_amount": 14000.0,
                    "sell_elg_vol": 6500.0, "sell_elg_amount": 13000.0,
                    "net_mf_vol": -1200.0, "net_mf_amount": -2400.5,
                }
            ]
        ),
    )
    record = _provider(client).get_moneyflow(TRADE_DAY, [PINGAN]).records[0]
    assert record.buy_sm_vol == 1000 and isinstance(record.buy_sm_vol, int)
    assert record.sell_md_vol is None
    assert record.net_mf_vol == -1200  # net 可负
    assert record.net_mf_amount == -2400.5


# ---- 主档分片（§8.3 / §9.2） ----


def _stock_basic_row(**overrides) -> dict:
    row = {
        "ts_code": "600519.SH", "symbol": "600519", "name": "贵州茅台",
        "area": "贵州", "industry": "白酒", "fullname": "贵州茅台酒股份有限公司",
        "enname": "Kweichow Moutai", "cnspell": "gzmt", "market": "主板",
        "exchange": "SSE", "curr_type": "CNY", "list_status": "L",
        "list_date": "20010827", "delist_date": None, "is_hs": "S",
        "act_name": "贵州茅台", "act_ent_type": "股份有限公司",
    }
    row.update(overrides)
    return row


def test_stock_basic_requests_fifteen_shards_and_allows_empty():
    client = FakeTushareClient()

    def respond(**params):
        if params["exchange"] == "SSE" and params["list_status"] == "L":
            return pd.DataFrame([_stock_basic_row()])
        return pd.DataFrame()  # 其余 14 个空分片

    client.add("stock_basic", respond)
    batch = _provider(client).get_stock_basic()

    calls = client.calls_for("stock_basic")
    assert len(calls) == 3 * 5  # SSE/SZSE/BSE × L/D/P/G/UN
    assert {params["exchange"] for params in calls} == {"SSE", "SZSE", "BSE"}
    assert {params["list_status"] for params in calls} == {"L", "D", "P", "G", "UN"}
    for params in calls:
        assert params["fields"] == ",".join(STOCK_BASIC_FIELDS)

    assert batch.raw_row_count == 1
    assert batch.truncation_risk is False
    record = batch.records[0]
    assert record.instrument_id == "CN:STOCK:600519"  # §7.1：不经交易所推断
    assert record.symbol == "600519"
    assert record.list_date == date(2001, 8, 27)
    assert record.delist_date is None
    assert record.list_status == "L"


def test_stock_basic_shard_at_cap_marks_truncation():
    client = FakeTushareClient()
    client.add("stock_basic", pd.DataFrame([_stock_basic_row() for _ in range(6000)]))
    batch = _provider(client).get_stock_basic()
    assert batch.truncation_risk is True
    assert batch.raw_row_count == 6000 * 15  # 全部 15 分片都命中


def test_stock_company_three_shards_and_cap_4500():
    client = FakeTushareClient()

    def respond(**params):
        if params["exchange"] == "SZSE":
            return pd.DataFrame([{"ts_code": "000001.SZ", "com_name": "平安银行股份有限公司"}])
        return pd.DataFrame()

    client.add("stock_company", respond)
    batch = _provider(client).get_stock_company()
    calls = client.calls_for("stock_company")
    assert [params["exchange"] for params in calls] == ["SSE", "SZSE", "BSE"]
    assert batch.records[0].instrument_id == "CN:STOCK:000001"

    client2 = FakeTushareClient()
    client2.add(
        "stock_company",
        pd.DataFrame([{"ts_code": "000001.SZ"} for _ in range(4500)]),
    )
    assert _provider(client2).get_stock_company().truncation_risk is True


# ---- namechange ----


def test_namechanges_formats_params_and_maps_instrument():
    client = FakeTushareClient()
    client.add(
        "namechange",
        pd.DataFrame(
            [
                {
                    "ts_code": "600519.SH", "name": "G贵茅台",
                    "start_date": "20010827", "end_date": "20050101",
                    "ann_date": None, "change_reason": "更名",
                }
            ]
        ),
    )
    batch = _provider(client).get_name_changes(
        ts_code="600519.SH",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 9, 16),
    )
    (params,) = client.calls_for("namechange")
    assert params["ts_code"] == "600519.SH"
    assert params["start_date"] == "20260101" and params["end_date"] == "20260916"
    assert params["fields"] == ",".join(NAMECHANGE_FIELDS)

    record = batch.records[0]
    assert record.instrument_id == "CN:STOCK:600519"
    assert record.start_date == date(2001, 8, 27)
    assert record.end_date == date(2005, 1, 1)
    assert record.ann_date is None


def test_namechanges_without_filters_requests_all():
    client = FakeTushareClient()
    client.add("namechange", pd.DataFrame())
    _provider(client).get_name_changes()
    (params,) = client.calls_for("namechange")
    assert "ts_code" not in params and "start_date" not in params and "end_date" not in params


# ---- 截断 fallback（§33.2） ----


def test_fallback_requests_per_instrument_with_exchange_suffix():
    client = FakeTushareClient()

    def respond(**params):
        ts_code = params["ts_code"]
        suffix = ts_code.split(".")[1]
        return pd.DataFrame([_daily_row(ts_code=ts_code, open=float(len(suffix)))])

    client.add("daily", respond)
    batch = _provider(client).get_daily_for_instruments(
        TRADE_DAY, [BSE_STOCK, MAOTAI, PINGAN]
    )
    calls = client.calls_for("daily")
    # 逐证券、按 symbol 升序；后缀按 exchange 显式映射（SH/SZ/BJ）
    assert [params["ts_code"] for params in calls] == [
        "000001.SZ", "430047.BJ", "600519.SH",
    ]
    assert all(params["trade_date"] == "20260916" for params in calls)
    assert batch.raw_row_count == 3
    assert {r.instrument_id for r in batch.records} == {
        "CN:STOCK:000001", "CN:STOCK:430047", "CN:STOCK:600519",
    }
    assert batch.truncation_risk is False


def test_fallback_rejects_instrument_without_exchange():
    no_exchange = Instrument(
        instrument_id="CN:STOCK:600519", symbol="600519", name="贵州茅台",
        market="CN", asset_type="STOCK", currency="CNY", exchange=None,
    )
    client = FakeTushareClient()
    client.add("daily", pd.DataFrame())
    with pytest.raises(UnknownInstrumentError) as exc_info:
        _provider(client).get_daily_for_instruments(TRADE_DAY, [no_exchange])
    assert "exchange" in str(exc_info.value)


# ---- HistoryProviderRegistry（§31.4/§31.5） ----


def test_registry_metrics_key_and_wrapping():
    client = FakeTushareClient()
    client.add("daily", pd.DataFrame([_daily_row()]))
    cfg = AppConfig(tushare=TushareConfig(token="fake-token"))
    provider = _provider(client)
    registry = HistoryProviderRegistry(cfg, provider=provider)

    batch = registry.get_daily(TRADE_DAY, [MAOTAI])
    assert batch.records[0].instrument_id == "CN:STOCK:600519"

    metrics = registry.metrics.get("tushare_history_daily")
    assert metrics.request_count == 1 and metrics.success_count == 1
    assert registry.metrics.get("tushare_history_stock_basic").request_count == 0

    # fallback 方法计入同一 dataset 的 metrics key
    registry.get_daily_for_instruments(TRADE_DAY, [MAOTAI])
    assert registry.metrics.get("tushare_history_daily").request_count == 2


def test_registry_classifies_timeout_and_propagates():
    def raise_timeout(**params):
        raise TimeoutError("network timeout")

    client = FakeTushareClient()
    client.add("daily", raise_timeout)
    cfg = AppConfig(tushare=TushareConfig(token="fake-token"))
    registry = HistoryProviderRegistry(cfg, provider=_provider(client))

    with pytest.raises(TimeoutError):
        registry.get_daily(TRADE_DAY, [MAOTAI])
    metrics = registry.metrics.get("tushare_history_daily")
    assert metrics.timeout_count == 1 and metrics.success_count == 0


def test_registry_does_not_impose_method_level_timeout_on_multi_request_methods():
    """15 个分片的 stock_basic 不因方法级 wall-clock 上限被误判超时。

    回归（真实发现）：Registry 曾以固定 15s 包整个方法，而 stock_basic 有
    15 个分片、受 gate 节流（1.25s/请求）正常就需 ~17.5s，必然假 timeout。
    现在只记录方法级 success/error/duration，不设墙钟上限。
    """
    client = FakeTushareClient()

    def slow_shard(**params):
        # 分片本身很慢，但每一步都远低于单请求超时
        time.sleep(0.02)
        return pd.DataFrame([_stock_basic_row(ts_code=f"{params['exchange']}.X")])

    client.add("stock_basic", slow_shard)
    # 单请求超时压到 0.05s，而 15 个分片合计 0.3s：若方法级仍套用该值，
    # 必然在第一个分片之后就被判超时
    cfg = AppConfig(
        tushare=TushareConfig(token="fake-token"),
        providers=ProvidersConfig(timeout=TimeoutConfig(tushare=0.05)),
    )
    registry = HistoryProviderRegistry(cfg, provider=_provider(client))

    batch = registry.get_stock_basic()
    assert len(client.calls_for("stock_basic")) == 15  # 15 个分片全部完成
    assert batch.records[0].symbol == "600519"
    metrics = registry.metrics.get("tushare_history_stock_basic")
    assert metrics.timeout_count == 0
    assert metrics.success_count == 1
    assert metrics.error_count == 0
    assert metrics.last_duration_ms is not None


def test_registry_counts_real_request_timeout_as_timeout_not_error():
    """去掉方法级 timeout 后，单请求真实超时仍进 timeout_count。

    回归约束：超时分类依据异常类型，不能因为方法级不限时就把超时退化成
    普通 error_count。用 ``requests.exceptions.ReadTimeout`` 是因为它
    **不是** 内建 TimeoutError 的子类——只按 TimeoutError 分类会漏掉真实
    SDK 超时。
    """
    import requests

    def raise_sdk_timeout(**params):
        raise requests.exceptions.ReadTimeout(
            "HTTPConnectionPool(host='api.waditu.com', port=80): Read timed out. (read timeout=15)"
        )

    client = FakeTushareClient()
    client.add("daily", raise_sdk_timeout)
    cfg = AppConfig(tushare=TushareConfig(token="fake-token"))
    registry = HistoryProviderRegistry(cfg, provider=_provider(client))

    with pytest.raises(TushareTimeoutError):
        registry.get_daily(TRADE_DAY, [MAOTAI])
    metrics = registry.metrics.get("tushare_history_daily")
    assert metrics.timeout_count == 1
    assert metrics.error_count == 0
    assert metrics.success_count == 0
    # 错误码与文本：TUSHARE_TIMEOUT，且不含 Token
    assert metrics.last_error is not None and "timeout" in metrics.last_error


def test_daily_basic_fallback_sends_one_request_per_missing_code():
    """daily_basic 补齐逐只请求，**不**使用逗号分隔的多代码参数。

    回归（真实 Token 验证）：``daily_basic(ts_code="a,b,c")`` 会**静默返回
    0 行**而不报错，因此本接口不得复用其他接口的 multi-code fallback。
    """
    client = FakeTushareClient()
    client.add(
        "daily_basic",
        lambda **params: pd.DataFrame(
            [
                {
                    "ts_code": params["ts_code"], "trade_date": "20260916",
                    "close": 10.5, "turnover_rate": None, "turnover_rate_f": None,
                    "volume_ratio": None, "pe": None, "pe_ttm": None, "pb": None,
                    "ps": None, "ps_ttm": None, "dv_ratio": None, "dv_ttm": None,
                    "total_share": None, "float_share": None, "free_share": None,
                    "total_mv": None, "circ_mv": None, "limit_status": None,
                }
            ]
        ),
    )
    batch = _provider(client).get_daily_basic_for_instruments(
        TRADE_DAY, [MAOTAI, PINGAN, BSE_STOCK],
        missing_ts_codes=["000001.SZ", "600519.SH"],
    )
    calls = client.calls_for("daily_basic")
    codes = [params["ts_code"] for params in calls]
    assert codes == ["000001.SZ", "600519.SH"]  # 只查缺失证券
    assert all("," not in code for code in codes)  # 绝不合并成多代码参数
    assert {r.instrument_id for r in batch.records} == {
        "CN:STOCK:000001", "CN:STOCK:600519",
    }


def test_daily_basic_fallback_rejects_code_outside_master():
    """候选集与主档不一致时抛 UNKNOWN_INSTRUMENT，不静默丢弃。"""
    client = FakeTushareClient()
    client.add("daily_basic", pd.DataFrame())
    with pytest.raises(UnknownInstrumentError):
        _provider(client).get_daily_basic_for_instruments(
            TRADE_DAY, [MAOTAI], missing_ts_codes=["000001.SZ"]
        )


def test_registry_runs_provider_on_caller_thread_so_no_abandoned_request():
    """Provider 方法在调用方线程执行：不存在可被放弃、仍在发请求的线程。

    回归约束：方法级 ``ThreadPoolExecutor`` 限时（现已移除）会把调用丢到
    worker 线程，超时后 ``shutdown(wait=False)``——被放弃的线程继续发远端
    请求，与 Service 的重试重叠成并发请求（既冲击限流，也让"超时后重试"实际
    变成两个在飞的请求）。现在由 SDK 对单请求原生超时，调用同步返回即代表
    该请求已结束，因此重试与上一次请求绝不重叠。
    """
    import threading

    seen_threads: list[int] = []

    def record_thread(**params):
        seen_threads.append(threading.get_ident())
        return pd.DataFrame([_daily_row()])

    client = FakeTushareClient()
    client.add("daily", record_thread)
    cfg = AppConfig(tushare=TushareConfig(token="fake-token"))
    registry = HistoryProviderRegistry(cfg, provider=_provider(client))

    caller = threading.get_ident()
    registry.get_daily(TRADE_DAY, [MAOTAI])
    registry.get_daily_for_instruments(TRADE_DAY, [MAOTAI])
    # 15 分片的复合方法同样不得落入线程池
    client.add(
        "stock_basic",
        lambda **params: (seen_threads.append(threading.get_ident()), pd.DataFrame())[1],
    )
    registry.get_stock_basic()

    assert seen_threads, "Provider 应被调用"
    assert set(seen_threads) == {caller}, "Provider 必须在调用方线程同步执行"


def test_registry_unknown_source_raises_value_error():
    cfg = AppConfig(
        providers=ProvidersConfig(history=HistoryProviderConfig(market_data="bad"))
    )
    with pytest.raises(ValueError, match="未知的历史数据源"):
        HistoryProviderRegistry(cfg)


def test_transport_normalizes_sdk_exception_with_error_code():
    def raise_token_error(**params):
        raise Exception("您的token不对，请确认。")

    client = FakeTushareClient()
    client.add("daily", raise_token_error)
    with pytest.raises(Exception) as exc_info:
        _provider(client).get_daily(TRADE_DAY, [MAOTAI])
    assert exc_info.value.error_code == "TUSHARE_TOKEN_MISSING"
    # TushareTimeoutError 同时是 TimeoutError（进入 metrics 超时桶）
    assert issubclass(TushareTimeoutError, TimeoutError)


def test_registry_metrics_registry_injection():
    client = FakeTushareClient()
    client.add("daily", pd.DataFrame([_daily_row()]))
    metrics = ProviderMetricsRegistry()
    cfg = AppConfig(tushare=TushareConfig(token="fake-token"))
    registry = HistoryProviderRegistry(cfg, metrics=metrics, provider=_provider(client))
    assert registry.metrics is metrics
    registry.get_daily(TRADE_DAY, [MAOTAI])
    assert metrics.get("tushare_history_daily").success_count == 1
