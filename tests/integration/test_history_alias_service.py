"""ts_code 别名修复的端到端集成测试（真实 Tushare Provider + 真实 DuckDB）。

与 ``tests/unit/test_history_ts_code_alias.py``（Provider 边界单测）互补：
这里把**真实** ``TushareHistoricalMarketDataProvider`` 接进
``HistorySyncService``，走完整的"抓取 → 别名规范化 → 校验（含
``known_instrument_ids`` 主档保护）→ 单日原子提交 → 水位推进"链路，验证修复
在业务层真的生效，而不只是在 Provider 单测里成立。

覆盖：

- 旧代码只出现在事实数据里时，历史交易日能被正常提交、水位推进，落库的是
  规范 instrument，且不新建 ``CN:STOCK:000022`` 假证券；
- ``ALIAS_CONFLICT`` 属配置类错误 → 一次尝试即终态失败、水位不推进；
- 一个数据集冲突不影响其余数据集推进（design D17 互不阻塞）；
- ``daily_basic`` 截断补齐的候选集差额不含规范代码，不会重复请求（§11.3）。

Tushare SDK 由 fake client 替代——本文件不访问网络。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import select

from app.config import AppConfig, TushareConfig
from app.models.history_sync import DatasetName, DatasetStatus, TriggerType
from app.providers.history import HistoryProviderRegistry
from app.providers.history.tushare import (
    STOCK_BASIC_FIELDS,
    STOCK_COMPANY_FIELDS,
    TushareHistoricalMarketDataProvider,
)
from app.providers.trading_calendar.provider import CalendarDayRecord
from app.providers.tushare_common import TushareRequestGate, TushareTransport
from app.repositories.history_fact import HistoryFactRepository
from app.repositories.history_master import HistoryMasterRepository
from app.repositories.history_sync import HistorySyncStateRepository
from app.services.history.sync_service import HistorySyncService

BEIJING = ZoneInfo("Asia/Shanghai")

LEGACY_DAY = date(2010, 1, 4)
NEXT_DAY = date(2010, 1, 5)
OPEN_DAYS = [LEGACY_DAY, NEXT_DAY]
FROZEN_NOW = datetime(2010, 1, 5, 21, 0, tzinfo=BEIJING)

LEGACY_TS_CODE = "000022.SZ"       # 深赤湾A（2018-12-26 前的代码）
CANONICAL_TS_CODE = "001872.SZ"    # 招商港口（今天的代码）
CANONICAL_SYMBOL = "001872"
LEGACY_SYMBOL = "000022"
OTHER_SYMBOL = "000001"

DAY_LEVEL_DATASETS = (
    DatasetName.DAILY,
    DatasetName.ADJ_FACTOR,
    DatasetName.DAILY_BASIC,
    DatasetName.MONEYFLOW,
)


class FakeCalendarProvider:
    """严格日历 fake：只需 ``get_days(..., strict=True)`` 契约。"""

    def __init__(self):
        self.calls: list[tuple[str, date, date]] = []

    def get_days(self, market, start, end, *, strict=False):
        self.calls.append((market, start, end))
        return [
            CalendarDayRecord(trade_date=day, is_open=day in OPEN_DAYS)
            for day in OPEN_DAYS
            if start <= day <= end
        ]


class FakeTushareClient:
    """按 endpoint 预置响应的 fake SDK client。

    ``stock_basic`` 只返回**今天的**代码——旧代码在任何 exchange ×
    list_status 分片里都不存在，与真实情况一致（这正是问题成因）。
    ``daily`` 同时返回新旧两行（模拟上游同时给出两套代码）。
    """

    def __init__(self, *, daily_conflict: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.daily_conflict = daily_conflict

    # -- 主档 --

    def stock_basic(self, **params) -> pd.DataFrame:
        self.calls.append(("stock_basic", params))
        if params.get("exchange") == "SZSE" and params.get("list_status") == "L":
            return pd.DataFrame(
                [
                    {"ts_code": CANONICAL_TS_CODE, "symbol": CANONICAL_SYMBOL,
                     "name": "招商港口", "exchange": "SZSE", "list_status": "L",
                     "list_date": "19930101"},
                    {"ts_code": f"{OTHER_SYMBOL}.SZ", "symbol": OTHER_SYMBOL,
                     "name": "平安银行", "exchange": "SZSE", "list_status": "L",
                     "list_date": "19910403"},
                ]
            )
        return pd.DataFrame(columns=list(STOCK_BASIC_FIELDS))

    def stock_company(self, **params) -> pd.DataFrame:
        self.calls.append(("stock_company", params))
        return pd.DataFrame(columns=list(STOCK_COMPANY_FIELDS))

    def namechange(self, **params) -> pd.DataFrame:
        self.calls.append(("namechange", params))
        return pd.DataFrame(
            [
                {"ts_code": params.get("ts_code", CANONICAL_TS_CODE),
                 "name": "招商港口", "start_date": "20181226",
                 "end_date": None, "ann_date": "20181226",
                 "change_reason": "证券简称变更"},
            ]
        )

    # -- 日级事实 --

    def daily(self, **params) -> pd.DataFrame:
        self.calls.append(("daily", params))
        day = params["trade_date"]
        legacy_close = 12.3
        canonical_close = 99.0 if self.daily_conflict else 12.3
        return pd.DataFrame(
            [
                _daily_row(LEGACY_TS_CODE, day, close=legacy_close),
                _daily_row(CANONICAL_TS_CODE, day, close=canonical_close),
            ]
        )

    def adj_factor(self, **params) -> pd.DataFrame:
        self.calls.append(("adj_factor", params))
        day = params["trade_date"]
        return pd.DataFrame(
            [
                {"ts_code": LEGACY_TS_CODE, "trade_date": day, "adj_factor": 1.0},
                {"ts_code": CANONICAL_TS_CODE, "trade_date": day, "adj_factor": 1.0},
            ]
        )

    def daily_basic(self, **params) -> pd.DataFrame:
        self.calls.append(("daily_basic", params))
        day = params["trade_date"]
        requested = params.get("ts_code")
        if requested == CANONICAL_TS_CODE:
            # 上游对历史日期即使被问规范代码，仍可能回旧代码
            codes = [LEGACY_TS_CODE]
        elif requested:
            codes = [requested]
        else:
            codes = [LEGACY_TS_CODE, CANONICAL_TS_CODE]
        return pd.DataFrame([_daily_basic_row(code, day) for code in codes])

    def moneyflow(self, **params) -> pd.DataFrame:
        self.calls.append(("moneyflow", params))
        day = params["trade_date"]
        return pd.DataFrame(
            [
                _moneyflow_row(LEGACY_TS_CODE, day),
                _moneyflow_row(CANONICAL_TS_CODE, day),
            ]
        )

    def __getattr__(self, endpoint: str):
        def call(**params):
            self.calls.append((endpoint, params))
            return pd.DataFrame()

        return call


def _daily_row(ts_code: str, day: str, *, close: float) -> dict:
    return {
        "ts_code": ts_code, "trade_date": day,
        "open": 12.0, "high": 12.5, "low": 11.8, "close": close,
        "pre_close": 12.0, "change": 0.3, "pct_chg": 2.5,
        "vol": 12345.0, "amount": 15200.0,
        "ah_vol": None, "ah_amount": None,
    }


def _daily_basic_row(ts_code: str, day: str) -> dict:
    return {
        "ts_code": ts_code, "trade_date": day, "close": 12.3,
        "turnover_rate": 1.1, "turnover_rate_f": 1.2, "volume_ratio": 0.9,
        "pe": 15.0, "pe_ttm": 14.0, "pb": 1.5, "ps": 2.0, "ps_ttm": 2.1,
        "dv_ratio": 0.5, "dv_ttm": 0.6, "total_share": 100.0,
        "float_share": 80.0, "free_share": 70.0, "total_mv": 1230.0,
        "circ_mv": 984.0, "limit_status": 0,
    }


def _moneyflow_row(ts_code: str, day: str) -> dict:
    return {
        "ts_code": ts_code, "trade_date": day,
        "buy_sm_vol": 10, "buy_sm_amount": 1.0, "sell_sm_vol": 20,
        "sell_sm_amount": 2.0, "buy_md_vol": 30, "buy_md_amount": 3.0,
        "sell_md_vol": 40, "sell_md_amount": 4.0, "buy_lg_vol": 50,
        "buy_lg_amount": 5.0, "sell_lg_vol": 60, "sell_lg_amount": 6.0,
        "buy_elg_vol": 70, "buy_elg_amount": 7.0, "sell_elg_vol": 80,
        "sell_elg_amount": 8.0, "net_mf_vol": -90, "net_mf_amount": -9.0,
    }


def _build_provider(client: FakeTushareClient) -> TushareHistoricalMarketDataProvider:
    config = AppConfig(tushare=TushareConfig(token="fake-token"))
    transport = TushareTransport(
        config, gate=TushareRequestGate(0), client_factory=lambda _c: client
    )
    return TushareHistoricalMarketDataProvider(config, transport=transport)


class SpyRegistry(HistoryProviderRegistry):
    """记录 ``get_daily_basic_for_instruments`` 收到的缺失代码。"""

    def __init__(self, client: FakeTushareClient):
        config = AppConfig(tushare=TushareConfig(token="fake-token"))
        super().__init__(config, provider=_build_provider(client))
        self.missing_calls: list[list[str] | None] = []

    def get_daily_basic_for_instruments(
        self, trade_date, instruments, *, missing_ts_codes=None
    ):
        self.missing_calls.append(missing_ts_codes)
        return super().get_daily_basic_for_instruments(
            trade_date, instruments, missing_ts_codes=missing_ts_codes
        )


@pytest.fixture()
def frozen_now(monkeypatch):
    """固定"现在"为 2010-01-05 21:00（四个数据集 cutoff 均已过）。"""

    def _now():
        return FROZEN_NOW

    monkeypatch.setattr("app.services.history.sync_service.now_beijing", _now)
    return FROZEN_NOW


@pytest.fixture()
def make_service(session_factory, frozen_now):
    def _make(client: FakeTushareClient, *, registry=None) -> HistorySyncService:
        config = AppConfig(tushare=TushareConfig(token="fake-token"))
        config.history.start_date = LEGACY_DAY
        config.history.max_attempts = 3
        return HistorySyncService(
            config,
            session_factory,
            registry if registry is not None else SpyRegistry(client),
            FakeCalendarProvider(),
            sleep=lambda _seconds: None,
            random_fn=lambda: 0.5,
        )

    return _make


def _state(session_factory, dataset: DatasetName):
    with session_factory() as session:
        return HistorySyncStateRepository(session).get(dataset)


def _fact_rows(session_factory, dataset: DatasetName, trade_date: date) -> int:
    with session_factory() as session:
        return HistoryFactRepository(session).count_for_date(dataset, trade_date)


def _fact_ts_codes(session_factory, dataset: DatasetName, trade_date: date) -> set[str]:
    from app.models.history_fact import HISTORY_FACT_TABLES

    with session_factory() as session:
        table = HISTORY_FACT_TABLES[dataset.value]
        return set(
            session.execute(
                select(table.c.ts_code).where(table.c.trade_date == trade_date)
            ).scalars()
        )


def _master_ids(session_factory) -> set[str]:
    with session_factory() as session:
        return {
            inst.instrument_id
            for inst in HistoryMasterRepository(session).list_cn_stock_instruments()
        }


# ---- 主场景：旧代码不再阻塞历史回填 ----


class TestLegacyCodeBackfill:
    def test_all_day_level_datasets_advance_watermark(
        self, make_service, session_factory
    ):
        """§15.1/§15.2：旧代码出现不再让日级数据集失败，水位正常推进。"""
        service = make_service(FakeTushareClient())
        service.run(trigger=TriggerType.MANUAL)

        for dataset in DAY_LEVEL_DATASETS:
            state = _state(session_factory, dataset)
            assert state.last_error_code is None, (
                f"{dataset.value} 不应因旧代码失败，实际 {state.last_error_code}: "
                f"{state.last_error}"
            )
            assert state.latest_complete_trade_date == NEXT_DAY, (
                f"{dataset.value} 水位应为 {NEXT_DAY}，实际 "
                f"{state.latest_complete_trade_date}"
            )

    def test_facts_stored_under_canonical_ts_code(
        self, make_service, session_factory
    ):
        """§11.3：落库 ts_code 是规范代码，旧代码不得进入事实表。"""
        service = make_service(FakeTushareClient())
        service.run(trigger=TriggerType.MANUAL)

        for dataset in DAY_LEVEL_DATASETS:
            codes = _fact_ts_codes(session_factory, dataset, LEGACY_DAY)
            assert CANONICAL_TS_CODE in codes, f"{dataset.value} 缺少规范证券"
            assert LEGACY_TS_CODE not in codes, (
                f"{dataset.value} 把旧代码写进了事实表: {codes}"
            )

    def test_no_duplicate_fact_rows_after_merge(self, make_service, session_factory):
        """§15.4：新旧两行合并后，单证券单日只有一条事实记录。"""
        service = make_service(FakeTushareClient())
        service.run(trigger=TriggerType.MANUAL)

        # 该日主档只有 001872（000001 未出现在事实数据里），合并后应为 1 行
        for dataset in DAY_LEVEL_DATASETS:
            assert _fact_rows(session_factory, dataset, LEGACY_DAY) == 1, (
                f"{dataset.value} 出现重复事实键"
            )
        # 连续两个交易日都写入，record_count 不因合并而少算
        assert _state(session_factory, DatasetName.DAILY).record_count == 2

    def test_no_placeholder_instrument_created(self, make_service, session_factory):
        """§15.3：不得为旧代码创建 CN:STOCK:000022 假证券。"""
        service = make_service(FakeTushareClient())
        service.run(trigger=TriggerType.MANUAL)

        ids = _master_ids(session_factory)
        assert f"CN:STOCK:{CANONICAL_SYMBOL}" in ids
        assert f"CN:STOCK:{LEGACY_SYMBOL}" not in ids, "不得创建假历史证券"


# ---- 冲突：一次尝试即终态失败，水位不推进 ----


class TestAliasConflictTerminalFailure:
    def test_conflict_fails_fast_without_advancing(
        self, make_service, session_factory
    ):
        """同一证券同一天两种取值 → ALIAS_CONFLICT，不提交、不推进水位。"""
        client = FakeTushareClient(daily_conflict=True)
        service = make_service(client)
        run_id = service.run(trigger=TriggerType.MANUAL)

        state = _state(session_factory, DatasetName.DAILY)
        assert state.status == DatasetStatus.FAILED.value
        assert state.last_error_code == "ALIAS_CONFLICT"
        assert state.latest_complete_trade_date is None, "冲突日不得推进水位"
        assert state.current_trade_date == LEGACY_DAY
        assert _fact_rows(session_factory, DatasetName.DAILY, LEGACY_DAY) == 0

        # 配置类错误：只尝试一次，不睡满 max_attempts 轮退避
        daily_calls = [params for name, params in client.calls if name == "daily"]
        assert len(daily_calls) == 1, (
            f"ALIAS_CONFLICT 应快速失败，实际请求 {len(daily_calls)} 次"
        )
        assert run_id

    def test_conflict_is_recorded_on_run_dataset(
        self, make_service, session_factory
    ):
        """运行记录（/admin/data 的"最近执行记录"）必须暴露冲突与失败日。"""
        from app.repositories.history_sync import HistorySyncRunDatasetRepository

        service = make_service(FakeTushareClient(daily_conflict=True))
        run_id = service.run(trigger=TriggerType.MANUAL)

        with session_factory() as session:
            row = HistorySyncRunDatasetRepository(session).get(
                run_id, DatasetName.DAILY
            )
        assert row.last_error_code == "ALIAS_CONFLICT"
        assert row.failed_trade_date == LEGACY_DAY

    def test_conflict_does_not_block_other_datasets(
        self, make_service, session_factory
    ):
        """数据集互不阻塞（design D17）：daily 冲突不影响其余三个数据集。"""
        service = make_service(FakeTushareClient(daily_conflict=True))
        service.run(trigger=TriggerType.MANUAL)

        assert _state(session_factory, DatasetName.DAILY).status == (
            DatasetStatus.FAILED.value
        )
        for dataset in (DatasetName.ADJ_FACTOR, DatasetName.DAILY_BASIC,
                        DatasetName.MONEYFLOW):
            assert _state(
                session_factory, dataset
            ).latest_complete_trade_date == NEXT_DAY, (
                f"{dataset.value} 不应被 daily 的冲突拖累"
            )


# ---- §11.3：daily_basic 候选集补齐不重复请求规范证券 ----


class TestDailyBasicCandidateSet:
    """§11.3：候选集差额用的是 Provider **输出**的 ts_code。

    Service 计算 ``候选集 - 已返回代码`` 时两边都必须是规范代码：否则
    ``returned`` 里是 000022.SZ、候选集里是 001872.SZ，规范证券会被判为
    缺失而重复请求一次。
    """

    def _run_to_master(self, make_service, registry, client) -> list:
        """先跑一轮让主档落库，再取主档 instruments 直接调 Provider。"""
        service = make_service(client, registry=registry)
        service.run(trigger=TriggerType.MANUAL)
        with service.session_factory() as session:
            return HistoryMasterRepository(session).list_cn_stock_instruments()

    def test_canonical_code_is_not_treated_as_missing(
        self, make_service, session_factory
    ):
        client = FakeTushareClient()
        registry = SpyRegistry(client)
        master = self._run_to_master(make_service, registry, client)

        primary = registry.provider.get_daily_basic(LEGACY_DAY, master)
        returned = {record.ts_code for record in primary.records}
        assert CANONICAL_TS_CODE in returned
        assert LEGACY_TS_CODE not in returned

        with session_factory() as session:
            candidates = set(
                HistoryMasterRepository(session).list_ts_codes_tradable_on(LEGACY_DAY)
            )
        assert CANONICAL_TS_CODE in candidates
        assert CANONICAL_TS_CODE not in sorted(candidates - returned), (
            "规范证券被误判为缺失，会被重复请求"
        )

    def test_daily_basic_primary_batch_is_a_single_record_per_instrument(
        self, make_service, session_factory
    ):
        """新旧两行合并后，Provider 只交付一条记录（不产生第二个 instrument）。"""
        client = FakeTushareClient()
        registry = SpyRegistry(client)
        master = self._run_to_master(make_service, registry, client)

        primary = registry.provider.get_daily_basic(LEGACY_DAY, master)
        assert len(primary.records) == 1
        assert primary.raw_row_count == 2, "原始行数仍记录上游真实规模"
        assert primary.records[0].instrument_id == f"CN:STOCK:{CANONICAL_SYMBOL}"

    def test_fallback_querying_canonical_code_yields_canonical_record(
        self, make_service, session_factory
    ):
        """逐只补齐请求 001872.SZ，上游回旧代码时仍归到规范 instrument。"""
        client = FakeTushareClient()
        registry = SpyRegistry(client)
        master = self._run_to_master(make_service, registry, client)

        batch = registry.provider.get_daily_basic_for_instruments(
            LEGACY_DAY, master, missing_ts_codes=[CANONICAL_TS_CODE]
        )
        assert len(batch.records) == 1
        assert batch.records[0].instrument_id == f"CN:STOCK:{CANONICAL_SYMBOL}"
        assert batch.records[0].ts_code == CANONICAL_TS_CODE
        # 逐只补齐确实按规范代码发起请求（不是旧代码）
        per_ts_code = [
            params for name, params in client.calls
            if name == "daily_basic" and params.get("ts_code")
        ]
        assert [params["ts_code"] for params in per_ts_code] == [CANONICAL_TS_CODE]

    def test_candidate_missing_diff_is_computed_on_canonical_codes(
        self, make_service, session_factory, monkeypatch
    ):
        """端到端：真实截断触发补齐，缺失集合按**规范代码**计算。

        把行数上限压低到 2，主路径的 daily_basic（新旧两行，规范化后合并为
        一条规范记录）即被判定为截断，从而真正驱动 ``Service._daily_basic_fallback``。
        断言传入补齐路径的缺失集合不含规范代码——若 Provider 输出的 ts_code
        仍是旧代码，``候选集 - 已返回`` 会把 001872.SZ 算成缺失并重复请求。
        """
        monkeypatch.setattr(
            "app.providers.history.tushare.DAILY_ROW_CAP", 2, raising=True
        )
        client = FakeTushareClient()
        registry = SpyRegistry(client)
        service = make_service(client, registry=registry)
        service.run(trigger=TriggerType.MANUAL)

        # 截断必须真的发生过，否则本用例退化为恒真
        assert registry.missing_calls, (
            "补齐路径未被驱动（截断未触发），用例失效"
        )
        for missing in registry.missing_calls:
            if missing is None:
                continue
            assert CANONICAL_TS_CODE not in missing, (
                f"规范证券被误判为缺失、会被重复请求: {missing}"
            )
        # 旧代码也不该出现在缺失集合里（它不是候选集成员）
        for missing in registry.missing_calls:
            if missing:
                assert LEGACY_TS_CODE not in missing
