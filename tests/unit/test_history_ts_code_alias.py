"""Tushare ts_code 别名规范化（UNKNOWN_INSTRUMENT 修复）离线单测。

背景：Tushare 历史事实返回的 ts_code 可能是该证券的**历史代码**，而
``stock_basic`` 已不再包含它（代码变更后主档只有新代码）。典型实例是
深赤湾A ``000022.SZ`` —— 2018-12-26 代码变更为招商港口 ``001872.SZ``，
但 2010 年的 daily/adj_factor/daily_basic/moneyflow 仍以旧代码返回。

本模块验证 Provider 边界的别名规范化层：
- 仅旧代码 → 改写为规范代码，映射到同一 instrument，不新建证券（§15.3）；
- 新旧代码并存且字段一致 → 只保留规范代码一行（不制造重复事实键，§15.4）；
- 新旧代码并存但字段冲突 → ALIAS_CONFLICT 终态失败，不推进水位（§12）；
- 未登记别名的未知代码 → 仍是 UNKNOWN_INSTRUMENT（§15.5，不放行全部未知）；
- 四个日级数据集与两条抓取路径行为一致（§11.1/§11.2）；
- fallback 输出规范 ts_code，使 Service 的"候选集 - 已返回"不再误判缺失
  （§11.3，见 tests/integration/test_history_sync_service.py）。

别名层只解决"明确登记的代码变更"引起的重复；普通重复行仍由现有
``DUPLICATE_KEY`` 校验拦截（§13 Test 8 / §9.2）。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pandas as pd
import pytest
import app.providers.history.tushare_aliases as aliases_module

from app.config import (
    AppConfig,
    HistoryProviderConfig,
    ProvidersConfig,
    TimeoutConfig,
    TushareConfig,
)
from app.models.instrument import Instrument
from app.providers.history.tushare import (
    ADJ_FACTOR_FIELDS,
    DAILY_BASIC_FIELDS,
    DAILY_FIELDS,
    MONEYFLOW_FIELDS,
    HistoricalAliasConflictError,
    TushareHistoricalMarketDataProvider,
    UnknownInstrumentError,
    canonical_ts_code,
    normalize_historical_aliases,
)
from app.providers.tushare_common import TushareRequestGate, TushareTransport

TRADE_DAY = date(2010, 1, 4)

# 深赤湾A（000022.SZ）2018-12-26 代码变更为招商港口（001872.SZ）
LEGACY_TS_CODE = "000022.SZ"
CANONICAL_TS_CODE = "001872.SZ"

# 两步别名链的测试用代码：A、B 都登记指向 C（≠ CANONICAL_TS_CODE，
# 避免与上面的单步用例混淆）
A_LEGACY = "000022.SZ"
B_LEGACY = "001872.SZ"
CHAIN_CANONICAL = "600519.SH"


@contextmanager
def _alias_table(mapping: dict[str, str]):
    """临时替换全局别名表并在退出时还原（测试用，不影响其他用例）。"""
    original = dict(aliases_module.TUSHARE_TS_CODE_ALIASES)
    aliases_module.TUSHARE_TS_CODE_ALIASES.clear()
    aliases_module.TUSHARE_TS_CODE_ALIASES.update(mapping)
    try:
        yield
    finally:
        aliases_module.TUSHARE_TS_CODE_ALIASES.clear()
        aliases_module.TUSHARE_TS_CODE_ALIASES.update(original)

# 主档里只有在市的新代码证券——旧代码永远不在 stock_basic 中
ZHAOSHANG = Instrument(
    instrument_id="CN:STOCK:001872", symbol="001872", name="招商港口",
    market="CN", asset_type="STOCK", currency="CNY", exchange="SZSE",
)
PINGAN = Instrument(
    instrument_id="CN:STOCK:000001", symbol="000001", name="平安银行",
    market="CN", asset_type="STOCK", currency="CNY", exchange="SZSE",
)
MAOTAI = Instrument(
    instrument_id="CN:STOCK:600519", symbol="600519", name="贵州茅台",
    market="CN", asset_type="STOCK", currency="CNY", exchange="SSE",
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


def _provider(client: FakeTushareClient) -> TushareHistoricalMarketDataProvider:
    cfg = AppConfig(tushare=TushareConfig(token="fake-token"))
    transport = TushareTransport(
        cfg, gate=TushareRequestGate(0), client_factory=lambda c: client
    )
    return TushareHistoricalMarketDataProvider(cfg, transport=transport)


# ---- 各数据集行构造器（字段口径与技术方案 §13~§16 一致） ----


def daily_row(ts_code: str, **overrides) -> dict:
    row = {
        "ts_code": ts_code, "trade_date": "20100104",
        "open": 12.0, "high": 12.5, "low": 11.8, "close": 12.3,
        "pre_close": 12.0, "change": 0.3, "pct_chg": 2.5,
        "vol": 12345.0, "amount": 15200.0,
        "ah_vol": None, "ah_amount": None,
    }
    row.update(overrides)
    return row


def adj_factor_row(ts_code: str, **overrides) -> dict:
    row = {"ts_code": ts_code, "trade_date": "20100104", "adj_factor": 1.0}
    row.update(overrides)
    return row


def daily_basic_row(ts_code: str, **overrides) -> dict:
    row = {
        "ts_code": ts_code, "trade_date": "20100104", "close": 12.3,
        "turnover_rate": 1.1, "turnover_rate_f": 1.2, "volume_ratio": 0.9,
        "pe": 15.0, "pe_ttm": 14.0, "pb": 1.5, "ps": 2.0, "ps_ttm": 2.1,
        "dv_ratio": 0.5, "dv_ttm": 0.6, "total_share": 100.0,
        "float_share": 80.0, "free_share": 70.0, "total_mv": 1230.0,
        "circ_mv": 984.0, "limit_status": 0,
    }
    row.update(overrides)
    return row


def moneyflow_row(ts_code: str, **overrides) -> dict:
    row = {
        "ts_code": ts_code, "trade_date": "20100104",
        "buy_sm_vol": 10, "buy_sm_amount": 1.0, "sell_sm_vol": 20,
        "sell_sm_amount": 2.0, "buy_md_vol": 30, "buy_md_amount": 3.0,
        "sell_md_vol": 40, "sell_md_amount": 4.0, "buy_lg_vol": 50,
        "buy_lg_amount": 5.0, "sell_lg_vol": 60, "sell_lg_amount": 6.0,
        "buy_elg_vol": 70, "buy_elg_amount": 7.0, "sell_elg_vol": 80,
        "sell_elg_amount": 8.0, "net_mf_vol": -90, "net_mf_amount": -9.0,
    }
    row.update(overrides)
    return row


# 数据集 -> (endpoint, 字段常量, 行构造器, 主路径方法名, fallback 方法名)
DATASETS = [
    ("daily", "daily", DAILY_FIELDS, daily_row, "get_daily", "get_daily_for_instruments"),
    ("adj_factor", "adj_factor", ADJ_FACTOR_FIELDS, adj_factor_row,
     "get_adj_factors", "get_adj_factors"),
    ("daily_basic", "daily_basic", DAILY_BASIC_FIELDS, daily_basic_row,
     "get_daily_basic", "get_daily_basic_for_instruments"),
    ("moneyflow", "moneyflow", MONEYFLOW_FIELDS, moneyflow_row,
     "get_moneyflow", "get_moneyflow_for_instruments"),
]
DATASET_IDS = [case[0] for case in DATASETS]


def _call_main(provider, method_name, endpoint, rows):
    """经指定方法取一个交易日的批次（主路径用全市场接口）。"""
    return getattr(provider, method_name)(TRADE_DAY, [ZHAOSHANG, PINGAN, MAOTAI])


# ================================================================
# 一、规范化层本身（纯函数口径）
# ================================================================


class TestCanonicalTsCode:
    def test_registered_legacy_code_maps_to_canonical(self):
        assert canonical_ts_code(LEGACY_TS_CODE) == CANONICAL_TS_CODE
        assert canonical_ts_code(CANONICAL_TS_CODE) == CANONICAL_TS_CODE

    def test_unregistered_code_is_returned_unchanged(self):
        for code in ("000001.SZ", "600519.SH", "430047.BJ", "999999.SZ"):
            assert canonical_ts_code(code) == code

    def test_alias_registry_is_code_change_only_not_pattern_based(self):
        """别名表是逐条登记的代码变更，不是后缀/前缀规则（§9.4）。"""
        from app.providers.history.tushare import TUSHARE_TS_CODE_ALIASES

        assert TUSHARE_TS_CODE_ALIASES[LEGACY_TS_CODE] == CANONICAL_TS_CODE
        # 同 symbol 段的其它交易所代码不受影响
        assert canonical_ts_code("000022.SH") == "000022.SH"


class TestNormalizeHistoricalAliases:
    """§10 职责 1~8：不改输入、只改写已登记别名、冲突即失败。"""

    def test_does_not_mutate_input_dataframe(self):
        df = pd.DataFrame([daily_row(LEGACY_TS_CODE)])
        original = df.copy(deep=True)
        normalize_historical_aliases(df, endpoint="daily", trade_date=TRADE_DAY)
        pd.testing.assert_frame_equal(df, original)

    def test_missing_ts_code_column_raises_schema_mismatch(self):
        from app.providers.history.tushare import TushareHistorySchemaError

        df = pd.DataFrame([{"trade_date": "20100104", "close": 1.0}])
        with pytest.raises(TushareHistorySchemaError) as exc_info:
            normalize_historical_aliases(df, endpoint="daily", trade_date=TRADE_DAY)
        assert exc_info.value.error_code == "SCHEMA_MISMATCH"

    def test_empty_dataframe_passes_through(self):
        df = pd.DataFrame()
        assert len(
            normalize_historical_aliases(df, endpoint="daily", trade_date=TRADE_DAY)
        ) == 0

    def test_only_legacy_code_is_rewritten(self):
        df = pd.DataFrame([daily_row(LEGACY_TS_CODE), daily_row("000001.SZ")])
        out = normalize_historical_aliases(df, endpoint="daily", trade_date=TRADE_DAY)
        assert sorted(out["ts_code"]) == ["000001.SZ", CANONICAL_TS_CODE]

    def test_legacy_and_canonical_identical_keeps_only_canonical(self):
        df = pd.DataFrame(
            [daily_row(LEGACY_TS_CODE), daily_row(CANONICAL_TS_CODE)]
        )
        out = normalize_historical_aliases(df, endpoint="daily", trade_date=TRADE_DAY)
        assert len(out) == 1
        assert out.iloc[0]["ts_code"] == CANONICAL_TS_CODE

    def test_legacy_and_canonical_conflicting_raises_alias_conflict(self):
        df = pd.DataFrame(
            [
                daily_row(LEGACY_TS_CODE, close=10.0),
                daily_row(CANONICAL_TS_CODE, close=11.0),
            ]
        )
        with pytest.raises(HistoricalAliasConflictError) as exc_info:
            normalize_historical_aliases(df, endpoint="daily", trade_date=TRADE_DAY)
        assert exc_info.value.error_code == "ALIAS_CONFLICT"
        # 诊断信息含端点、日期与两个代码，但不含 Token
        message = str(exc_info.value)
        assert "daily" in message and "20100104" in message
        assert LEGACY_TS_CODE in message and CANONICAL_TS_CODE in message

    def test_non_alias_duplicate_is_left_for_duplicate_key_check(self):
        """§10 职责 8：非别名的重复行不在此去重，交给 DUPLICATE_KEY。"""
        df = pd.DataFrame([daily_row("000001.SZ"), daily_row("000001.SZ")])
        out = normalize_historical_aliases(df, endpoint="daily", trade_date=TRADE_DAY)
        assert len(out) == 2

    def test_same_legacy_code_twice_is_left_for_duplicate_key_check(self):
        """组内**全是同一个旧代码**时不做比较也不合并。

        与上一条不同，这里输入命中了别名表（不会走 early-return），真正
        进入分组逻辑：两行同属 000022.SZ、组内没有任何字面规范代码行。
        代码仍应被改写成规范代码（别名层的职责），但两行都要保留——去重
        不是本层职责，交给 DUPLICATE_KEY。
        """
        df = pd.DataFrame([daily_row(LEGACY_TS_CODE), daily_row(LEGACY_TS_CODE)])
        out = normalize_historical_aliases(df, endpoint="daily", trade_date=TRADE_DAY)
        assert len(out) == 2, "同一旧代码的重复行不属别名层去重职责"
        assert list(out["ts_code"]) == [CANONICAL_TS_CODE, CANONICAL_TS_CODE]

    def test_two_legacy_codes_sharing_a_canonical_are_merged(self):
        """两步别名链：A 与 B 都登记指向 C 时，同时返回也要比较并合并。

        没有字面规范行并不代表"与别名无关的重复"——组内来自不同旧代码时
        仍须走冲突比较，否则会退化成 DUPLICATE_KEY（既不检测真冲突，又要
        白等重试）。
        """
        df = pd.DataFrame([daily_row(A_LEGACY), daily_row(B_LEGACY)])
        with _alias_table({A_LEGACY: CHAIN_CANONICAL, B_LEGACY: CHAIN_CANONICAL}):
            out = normalize_historical_aliases(
                df, endpoint="daily", trade_date=TRADE_DAY
            )
        assert len(out) == 1
        assert list(out["ts_code"]) == [CHAIN_CANONICAL]

    def test_two_legacy_codes_sharing_a_canonical_conflict_raises(self):
        """别名链下字段不一致同样必须抛 ALIAS_CONFLICT，而不是放行到下游。"""
        df = pd.DataFrame(
            [
                daily_row(A_LEGACY, close=10.0),
                daily_row(B_LEGACY, close=99.0),
            ]
        )
        with _alias_table({A_LEGACY: CHAIN_CANONICAL, B_LEGACY: CHAIN_CANONICAL}):
            with pytest.raises(HistoricalAliasConflictError) as excinfo:
                normalize_historical_aliases(
                    df, endpoint="daily", trade_date=TRADE_DAY
                )
        assert excinfo.value.error_code == "ALIAS_CONFLICT"


class TestConflictComparisonTolerance:
    """§12：不直接用 DataFrame.equals——空值/dtype/浮点尾差不得制造假冲突。"""

    def test_none_and_nan_are_both_empty(self):
        df = pd.DataFrame(
            [
                daily_row(LEGACY_TS_CODE, ah_vol=None),
                daily_row(CANONICAL_TS_CODE, ah_vol=float("nan")),
            ]
        )
        assert len(
            normalize_historical_aliases(df, endpoint="daily", trade_date=TRADE_DAY)
        ) == 1

    def test_integer_and_float_representation_are_equal(self):
        df = pd.DataFrame(
            [
                adj_factor_row(LEGACY_TS_CODE, adj_factor=1),
                adj_factor_row(CANONICAL_TS_CODE, adj_factor=1.0),
            ]
        )
        assert len(
            normalize_historical_aliases(
                df, endpoint="adj_factor", trade_date=TRADE_DAY
            )
        ) == 1

    def test_dash_and_empty_string_are_empty(self):
        df = pd.DataFrame(
            [
                daily_row(LEGACY_TS_CODE, ah_amount="-"),
                daily_row(CANONICAL_TS_CODE, ah_amount=""),
            ]
        )
        assert len(
            normalize_historical_aliases(df, endpoint="daily", trade_date=TRADE_DAY)
        ) == 1

    def test_numpy_scalar_nan_counts_as_empty(self):
        """numpy 标量的 NaN/NaT 也判为空——``value != value`` 返回的是
        ``np.bool_`` 而非 Python ``bool``，早期实现因此漏判。"""
        import numpy as np

        from app.providers.history.tushare_aliases import _is_missing

        assert _is_missing(np.float64("nan")) is True
        assert _is_missing(np.datetime64("NaT", "us")) is True
        assert _is_missing(np.float64(1.0)) is False
        # 不可比较对象（如数组）不得抛异常
        assert _is_missing(np.array([1.0, 2.0])) is False

    def test_numpy_nan_and_none_do_not_conflict(self):
        """一行是 numpy NaN、另一行是 None：都只是缺值，不得判为冲突。"""
        import numpy as np

        df = pd.DataFrame(
            [
                daily_row(LEGACY_TS_CODE, ah_vol=np.float64("nan")),
                daily_row(CANONICAL_TS_CODE, ah_vol=None),
            ]
        )
        assert len(
            normalize_historical_aliases(df, endpoint="daily", trade_date=TRADE_DAY)
        ) == 1

    def test_oversized_integer_does_not_leak_raw_overflow_error(self):
        """超大整数（10**400）不得让裸 OverflowError 穿透 TushareError 体系。

        这类脏值无法转 float；比较按"不能证明相等即不等"处理，但异常类型
        必须仍属领域异常（否则同步重试编排无法归类）。
        """
        from app.providers.history.tushare_aliases import _values_equal

        huge = 10 ** 400
        assert _values_equal(huge, huge) is True
        assert _values_equal(huge, 10 ** 400 - 1) is False
        # 与普通数值比较同样不得抛异常
        assert _values_equal(huge, 1.0) is False

    def test_conflict_between_oversized_integers_is_a_domain_error(self):
        """两组超大整数不一致时，抛的是 ALIAS_CONFLICT 而不是裸 OverflowError。

        直接用 ``_rows_equal`` 探测（pandas 构造 DataFrame 时会先行溢出，
        构造不出这样的帧；真实触发点是 object dtype 列里的脏值）。
        """
        from app.providers.history.tushare_aliases import _rows_equal

        assert _rows_equal({"vol": 10 ** 400}, {"vol": 10 ** 400}) is True
        assert _rows_equal({"vol": 10 ** 400}, {"vol": 10 ** 400 - 1}) is False

    def test_float_serialization_tail_difference_is_absorbed(self):
        """浮点序列化尾差（12.3 vs 12.300000000000001）必须被容差吸收。

        没有这条用例时把 ``_VALUE_TOLERANCE`` 改成 0 也能全绿——上游尾差
        会变成假 ALIAS_CONFLICT 并卡住水位。
        """
        from app.providers.history.tushare_aliases import _values_equal

        assert _values_equal(12.3, 12.300000000000001) is True
        # 容差是相对量级 1e-9，真实差异不得被吸收
        assert _values_equal(10.0, 10.01) is False

    def test_materially_different_value_still_conflicts(self):
        """容差只吸收浮点尾差，不吸收真实差异。"""
        df = pd.DataFrame(
            [
                daily_row(LEGACY_TS_CODE, close=10.0),
                daily_row(CANONICAL_TS_CODE, close=10.01),
            ]
        )
        with pytest.raises(HistoricalAliasConflictError):
            normalize_historical_aliases(df, endpoint="daily", trade_date=TRADE_DAY)

    def test_trade_date_difference_conflicts(self):
        df = pd.DataFrame(
            [
                daily_row(LEGACY_TS_CODE, trade_date="20100104"),
                daily_row(CANONICAL_TS_CODE, trade_date="20100105"),
            ]
        )
        with pytest.raises(HistoricalAliasConflictError):
            normalize_historical_aliases(df, endpoint="daily", trade_date=TRADE_DAY)


# ================================================================
# 二、Provider 主路径（四个日级数据集）
# ================================================================


@pytest.mark.parametrize(
    ("dataset", "endpoint", "fields", "row_factory", "main_method", "fallback_method"),
    DATASETS,
    ids=DATASET_IDS,
)
class TestProviderAliasNormalization:
    """§13 Test 1/2/3/5：四个数据集行为必须一致。"""

    def test_legacy_only_maps_to_canonical_instrument(
        self, dataset, endpoint, fields, row_factory, main_method, fallback_method
    ):
        """§13 Test 1：只有旧代码 → 不抛 UNKNOWN_INSTRUMENT，落到新代码证券。"""
        client = FakeTushareClient()
        client.add(endpoint, pd.DataFrame([row_factory(LEGACY_TS_CODE)]))
        batch = getattr(_provider(client), main_method)(
            TRADE_DAY, [ZHAOSHANG, PINGAN]
        )

        assert len(batch.records) == 1
        record = batch.records[0]
        assert record.ts_code == CANONICAL_TS_CODE
        assert record.instrument_id == "CN:STOCK:001872"

    def test_legacy_and_canonical_identical_yields_one_row(
        self, dataset, endpoint, fields, row_factory, main_method, fallback_method
    ):
        """§13 Test 2：新旧并存且一致 → 1 行，保留规范代码。"""
        client = FakeTushareClient()
        client.add(
            endpoint,
            pd.DataFrame(
                [row_factory(LEGACY_TS_CODE), row_factory(CANONICAL_TS_CODE)]
            ),
        )
        batch = getattr(_provider(client), main_method)(
            TRADE_DAY, [ZHAOSHANG, PINGAN]
        )

        assert len(batch.records) == 1
        assert batch.records[0].ts_code == CANONICAL_TS_CODE
        assert batch.records[0].instrument_id == "CN:STOCK:001872"
        # raw_row_count 记录上游真实返回行数（规范化前），监控口径不变
        assert batch.raw_row_count == 2

    def test_legacy_and_canonical_conflicting_raises_alias_conflict(
        self, dataset, endpoint, fields, row_factory, main_method, fallback_method
    ):
        """§13 Test 3：新旧并存但冲突 → ALIAS_CONFLICT，不产出任何记录。"""
        changes = {
            "daily": {"close": 99.0},
            "adj_factor": {"adj_factor": 2.0},
            "daily_basic": {"turnover_rate": 88.0},
            "moneyflow": {"buy_sm_vol": 999},
        }[dataset]
        client = FakeTushareClient()
        client.add(
            endpoint,
            pd.DataFrame(
                [
                    row_factory(LEGACY_TS_CODE),
                    row_factory(CANONICAL_TS_CODE, **changes),
                ]
            ),
        )
        with pytest.raises(HistoricalAliasConflictError) as exc_info:
            getattr(_provider(client), main_method)(TRADE_DAY, [ZHAOSHANG, PINGAN])
        assert exc_info.value.error_code == "ALIAS_CONFLICT"

    def test_unregistered_unknown_code_still_rejected(
        self, dataset, endpoint, fields, row_factory, main_method, fallback_method
    ):
        """§13 Test 4 / §15.5：未登记别名的未知证券仍然 UNKNOWN_INSTRUMENT。"""
        client = FakeTushareClient()
        client.add(endpoint, pd.DataFrame([row_factory("999999.SZ")]))
        with pytest.raises(UnknownInstrumentError) as exc_info:
            getattr(_provider(client), main_method)(TRADE_DAY, [ZHAOSHANG, PINGAN])
        assert exc_info.value.error_code == "UNKNOWN_INSTRUMENT"

    def test_other_instruments_unaffected(
        self, dataset, endpoint, fields, row_factory, main_method, fallback_method
    ):
        """§13 Test 7：普通证券映射不受影响。"""
        client = FakeTushareClient()
        client.add(
            endpoint,
            pd.DataFrame([row_factory("000001.SZ"), row_factory("600519.SH")]),
        )
        batch = getattr(_provider(client), main_method)(
            TRADE_DAY, [ZHAOSHANG, PINGAN, MAOTAI]
        )
        assert {r.instrument_id for r in batch.records} == {
            "CN:STOCK:000001", "CN:STOCK:600519",
        }
        assert {r.ts_code for r in batch.records} == {"000001.SZ", "600519.SH"}


# ================================================================
# 三、Provider 截断 fallback 路径（§11.2）
# ================================================================


@pytest.mark.parametrize(
    ("dataset", "endpoint", "fields", "row_factory", "main_method", "fallback_method"),
    DATASETS,
    ids=DATASET_IDS,
)
class TestFallbackPathAliasNormalization:
    """fallback 与主路径必须走同一规范化口径，否则身份一致性会在补齐时破功。"""

    def test_fallback_rewrites_legacy_response(
        self, dataset, endpoint, fields, row_factory, main_method, fallback_method
    ):
        """逐只请求 001872.SZ，上游仍回旧代码时也要落到同一 instrument。"""
        client = FakeTushareClient()

        def respond(**params):
            requested = params.get("ts_code", CANONICAL_TS_CODE)
            code = LEGACY_TS_CODE if requested == CANONICAL_TS_CODE else requested
            return pd.DataFrame([row_factory(code)])

        client.add(endpoint, respond)
        provider = _provider(client)
        if dataset == "daily_basic":
            batch = provider.get_daily_basic_for_instruments(
                TRADE_DAY, [ZHAOSHANG], missing_ts_codes=[CANONICAL_TS_CODE]
            )
        else:
            batch = getattr(provider, fallback_method)(TRADE_DAY, [ZHAOSHANG])

        assert len(batch.records) == 1
        assert batch.records[0].ts_code == CANONICAL_TS_CODE
        assert batch.records[0].instrument_id == "CN:STOCK:001872"

    def test_fallback_conflict_still_raises(
        self, dataset, endpoint, fields, row_factory, main_method, fallback_method
    ):
        changes = {
            "daily": {"close": 99.0},
            "adj_factor": {"adj_factor": 2.0},
            "daily_basic": {"turnover_rate": 88.0},
            "moneyflow": {"buy_sm_vol": 999},
        }[dataset]
        client = FakeTushareClient()
        client.add(
            endpoint,
            pd.DataFrame(
                [
                    row_factory(LEGACY_TS_CODE),
                    row_factory(CANONICAL_TS_CODE, **changes),
                ]
            ),
        )
        provider = _provider(client)
        with pytest.raises(HistoricalAliasConflictError):
            if dataset == "daily_basic":
                provider.get_daily_basic_for_instruments(
                    TRADE_DAY, [ZHAOSHANG], missing_ts_codes=[CANONICAL_TS_CODE]
                )
            else:
                getattr(provider, fallback_method)(TRADE_DAY, [ZHAOSHANG])


# ================================================================
# 四、不退化：严格保护与重复键校验仍在
# ================================================================


class TestStrictProtectionsRetained:
    def test_duplicate_key_check_still_rejects_plain_duplicates(self):
        """§13 Test 8：非 alias 的重复行仍由 DUPLICATE_KEY 拦截。"""
        from app.services.history.validation import (
            DuplicateKeyError,
            validate_batch,
        )

        client = FakeTushareClient()
        client.add(
            "daily",
            pd.DataFrame([daily_row("000001.SZ"), daily_row("000001.SZ")]),
        )
        batch = _provider(client).get_daily(TRADE_DAY, [PINGAN])
        with pytest.raises(DuplicateKeyError):
            validate_batch("daily", batch, trade_date=TRADE_DAY)

    def test_known_instrument_guard_still_enforced(self):
        """§9.3：known_instrument_ids 校验不得因本修复被关闭。"""
        from app.services.history.validation import (
            HistoryUnknownInstrumentError,
            validate_batch,
        )

        client = FakeTushareClient()
        client.add("daily", pd.DataFrame([daily_row("000001.SZ")]))
        batch = _provider(client).get_daily(TRADE_DAY, [PINGAN])
        with pytest.raises(HistoryUnknownInstrumentError):
            validate_batch(
                "daily", batch, trade_date=TRADE_DAY, known_instrument_ids=set()
            )

    def test_alias_conflict_is_a_config_error_fast_fail(self):
        """ALIAS_CONFLICT 不会随重试自愈：必须快速终态失败，不做 10 轮退避。"""
        from app.services.history.retry import is_config_error

        assert is_config_error("ALIAS_CONFLICT") is True

    def test_stock_basic_is_not_alias_normalized(self):
        """主档是 instrument 的来源，别名层不得改写主档（§9.5）。

        否则 stock_basic 会新增一条 CN:STOCK:001872 的重复主档行。
        """
        from app.providers.history.tushare import STOCK_BASIC_FIELDS

        client = FakeTushareClient()
        client.add(
            "stock_basic",
            lambda **params: pd.DataFrame(
                [
                    {
                        "ts_code": LEGACY_TS_CODE, "symbol": "000022",
                        "name": "深赤湾A", "exchange": params.get("exchange", "SZSE"),
                        "list_status": params.get("list_status", "D"),
                    }
                ]
            )
            if params.get("exchange") == "SZSE" and params.get("list_status") == "D"
            else pd.DataFrame(columns=list(STOCK_BASIC_FIELDS)),
        )
        batch = _provider(client).get_stock_basic()
        # 主档原样交付，由 Service 按 list_status 处置（不在此处改写代码）
        assert [r.ts_code for r in batch.records] == [LEGACY_TS_CODE]
        assert [r.instrument_id for r in batch.records] == ["CN:STOCK:000022"]
