"""ts_code 别名修复的端到端集成测试（真实 Tushare Provider + 真实 DuckDB）。

与 ``tests/unit/test_history_ts_code_alias.py``（Provider 边界单测）互补：
这里把**真实** ``TushareHistoricalMarketDataProvider`` 接进
``HistorySyncService``，走完整的"抓取 → 别名规范化 → 校验 → 单股区间原子
提交 → 水位推进"链路（per-stock-history-sync 个股口径，tasks 7.3），验证
修复在业务层真的生效，而不只是在 Provider 单测里成立。

覆盖：

- 旧代码股区间回填落规范码并推进水位（个股水位，非数据集级水位）
- ALIAS_CONFLICT 只失败该股不阻塞数据集（其他股正常、数据集 LAGGING、Run SUCCESS）
- UNKNOWN_INSTRUMENT 拒绝不建占位证券（instrument 表断言无新增）

Tushare SDK 由 fake client 替代——本文件不访问网络。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import select

from app.config import AppConfig, TushareConfig
from app.models.history_fact import HISTORY_FACT_TABLES
from app.models.history_sync import (
    DatasetName,
    DatasetStatus,
    RunDatasetStatus,
    RunStatus,
    TASK_STATUS_FAILED,
    TASK_STATUS_SUCCESS,
    TriggerType,
)
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
from app.repositories.history_sync import (
    HistorySyncRunDatasetRepository,
    HistorySyncRunRepository,
    HistorySyncStateRepository,
    StockSyncStateRepository,
)
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
CANONICAL_INSTRUMENT_ID = f"CN:STOCK:{CANONICAL_SYMBOL}"
OTHER_INSTRUMENT_ID = f"CN:STOCK:{OTHER_SYMBOL}"

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

    同时支持两种调用形态：
    - 单日：``endpoint(trade_date="YYYYMMDD", ...)`` —— 旧路径/主档用
    - 区间：``endpoint(ts_code="XXX.SZ", start_date="YYYYMMDD", end_date="YYYYMMDD")``
      —— 个股路径 ``get_history_by_stock`` 用

    ``stock_basic`` 只返回**今天的**代码——旧代码在任何 exchange ×
    list_status 分片里都不存在，与真实情况一致（这正是问题成因）。
    ``daily`` 同时返回新旧两行（模拟上游同时给出两套代码，别名层负责合并/冲突检测）。
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

    # -- 日级事实（同时支持单日与区间调用） --

    def _range_dates(self, params: dict) -> list[date]:
        """从 params 中解析日期范围，返回 date 列表。

        - 有 ``trade_date`` → 单日
        - 有 ``start_date`` + ``end_date`` → 区间（含端点）
        """
        if "trade_date" in params:
            return [pd.Timestamp(params["trade_date"]).date()]
        if "start_date" in params and "end_date" in params:
            start = pd.Timestamp(params["start_date"]).date()
            end = pd.Timestamp(params["end_date"]).date()
            dates = []
            d = start
            while d <= end:
                dates.append(d)
                d += timedelta(days=1)
            return dates
        return []

    def _codes_for_request(self, params: dict) -> list[str]:
        """根据请求参数确定返回哪些 ts_code 的行。

        规则：
        - 没指定 ts_code（全市场单日）→ 返回旧码 + 新码 + 000001.SZ
        - 指定规范码 001872.SZ → 返回旧码 000022.SZ + 规范码 001872.SZ（模拟上游回旧代码）
        - 指定其他代码 → 返回该代码自己的一行
        """
        requested = params.get("ts_code")
        if requested is None:
            return [LEGACY_TS_CODE, CANONICAL_TS_CODE, f"{OTHER_SYMBOL}.SZ"]
        if requested == CANONICAL_TS_CODE:
            # 上游对历史日期即使被问规范代码，仍可能回旧代码
            return [LEGACY_TS_CODE, CANONICAL_TS_CODE]
        return [requested]

    def daily(self, **params) -> pd.DataFrame:
        self.calls.append(("daily", params))
        dates = self._range_dates(params)
        codes = self._codes_for_request(params)
        rows = []
        for d in dates:
            day_str = d.strftime("%Y%m%d")
            for code in codes:
                if code == CANONICAL_TS_CODE and self.daily_conflict:
                    rows.append(_daily_row(code, day_str, close=99.0))
                else:
                    rows.append(_daily_row(code, day_str, close=12.3))
        return pd.DataFrame(rows)

    def adj_factor(self, **params) -> pd.DataFrame:
        self.calls.append(("adj_factor", params))
        dates = self._range_dates(params)
        codes = self._codes_for_request(params)
        rows = []
        for d in dates:
            day_str = d.strftime("%Y%m%d")
            for code in codes:
                rows.append({"ts_code": code, "trade_date": day_str, "adj_factor": 1.0})
        return pd.DataFrame(rows)

    def daily_basic(self, **params) -> pd.DataFrame:
        self.calls.append(("daily_basic", params))
        dates = self._range_dates(params)
        codes = self._codes_for_request(params)
        rows = []
        for d in dates:
            day_str = d.strftime("%Y%m%d")
            for code in codes:
                rows.append(_daily_basic_row(code, day_str))
        return pd.DataFrame(rows)

    def moneyflow(self, **params) -> pd.DataFrame:
        self.calls.append(("moneyflow", params))
        dates = self._range_dates(params)
        codes = self._codes_for_request(params)
        rows = []
        for d in dates:
            day_str = d.strftime("%Y%m%d")
            for code in codes:
                rows.append(_moneyflow_row(code, day_str))
        return pd.DataFrame(rows)

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
    """透传 registry：用于观察 Provider 行为。"""

    def __init__(self, client: FakeTushareClient):
        config = AppConfig(tushare=TushareConfig(token="fake-token"))
        super().__init__(config, provider=_build_provider(client))


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
        config.history.max_retries = 3  # 总尝试 4 次
        return HistorySyncService(
            config,
            session_factory,
            registry if registry is not None else SpyRegistry(client),
            FakeCalendarProvider(),
            sleep=lambda _seconds: None,
            random_fn=lambda: 0.5,
        )

    return _make


def _stock_state(session_factory, dataset: DatasetName, instrument_id: str):
    with session_factory() as session:
        return StockSyncStateRepository(session).get(dataset, instrument_id)


def _dataset_state(session_factory, dataset: DatasetName):
    with session_factory() as session:
        return HistorySyncStateRepository(session).get(dataset)


def _fact_rows(session_factory, dataset: DatasetName, trade_date: date) -> int:
    with session_factory() as session:
        return HistoryFactRepository(session).count_for_date(dataset, trade_date)


def _fact_ts_codes(session_factory, dataset: DatasetName, trade_date: date) -> set[str]:
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


# ---- 主场景：旧代码区间回填落规范码 ----


class TestLegacyCodeBackfill:
    """旧代码股区间回填：落规范码、推进个股水位。"""

    def test_all_day_level_datasets_advance_stock_watermark(
        self, make_service, session_factory
    ):
        """旧代码出现不再让数据集失败，个股水位正常推进到目标日。"""
        service = make_service(FakeTushareClient())
        service.run(trigger=TriggerType.MANUAL)

        for dataset in DAY_LEVEL_DATASETS:
            state = _stock_state(session_factory, dataset, CANONICAL_INSTRUMENT_ID)
            assert state is not None, f"{dataset.value} 缺少 stock_sync_state 行"
            assert state.last_status == TASK_STATUS_SUCCESS, (
                f"{dataset.value} 不应因旧代码失败，实际 last_error_code="
                f"{state.last_error_code}: {state.last_error}"
            )
            assert state.watermark_date == NEXT_DAY, (
                f"{dataset.value} 水位应为 {NEXT_DAY}，实际 {state.watermark_date}"
            )
            assert state.ts_code == CANONICAL_TS_CODE, (
                f"{dataset.value} 冗余 ts_code 列应为规范码"
            )

    def test_facts_stored_under_canonical_ts_code(
        self, make_service, session_factory
    ):
        """落库 ts_code 是规范代码，旧代码不得进入事实表。"""
        service = make_service(FakeTushareClient())
        service.run(trigger=TriggerType.MANUAL)

        for dataset in DAY_LEVEL_DATASETS:
            codes = _fact_ts_codes(session_factory, dataset, LEGACY_DAY)
            assert CANONICAL_TS_CODE in codes, f"{dataset.value} 缺少规范证券"
            assert LEGACY_TS_CODE not in codes, (
                f"{dataset.value} 把旧代码写进了事实表: {codes}"
            )

    def test_no_duplicate_fact_rows_after_merge(self, make_service, session_factory):
        """新旧两行合并后，单证券单日只有一条事实记录。

        stock_basic 返回 001872 + 000001 两只；其中 001872 的上游返回
        新旧两行，经别名合并后只有 1 条规范码记录；000001 只有 1 行。
        因此每日总事实数 = 2（2 只证券各 1 条），总 record_count = 4（2 只 × 2 天）。
        """
        service = make_service(FakeTushareClient())
        service.run(trigger=TriggerType.MANUAL)

        for dataset in DAY_LEVEL_DATASETS:
            # 每日 2 行（2 只证券各 1 行）：001872 合并后 1 行，000001 本来就 1 行
            assert _fact_rows(session_factory, dataset, LEGACY_DAY) == 2, (
                f"{dataset.value} 出现重复事实键或行数不符"
            )
            # 001872 的事实行 ts_code 是规范码（不是旧码）
            codes = _fact_ts_codes(session_factory, dataset, LEGACY_DAY)
            assert CANONICAL_TS_CODE in codes
            assert LEGACY_TS_CODE not in codes
        # 总 record_count = 2 只 × 2 天
        assert _dataset_state(session_factory, DatasetName.DAILY).record_count == 4

    def test_no_placeholder_instrument_created(self, make_service, session_factory):
        """不得为旧代码创建 CN:STOCK:000022 假证券。"""
        service = make_service(FakeTushareClient())
        service.run(trigger=TriggerType.MANUAL)

        ids = _master_ids(session_factory)
        assert CANONICAL_INSTRUMENT_ID in ids
        assert f"CN:STOCK:{LEGACY_SYMBOL}" not in ids, "不得创建假历史证券"


# ---- 冲突：单股快速失败、不阻塞其他股、Run SUCCESS ----


class TestAliasConflictTerminalFailure:
    """ALIAS_CONFLICT 属配置类错误：首试即终态、不阻塞数据集。"""

    def test_conflict_fails_fast_for_that_stock(
        self, make_service, session_factory
    ):
        """同一证券同一交易日两种取值 → ALIAS_CONFLICT，该股 task=failed、水位不动。"""
        client = FakeTushareClient(daily_conflict=True)
        service = make_service(client)
        run_id = service.run(trigger=TriggerType.MANUAL)

        # 001872（冲突股）：个股失败
        state = _stock_state(session_factory, DatasetName.DAILY, CANONICAL_INSTRUMENT_ID)
        assert state.last_status == TASK_STATUS_FAILED
        assert state.last_error_code == "ALIAS_CONFLICT"
        assert state.watermark_date is None, "冲突股不得推进水位"
        # 冲突股（001872）没有事实行；其他股（000001）正常落库
        from app.models.history_fact import HISTORY_FACT_TABLES

        with session_factory() as session:
            table = HISTORY_FACT_TABLES[DatasetName.DAILY.value]
            canonical_rows = session.execute(
                select(table.c.ts_code).where(
                    table.c.trade_date == LEGACY_DAY,
                    table.c.ts_code == CANONICAL_TS_CODE,
                )
            ).fetchall()
        assert len(canonical_rows) == 0, "冲突股不得写入事实数据"

        # 配置类错误：只请求一次（快速失败）
        daily_calls = [params for name, params in client.calls if name == "daily"]
        # 有两只股票（001872 + 000001），各一次区间请求
        assert len(daily_calls) == 2, (
            f"配置类错误每只股票应只请求一次，实际 {len(daily_calls)} 次"
        )
        # 000001（非冲突股）应成功
        other_state = _stock_state(session_factory, DatasetName.DAILY, OTHER_INSTRUMENT_ID)
        assert other_state.watermark_date == NEXT_DAY

        # Run 仍为 SUCCESS（个股失败不使 Run 失败）
        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
        assert run.status == RunStatus.SUCCESS.value

        # 数据集级状态 = LAGGING（有个股失败）
        ds_state = _dataset_state(session_factory, DatasetName.DAILY)
        assert ds_state.status == DatasetStatus.LAGGING.value

    def test_conflict_recorded_on_run_dataset_and_stock_state(
        self, make_service, session_factory
    ):
        """run_dataset.task_failed_count 与 stock_sync_state.last_error_code 都反映冲突。"""
        service = make_service(FakeTushareClient(daily_conflict=True))
        run_id = service.run(trigger=TriggerType.MANUAL)

        with session_factory() as session:
            row = HistorySyncRunDatasetRepository(session).get(
                run_id, DatasetName.DAILY
            )
        # 个股失败口径：task_failed_count = 1（001872 失败，000001 成功）
        assert row.task_failed_count == 1
        assert row.task_success_count == 1
        assert row.processed_count == 2
        # 旧水位列冻结
        assert row.failed_trade_date is None
        # run_dataset 状态仍为 SUCCESS（允许存在个股失败）
        assert row.status == RunDatasetStatus.SUCCESS.value

        # 个股 state 记录错误码
        stock_state = _stock_state(
            session_factory, DatasetName.DAILY, CANONICAL_INSTRUMENT_ID
        )
        assert stock_state.last_error_code == "ALIAS_CONFLICT"

    def test_conflict_does_not_block_other_datasets(
        self, make_service, session_factory
    ):
        """数据集互不阻塞：daily 冲突不影响其余三个数据集推进。"""
        service = make_service(FakeTushareClient(daily_conflict=True))
        service.run(trigger=TriggerType.MANUAL)

        daily_state = _dataset_state(session_factory, DatasetName.DAILY)
        assert daily_state.status == DatasetStatus.LAGGING.value

        for dataset in (DatasetName.ADJ_FACTOR, DatasetName.DAILY_BASIC,
                        DatasetName.MONEYFLOW):
            stock_state = _stock_state(session_factory, dataset, CANONICAL_INSTRUMENT_ID)
            assert stock_state.watermark_date == NEXT_DAY, (
                f"{dataset.value} 不应被 daily 的冲突拖累"
            )
            assert stock_state.last_status == TASK_STATUS_SUCCESS
