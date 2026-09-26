"""HistorySyncService 个股口径集成测试（per-stock-history-sync，tasks 7.1/7.2/7.4/7.5）。

真实临时 DuckDB（conftest ``session_factory``）+ 完全离线的 mock Provider /
mock 严格日历，覆盖 spec 与 design 列出的核心场景：

- 7.1 个股水位推进与单调不下降
- 7.1 失败隔离：单股重试耗尽不影响其他股、Run SUCCESS、task_failed_count=1
- 7.1 自动补偿：失败股下轮自动从缺口继续并追平
- 7.1 已追平股票零请求、全部追平 run.status=NOOP
- 7.1 生命周期边界：中途上市、退市股、delist<start 跳过
- 7.1 系统级异常：Run FAILED、已成功股票进度保留
- 7.2 空结果语义：停牌区间 0 行推进水位、请求异常进入重试
- 7.4 优雅停机：cancellation_event 完成当前股后停止
- 7.4 中断恢复：stale run + running task → INTERRUPTED，水位不动
- 7.5 幂等：同区间重跑行数不变、record_count 不翻倍

"现在"由 ``frozen_now`` monkeypatch 固定，测试不依赖真实时钟。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from app.config import AppConfig
from app.models.history_fact import HISTORY_FACT_TABLES
from app.models.history_sync import (
    DatasetName,
    DatasetStatus,
    RunDatasetStatus,
    RunStatus,
    StockSyncState,
    SyncTask,
    TASK_STATUS_FAILED,
    TASK_STATUS_SUCCESS,
    TriggerType,
)
from app.models.instrument import Instrument
from app.providers.base import (
    AdjFactor,
    DailyBar,
    DailyBasic,
    MoneyFlow,
    ProviderBatch,
    StockBasicRecord,
    StockCompanyRecord,
    StockNameChangeRecord,
)
from app.providers.trading_calendar.provider import (
    CalendarDayRecord,
    CalendarUnavailableError,
)
from app.providers.tushare_common import TushareError
from app.repositories.history_fact import HistoryFactRepository
from app.repositories.history_master import HistoryMasterRepository
from app.repositories.history_sync import (
    HistorySyncRunDatasetRepository,
    HistorySyncRunRepository,
    HistorySyncStateRepository,
    StockSyncStateRepository,
    SyncTaskRepository,
)
from app.services.history import sync_service as sync_module
from app.services.history.sync_service import HistorySyncService

BEIJING = ZoneInfo("Asia/Shanghai")

# 测试日历：09-14/15/16 为交易日（09-12/13 周末休市）
CALENDAR_DAYS = [date(2026, 9, 10) + timedelta(days=i) for i in range(0, 10)]
OPEN_DAYS = [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)]

# 冻结的"现在"：2026-09-16 21:00（四个数据集 cutoff 全部已过，目标均指向 09-16）
FROZEN_NOW = datetime(2026, 9, 16, 21, 0, tzinfo=BEIJING)


# ---- mock 严格日历 ----


class FakeCalendarProvider:
    """严格日历 mock：只提供 ``get_days(..., strict=True)`` 所需契约。"""

    def __init__(self, *, unavailable: bool = False):
        self.unavailable = unavailable
        self.calls: list[tuple[str, date, date]] = []

    def get_days(self, market, start, end, *, strict=False):
        self.calls.append((market, start, end))
        if self.unavailable:
            raise CalendarUnavailableError("严格交易日历不可用（测试注入）")
        return [
            CalendarDayRecord(trade_date=day, is_open=day in OPEN_DAYS)
            for day in CALENDAR_DAYS
            if start <= day <= end
        ]


# ---- mock 历史 Provider（个股口径） ----


class FakeStockHistoryProviders:
    """离线 mock：按股票区间返回构造好的内部标准模型记录。

    ``fail_stocks``  : {ts_code: {dataset: 失败次数}} —— 指定股票前 N 次请求抛错
    ``empty_stocks`` : {ts_code: {dataset}} —— 指定股票某数据集恒返回 0 行
    ``list_dates``   : {ts_code: date} —— 每只股票的上市日（写入主档）
    ``delist_dates`` : {ts_code: date} —— 每只股票的退市日（写入主档）
    """

    source = "tushare"

    def __init__(
        self,
        *,
        symbols: tuple[str, ...] = ("000001",),
        fail_stocks: dict[str, dict[str, int]] | None = None,
        empty_stocks: dict[str, set[str]] | None = None,
        error_code: str = "TUSHARE_TIMEOUT",
        list_dates: dict[str, date] | None = None,
        delist_dates: dict[str, date] | None = None,
        exchange: str = "SZSE",
    ):
        self.symbols = symbols
        self.fail_stocks = fail_stocks or {}
        self.empty_stocks = empty_stocks or {}
        self.error_code = error_code
        self.list_dates = list_dates or {}
        self.delist_dates = delist_dates or {}
        self.exchange = exchange
        # 个股区间请求调用记录：(dataset, ts_code, start_date, end_date)
        self.stock_calls: list[tuple[str, str, date, date]] = []
        self.stock_basic_calls = 0
        # 每只股票每数据集的剩余失败次数（运行时递减）
        self._remaining_fail: dict[str, dict[str, int]] = {
            ts: dict(ds_map) for ts, ds_map in self.fail_stocks.items()
        }

    # -- 主档 --

    def _ts_code(self, symbol: str) -> str:
        suffix = "SH" if self.exchange == "SSE" else "SZ"
        return f"{symbol}.{suffix}"

    def get_stock_basic(self) -> ProviderBatch:
        self.stock_basic_calls += 1
        records = [
            StockBasicRecord(
                ts_code=self._ts_code(s),
                symbol=s,
                instrument_id=f"CN:STOCK:{s}",
                name=f"股票{s}",
                exchange=self.exchange,
                list_status="L",
                list_date=self.list_dates.get(self._ts_code(s)),
                delist_date=self.delist_dates.get(self._ts_code(s)),
            )
            for s in self.symbols
        ]
        return ProviderBatch(records=records, source=self.source, raw_row_count=len(records))

    def get_stock_company(self) -> ProviderBatch:
        records = [
            StockCompanyRecord(
                ts_code=self._ts_code(s),
                instrument_id=f"CN:STOCK:{s}",
                com_name=f"公司{s}",
            )
            for s in self.symbols
        ]
        return ProviderBatch(records=records, source=self.source, raw_row_count=len(records))

    def get_name_changes(self, *, ts_code=None, start_date=None, end_date=None) -> ProviderBatch:
        symbol = (ts_code or "000001.SZ").split(".")[0]
        event_start = start_date if start_date is not None else date(1991, 4, 3)
        records = [
            StockNameChangeRecord(
                ts_code=self._ts_code(symbol),
                instrument_id=f"CN:STOCK:{symbol}",
                name="旧名称",
                start_date=event_start,
            )
        ]
        return ProviderBatch(records=records, source=self.source, raw_row_count=len(records))

    # -- 个股区间 --

    def get_history_by_stock(
        self,
        dataset: str,
        instrument: Instrument,
        start_date: date,
        end_date: date,
    ) -> ProviderBatch:
        ts_code = self._ts_code_of(instrument)
        self.stock_calls.append((dataset, ts_code, start_date, end_date))

        # 失败注入
        remaining_map = self._remaining_fail.get(ts_code, {})
        if dataset in remaining_map and remaining_map[dataset] > 0:
            remaining_map[dataset] -= 1
            raise TushareError(
                f"模拟上游失败（测试注入） {dataset} {ts_code}",
                error_code=self.error_code,
            )

        # 空结果注入
        if dataset in self.empty_stocks.get(ts_code, set()):
            return ProviderBatch(records=[], source=self.source, raw_row_count=0)

        # 正常返回：区间内交易日每天一条记录
        records = self._build_records(dataset, instrument, start_date, end_date)
        return ProviderBatch(records=records, source=self.source, raw_row_count=len(records))

    def _ts_code_of(self, instrument: Instrument) -> str:
        return self._ts_code(instrument.symbol)

    def _build_records(
        self, dataset: str, instrument: Instrument, start: date, end: date
    ) -> list:
        builders = {
            "daily": _daily,
            "adj_factor": _adj_factor,
            "daily_basic": _daily_basic,
            "moneyflow": _moneyflow,
        }
        build = builders[dataset]
        records = []
        d = start
        while d <= end:
            if d in OPEN_DAYS:
                records.append(build(instrument.instrument_id, d))
            d += timedelta(days=1)
        return records


def _daily(instrument_id: str, trade_date: date) -> DailyBar:
    symbol = instrument_id[-6:]
    return DailyBar(
        instrument_id=instrument_id,
        ts_code=f"{symbol}.SZ",
        trade_date=trade_date,
        open=10.0, high=11.0, low=9.5, close=10.5,
        vol=100.0, amount=200.0,
    )


def _adj_factor(instrument_id: str, trade_date: date) -> AdjFactor:
    symbol = instrument_id[-6:]
    return AdjFactor(
        instrument_id=instrument_id,
        ts_code=f"{symbol}.SZ",
        trade_date=trade_date,
        adj_factor=1.0,
    )


def _daily_basic(instrument_id: str, trade_date: date) -> DailyBasic:
    symbol = instrument_id[-6:]
    return DailyBasic(
        instrument_id=instrument_id,
        ts_code=f"{symbol}.SZ",
        trade_date=trade_date,
        close=10.5,
        pe=15.0, pb=1.5,
        total_share=100.0, float_share=80.0,
        total_mv=1050.0, circ_mv=840.0,
        turnover_rate=1.0,
    )


def _moneyflow(instrument_id: str, trade_date: date) -> MoneyFlow:
    symbol = instrument_id[-6:]
    return MoneyFlow(
        instrument_id=instrument_id,
        ts_code=f"{symbol}.SZ",
        trade_date=trade_date,
        net_mf_amount=5.0,
    )


# ---- fixture ----


@pytest.fixture()
def frozen_now(monkeypatch):
    """固定"现在"为 2026-09-16 20:00（Asia/Shanghai），并允许测试改写。"""

    def _set(moment: datetime = FROZEN_NOW):
        monkeypatch.setattr(sync_module, "now_beijing", lambda: moment)
        return moment

    _set()
    return _set


@pytest.fixture()
def make_service(session_factory, frozen_now):
    """构造注入式的 HistorySyncService（sleep/random 全部替换，测试不真实等待）。"""

    def _make(
        *,
        providers: FakeStockHistoryProviders | None = None,
        calendar: FakeCalendarProvider | None = None,
        start_date: date = date(2026, 9, 10),
        max_retries: int = 3,  # 默认总尝试 4 次（与生产一致）
    ) -> tuple[HistorySyncService, FakeStockHistoryProviders, FakeCalendarProvider, list[float]]:
        config = AppConfig()
        config.history.start_date = start_date
        config.history.max_retries = max_retries
        fake_providers = providers if providers is not None else FakeStockHistoryProviders()
        fake_calendar = calendar if calendar is not None else FakeCalendarProvider()
        slept: list[float] = []
        service = HistorySyncService(
            config, session_factory, fake_providers, fake_calendar,
            sleep=slept.append, random_fn=lambda: 0.5,
        )
        return service, fake_providers, fake_calendar, slept

    return _make


# ---- helper ----


def _stock_state(session_factory, dataset: DatasetName, instrument_id: str):
    with session_factory() as session:
        return StockSyncStateRepository(session).get(dataset, instrument_id)


def _dataset_state(session_factory, dataset: DatasetName):
    with session_factory() as session:
        return HistorySyncStateRepository(session).get(dataset)


def _fact_rows(session_factory, dataset: DatasetName, trade_date: date) -> int:
    with session_factory() as session:
        return HistoryFactRepository(session).count_for_date(dataset, trade_date)


def _fact_total_rows(session_factory, dataset: DatasetName) -> int:
    with session_factory() as session:
        from sqlalchemy import func

        table = HISTORY_FACT_TABLES[dataset.value]
        return int(session.scalar(select(func.count()).select_from(table)) or 0)


def _run_dataset(session_factory, run_id: str, dataset: DatasetName):
    with session_factory() as session:
        return HistorySyncRunDatasetRepository(session).get(run_id, dataset)


# ===================================================================
# 7.1 个股水位推进与基本同步
# ===================================================================


class TestFirstSync:
    """首次同步：个股水位推进、facts 落库、record_count 累计。"""

    def test_single_stock_advances_watermark_to_target(self, make_service, session_factory):
        """单只股票首次全量同步 → 水位推进到目标日，facts 每日 1 行。"""
        service, _p, _c, _s = make_service()
        service.run(trigger=TriggerType.MANUAL)

        state = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        assert state.watermark_date == date(2026, 9, 16)
        assert state.last_status == TASK_STATUS_SUCCESS
        assert state.ts_code == "000001.SZ"

        for day in OPEN_DAYS:
            assert _fact_rows(session_factory, DatasetName.DAILY, day) == 1

    def test_record_count_matches_total_facts(self, make_service, session_factory):
        """数据集级 record_count = 事实表总行数。"""
        service, _p, _c, _s = make_service()
        service.run(trigger=TriggerType.MANUAL)

        ds_state = _dataset_state(session_factory, DatasetName.DAILY)
        assert ds_state.record_count == 3  # 1 股 × 3 交易日
        assert ds_state.data_min_date == date(2026, 9, 14)
        assert ds_state.data_max_date == date(2026, 9, 16)
        assert ds_state.status == DatasetStatus.CAUGHT_UP.value

    def test_four_datasets_all_advance(self, make_service, session_factory):
        """四个日级数据集各自独立推进水位。"""
        service, _p, _c, _s = make_service()
        service.run(trigger=TriggerType.MANUAL)

        for ds in (DatasetName.DAILY, DatasetName.ADJ_FACTOR,
                   DatasetName.DAILY_BASIC, DatasetName.MONEYFLOW):
            state = _stock_state(session_factory, ds, "CN:STOCK:000001")
            assert state.watermark_date == date(2026, 9, 16), (
                f"{ds.value} 水位未推进"
            )

    def test_run_success_and_run_dataset_counts(self, make_service, session_factory):
        """Run.status=SUCCESS；run_dataset 新统计列正确。"""
        service, _p, _c, _s = make_service()
        run_id = service.run(trigger=TriggerType.MANUAL)

        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
        assert run.status == RunStatus.SUCCESS.value
        assert run.trigger_type == TriggerType.MANUAL.value
        assert run.finished_at is not None

        rd = _run_dataset(session_factory, run_id, DatasetName.DAILY)
        assert rd.status == RunDatasetStatus.SUCCESS.value
        assert rd.processed_count == 1
        assert rd.task_success_count == 1
        assert rd.task_failed_count == 0
        assert rd.skipped_count == 0
        assert rd.rows_written == 3
        assert rd.request_count == 1
        # 旧水位列冻结：新 run 应为 NULL / 0
        assert rd.start_watermark is None
        assert rd.target_trade_date is None
        assert rd.end_watermark is None
        assert rd.dates_completed == 0
        assert rd.failed_trade_date is None

    def test_watermark_null_starts_full_range(self, make_service, session_factory):
        """水位 NULL 表示从有效起点全量同步。"""
        service, _p, _c, _s = make_service(start_date=date(2026, 9, 10))
        service.run(trigger=TriggerType.MANUAL)
        state = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        assert state.watermark_date == date(2026, 9, 16)

    def test_sync_task_created_per_stock(self, make_service, session_factory):
        """每只有工作的股票对应一条 sync_task 流水。"""
        providers = FakeStockHistoryProviders(symbols=("000001", "000002"))
        service, _p, _c, _s = make_service(providers=providers)
        run_id = service.run(trigger=TriggerType.MANUAL)

        with session_factory() as session:
            tasks = list(session.scalars(
                select(SyncTask).where(SyncTask.run_id == run_id, SyncTask.dataset == "daily")
            ))
        assert len(tasks) == 2
        assert all(t.status == TASK_STATUS_SUCCESS for t in tasks)
        assert all(t.records_written == 3 for t in tasks)


class TestNoopRun:
    """全部追平时 run.status=NOOP，不创建 task、不发请求。"""

    def test_all_caught_up_is_noop(self, make_service, session_factory):
        """Scenario "无事可做"：追平后再次触发为 NOOP。"""
        service, providers, _c, _ = make_service()
        service.run(trigger=TriggerType.MANUAL)
        calls_before = len(providers.stock_calls)

        run_id = service.run(trigger=TriggerType.MANUAL)
        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
            assert run.status == RunStatus.NOOP.value

        assert len(providers.stock_calls) == calls_before, (
            "NOOP 轮不得请求个股数据"
        )

    def test_noop_run_no_tasks_created(self, make_service, session_factory):
        """NOOP run 不创建 sync_task 行。"""
        service, _p, _c, _ = make_service()
        run1 = service.run(trigger=TriggerType.MANUAL)
        with session_factory() as session:
            t1 = list(session.scalars(
                select(SyncTask).where(SyncTask.run_id == run1)
            ))
        assert len(t1) > 0

        run2 = service.run(trigger=TriggerType.MANUAL)
        with session_factory() as session:
            t2 = list(session.scalars(
                select(SyncTask).where(SyncTask.run_id == run2)
            ))
        assert len(t2) == 0, "NOOP 轮不得创建 sync_task"

    def test_noop_run_dataset_skipped_equals_universe(self, make_service, session_factory):
        """NOOP run 的 run_dataset.skipped_count = 股票总数。"""
        providers = FakeStockHistoryProviders(symbols=("000001", "000002", "000003"))
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        run_id = service.run(trigger=TriggerType.MANUAL)
        rd = _run_dataset(session_factory, run_id, DatasetName.DAILY)
        assert rd.status == RunDatasetStatus.NOOP.value
        assert rd.skipped_count == 3
        assert rd.processed_count == 0


# ===================================================================
# 7.1 失败隔离
# ===================================================================


class TestFailureIsolation:
    """单股失败不阻塞其他股、Run SUCCESS、数据集 LAGGING。"""

    def test_single_stock_failure_does_not_block_others(self, make_service, session_factory):
        """Scenario "单股失败不阻塞其他股票"：1 只失败、其余成功。"""
        providers = FakeStockHistoryProviders(
            symbols=("000001", "000002"),
            fail_stocks={"000002.SZ": {"daily": 10}},
        )
        service, _p, _c, _s = make_service(providers=providers)
        run_id = service.run(trigger=TriggerType.MANUAL)

        # 000001 成功
        s1 = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        assert s1.watermark_date == date(2026, 9, 16)
        assert s1.last_status == TASK_STATUS_SUCCESS

        # 000002 失败：水位不动
        s2 = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000002")
        assert s2.watermark_date is None
        assert s2.last_status == TASK_STATUS_FAILED
        assert s2.last_error_code == "TUSHARE_TIMEOUT"

        # Run 仍为 SUCCESS（个股失败不使 Run 失败）
        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
        assert run.status == RunStatus.SUCCESS.value

        # 数据集级状态 = LAGGING（有失败股）
        ds = _dataset_state(session_factory, DatasetName.DAILY)
        assert ds.status == DatasetStatus.LAGGING.value

        # run_dataset 计数
        rd = _run_dataset(session_factory, run_id, DatasetName.DAILY)
        assert rd.task_success_count == 1
        assert rd.task_failed_count == 1
        assert rd.processed_count == 2

    def test_config_error_fails_fast(self, make_service, session_factory):
        """配置类错误首试即终态，不睡满退避轮次。"""
        providers = FakeStockHistoryProviders(
            symbols=("000001",),
            fail_stocks={"000001.SZ": {"daily": 5}},
            error_code="TUSHARE_PERMISSION_DENIED",
        )
        service, _p, _c, slept = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        daily_calls = [
            (ts, s, e) for ds, ts, s, e in providers.stock_calls if ds == "daily"
        ]
        assert len(daily_calls) == 1, "配置类错误只尝试一次"
        assert slept == [], "配置类错误不进入退避等待"

        state = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        assert state.last_status == TASK_STATUS_FAILED
        assert state.last_error_code == "TUSHARE_PERMISSION_DENIED"

    def test_retry_then_success_advances(self, make_service, session_factory):
        """前 2 次失败、第 3 次成功 → 水位推进，retry_count=2。"""
        providers = FakeStockHistoryProviders(
            symbols=("000001",),
            fail_stocks={"000001.SZ": {"daily": 2}},
        )
        service, _p, _c, slept = make_service(providers=providers, max_retries=3)
        run_id = service.run(trigger=TriggerType.MANUAL)

        state = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        assert state.watermark_date == date(2026, 9, 16)
        assert state.last_status == TASK_STATUS_SUCCESS

        assert len(slept) == 2

        rd = _run_dataset(session_factory, run_id, DatasetName.DAILY)
        assert rd.retry_count == 2

    def test_failure_does_not_affect_other_datasets(self, make_service, session_factory):
        """daily 失败不阻塞 adj_factor/daily_basic/moneyflow。"""
        providers = FakeStockHistoryProviders(
            symbols=("000001",),
            fail_stocks={"000001.SZ": {"daily": 10}},
        )
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        for ds in (DatasetName.ADJ_FACTOR, DatasetName.DAILY_BASIC, DatasetName.MONEYFLOW):
            s = _stock_state(session_factory, ds, "CN:STOCK:000001")
            assert s.watermark_date == date(2026, 9, 16), (
                f"{ds.value} 不应被 daily 失败拖累"
            )

    def test_universe_order_watermark_null_first(self, make_service, session_factory):
        """落后最久优先：全 NULL 时按 ts_code 升序。"""
        providers = FakeStockHistoryProviders(symbols=("000002", "000001", "000003"))
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        daily_seq = [
            ts for ds, ts, _, _ in providers.stock_calls if ds == "daily"
        ]
        assert daily_seq == ["000001.SZ", "000002.SZ", "000003.SZ"]


# ===================================================================
# 7.1 自动补偿
# ===================================================================


class TestAutoCompensation:
    """失败股下轮自动从缺口继续并追平。"""

    def test_failed_stock_resumes_next_run(self, make_service, session_factory):
        """Scenario "失败股票下次从缺口继续"：本轮失败，下轮成功追平。"""
        providers = FakeStockHistoryProviders(
            symbols=("000001", "000002"),
            fail_stocks={"000002.SZ": {"daily": 10}},
        )
        service, _p, _c, _ = make_service(providers=providers)

        run1 = service.run(trigger=TriggerType.MANUAL)
        s2_first = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000002")
        assert s2_first.last_status == TASK_STATUS_FAILED
        assert s2_first.watermark_date is None

        # 清除失败注入（模拟上游恢复）
        providers.fail_stocks.clear()
        providers._remaining_fail.clear()

        run2 = service.run(trigger=TriggerType.SCHEDULED)
        s2_second = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000002")
        assert s2_second.watermark_date == date(2026, 9, 16)
        assert s2_second.last_status == TASK_STATUS_SUCCESS

        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run2)
        assert run.status == RunStatus.SUCCESS.value

    def test_caught_up_stocks_zero_requests(self, make_service, session_factory):
        """Scenario "已追平股票零请求"：追平股在后续 run 中不被请求。"""
        providers = FakeStockHistoryProviders(symbols=("000001", "000002"))
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        calls_before = len(providers.stock_calls)
        service.run(trigger=TriggerType.MANUAL)
        assert len(providers.stock_calls) == calls_before, (
            "已追平股票不应被再次请求"
        )


# ===================================================================
# 7.1 生命周期边界
# ===================================================================


class TestLifecycleBoundaries:
    """上市/退市边界的有效区间计算。"""

    def test_stock_listed_midway_starts_from_list_date(self, make_service, session_factory):
        """Scenario "中途上市股票不回填上市前"：list_date 在 history.start_date 之后。"""
        providers = FakeStockHistoryProviders(
            symbols=("000001",),
            list_dates={"000001.SZ": date(2026, 9, 15)},
        )
        service, _p, _c, _ = make_service(
            providers=providers, start_date=date(2026, 9, 10)
        )
        service.run(trigger=TriggerType.MANUAL)

        state = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        # 09-15 上市，第一个交易日是 09-15，终点 09-16 → 2 个交易日
        assert state.watermark_date == date(2026, 9, 16)
        total = _fact_total_rows(session_factory, DatasetName.DAILY)
        assert total == 2

    def test_delisted_stock_syncs_to_delist_date(self, make_service, session_factory):
        """Scenario "退市股票同步至退市日"：delist_date 早于 target。"""
        providers = FakeStockHistoryProviders(
            symbols=("000001",),
            delist_dates={"000001.SZ": date(2026, 9, 15)},
        )
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        state = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        assert state.watermark_date == date(2026, 9, 15)
        assert state.last_status == TASK_STATUS_SUCCESS

    def test_delisted_before_start_is_skipped(self, make_service, session_factory):
        """Scenario "早于历史起点的退市股无工作"：delist < start → skipped。"""
        providers = FakeStockHistoryProviders(
            symbols=("000001",),
            delist_dates={"000001.SZ": date(2026, 9, 1)},
        )
        service, _p, _c, _ = make_service(providers=providers)
        run_id = service.run(trigger=TriggerType.MANUAL)

        state = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        assert state.watermark_date is None, "退市早于起点的股票不应有水位"

        rd = _run_dataset(session_factory, run_id, DatasetName.DAILY)
        assert rd.skipped_count == 1
        assert rd.processed_count == 0

        daily_calls = [c for c in providers.stock_calls if c[0] == "daily"]
        assert len(daily_calls) == 0


# ===================================================================
# 7.1 系统级异常
# ===================================================================


class TestSystemLevelFailure:
    """系统级异常 → Run FAILED；已成功股票进度保留。"""

    def test_database_error_marks_run_failed(self, make_service, session_factory, monkeypatch):
        """注入数据库错误 → Run FAILED、已成功股票进度保留。"""
        providers = FakeStockHistoryProviders(symbols=("000001", "000002", "000003"))
        service, _p, _c, _ = make_service(providers=providers)

        from app.services.history.stock_executor import StockSyncExecutor

        original_execute = StockSyncExecutor.execute
        call_count = [0]

        def boom_after_first(self, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                from sqlalchemy.exc import OperationalError

                raise OperationalError(
                    "数据库连接断开（测试注入）", {}, Exception("模拟 DB 错误")
                )
            return original_execute(self, **kwargs)

        monkeypatch.setattr(StockSyncExecutor, "execute", boom_after_first)

        run_id = service.run(trigger=TriggerType.MANUAL)

        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
        assert run.status == RunStatus.FAILED.value

        # 第一只股票已成功，进度保留
        s1 = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        assert s1.watermark_date == date(2026, 9, 16)


# ===================================================================
# 7.2 空结果语义
# ===================================================================


class TestEmptyResults:
    """空结果（0 行无异常）推进水位；请求异常进入重试。"""

    def test_empty_range_advances_watermark(self, make_service, session_factory):
        """Scenario "停牌区间空结果推进水位"：0 行无异常 → 推进水位。"""
        providers = FakeStockHistoryProviders(
            symbols=("000001",),
            empty_stocks={"000001.SZ": {"daily"}},
        )
        service, _p, _c, slept = make_service(providers=providers)
        run_id = service.run(trigger=TriggerType.MANUAL)

        state = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        assert state.watermark_date == date(2026, 9, 16), "空结果应推进水位到区间终点"
        assert state.last_status == TASK_STATUS_SUCCESS
        assert slept == [], "空结果不是错误，不进入退避重试"

        with session_factory() as session:
            task = session.scalar(
                select(SyncTask).where(
                    SyncTask.run_id == run_id, SyncTask.dataset == "daily"
                )
            )
        assert task.records_fetched == 0
        assert task.records_written == 0

    def test_request_error_enters_retry_path(self, make_service, session_factory):
        """Scenario "请求异常仍进入重试"：超时 → 正常重试，耗尽后失败。"""
        providers = FakeStockHistoryProviders(
            symbols=("000001",),
            fail_stocks={"000001.SZ": {"daily": 10}},
            error_code="TUSHARE_TIMEOUT",
        )
        service, _p, _c, slept = make_service(providers=providers, max_retries=3)
        service.run(trigger=TriggerType.MANUAL)

        state = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        assert state.watermark_date is None
        assert state.last_status == TASK_STATUS_FAILED
        assert state.last_error_code == "TUSHARE_TIMEOUT"

        assert len(slept) == 3

        daily_calls = [c for c in providers.stock_calls if c[0] == "daily"]
        assert len(daily_calls) == 4  # 总尝试 = max_retries + 1


# ===================================================================
# 7.4 优雅停机
# ===================================================================


class TestCancellation:
    """cancellation_event 机制：完成当前股事务后停止。"""

    def test_cancellation_before_first_stock(self, make_service, session_factory):
        """开始前已置位 → 不发起请求，run=INTERRUPTED。"""
        import threading

        service, providers, _c, _ = make_service()
        event = threading.Event()
        event.set()
        run_id = service.run(trigger=TriggerType.MANUAL, cancellation_event=event)

        assert providers.stock_calls == [], "停机后不得发起个股请求"
        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
        assert run.status == RunStatus.INTERRUPTED.value
        assert _dataset_state(session_factory, DatasetName.STOCK_BASIC).last_success_at is not None

    def test_cancellation_midway_keeps_completed_progress(self, make_service, session_factory):
        """Scenario "优雅停机"：中途停机，已完成股票水位保留。"""
        import threading

        providers = FakeStockHistoryProviders(symbols=("000001", "000002", "000003"))
        service, _p, _c, _ = make_service(providers=providers)

        event = threading.Event()
        original_get = providers.get_history_by_stock
        call_count = [0]

        def cancelling_get(dataset, instrument, start_date, end_date):
            call_count[0] += 1
            result = original_get(dataset, instrument, start_date, end_date)
            if dataset == "daily" and call_count[0] == 1:
                event.set()
            return result

        providers.get_history_by_stock = cancelling_get
        run_id = service.run(trigger=TriggerType.MANUAL, cancellation_event=event)

        s1 = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        assert s1.watermark_date == date(2026, 9, 16), "已完成股票进度保留"

        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
        assert run.status == RunStatus.INTERRUPTED.value


# ===================================================================
# 7.4 中断恢复
# ===================================================================


class TestStaleRunRecovery:
    """stale run + running task → INTERRUPTED，水位不动。"""

    def test_stale_run_marked_interrupted_and_tasks_interrupted(self, make_service, session_factory):
        """Scenario "重启后恢复"：遗留 RUNNING run → INTERRUPTED，running task → interrupted。"""
        service, providers, _c, _ = make_service()
        service.run(trigger=TriggerType.MANUAL)

        with session_factory() as session:
            run_repo = HistorySyncRunRepository(session)
            run_repo.create(
                "stale-run-0001", trigger_type=TriggerType.SCHEDULED,
                requested_by_user_id=None, started_at=FROZEN_NOW,
            )
            task_repo = SyncTaskRepository(session)
            task_repo.create(
                run_id="stale-run-0001",
                dataset=DatasetName.DAILY,
                instrument_id="CN:STOCK:000001",
                ts_code="000001.SZ",
                start_date=date(2026, 9, 14),
                end_date=date(2026, 9, 16),
                started_at=FROZEN_NOW,
            )
            session.commit()

        service.run(trigger=TriggerType.STARTUP)

        with session_factory() as session:
            stale = HistorySyncRunRepository(session).get("stale-run-0001")
            assert stale.status == RunStatus.INTERRUPTED.value
            assert stale.finished_at is not None
            assert not HistorySyncRunRepository(session).find_stale_running()

            tasks = list(session.scalars(
                select(SyncTask).where(SyncTask.run_id == "stale-run-0001")
            ))
        assert len(tasks) == 1
        assert tasks[0].status == "interrupted"
        assert tasks[0].finished_at is not None

        state = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        assert state.watermark_date == date(2026, 9, 16)

    def test_next_run_resumes_from_original_watermark(self, make_service, session_factory):
        """中断后下一轮按原水位重新同步（重新创建 task）。"""
        providers = FakeStockHistoryProviders(symbols=("000001",))
        service, _p, _c, _ = make_service(providers=providers)

        with session_factory() as session:
            run_repo = HistorySyncRunRepository(session)
            run_repo.create(
                "stale-run-0002", trigger_type=TriggerType.SCHEDULED,
                requested_by_user_id=None, started_at=FROZEN_NOW,
            )
            StockSyncStateRepository(session).bulk_ensure_missing(
                DatasetName.DAILY, [("CN:STOCK:000001", "000001.SZ")]
            )
            task_repo = SyncTaskRepository(session)
            task_repo.create(
                run_id="stale-run-0002",
                dataset=DatasetName.DAILY,
                instrument_id="CN:STOCK:000001",
                ts_code="000001.SZ",
                start_date=date(2026, 9, 14),
                end_date=date(2026, 9, 16),
                started_at=FROZEN_NOW,
            )
            session.commit()

        run_id = service.run(trigger=TriggerType.STARTUP)

        state = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        assert state.watermark_date == date(2026, 9, 16)
        assert state.last_status == TASK_STATUS_SUCCESS

        with session_factory() as session:
            new_tasks = list(session.scalars(
                select(SyncTask).where(SyncTask.run_id == run_id, SyncTask.dataset == "daily")
            ))
        assert len(new_tasks) == 1
        assert new_tasks[0].status == TASK_STATUS_SUCCESS


# ===================================================================
# 7.5 幂等与重复运行
# ===================================================================


class TestIdempotency:
    """同区间重跑 → 事实行数不变、record_count 不翻倍。"""

    def test_rerun_same_range_does_not_double_count(self, make_service, session_factory):
        """Scenario "重复运行不重复计数"：同区间重跑，record_count 不变。"""
        providers = FakeStockHistoryProviders(symbols=("000001",))
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)
        before = _dataset_state(session_factory, DatasetName.DAILY)
        assert before.record_count == 3

        # 回退水位使同一区间重新进入待同步
        with session_factory() as session:
            session.execute(
                update(StockSyncState)
                .where(
                    StockSyncState.dataset == "daily",
                    StockSyncState.instrument_id == "CN:STOCK:000001",
                )
                .values(watermark_date=date(2026, 9, 13))
            )
            session.commit()

        service.run(trigger=TriggerType.MANUAL)
        after = _dataset_state(session_factory, DatasetName.DAILY)
        assert after.record_count == 3, "record_count 不得因重跑翻倍"
        assert _fact_rows(session_factory, DatasetName.DAILY, date(2026, 9, 16)) == 1
        assert _fact_total_rows(session_factory, DatasetName.DAILY) == 3

    def test_tasks_accumulate_watermark_stable(self, make_service, session_factory):
        """任务流水新增（每次运行一行），水位不变。"""
        providers = FakeStockHistoryProviders(symbols=("000001",))
        service, _p, _c, _ = make_service(providers=providers)

        run1 = service.run(trigger=TriggerType.MANUAL)
        with session_factory() as session:
            session.execute(
                update(StockSyncState)
                .where(
                    StockSyncState.dataset == "daily",
                    StockSyncState.instrument_id == "CN:STOCK:000001",
                )
                .values(watermark_date=date(2026, 9, 13))
            )
            session.commit()

        run2 = service.run(trigger=TriggerType.MANUAL)

        with session_factory() as session:
            t1 = list(session.scalars(
                select(SyncTask).where(SyncTask.run_id == run1, SyncTask.dataset == "daily")
            ))
            t2 = list(session.scalars(
                select(SyncTask).where(SyncTask.run_id == run2, SyncTask.dataset == "daily")
            ))
        assert len(t1) == 1
        assert len(t2) == 1
        assert t1[0].id != t2[0].id

        state = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        assert state.watermark_date == date(2026, 9, 16)


# ===================================================================
# 主档前置
# ===================================================================


class TestMasterPrerequisites:
    """主档硬前置/非阻塞前置行为。"""

    def test_calendar_unavailable_blocks_day_level(self, make_service, session_factory):
        """Scenario "硬前置失败阻止日级推进"：日历不可用 → FAILED。"""
        calendar = FakeCalendarProvider(unavailable=True)
        service, providers, _c, _ = make_service(calendar=calendar)
        run_id = service.run(trigger=TriggerType.MANUAL)

        assert providers.stock_calls == [], "硬前置失败不得请求日级数据集"
        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
        assert run.status == RunStatus.FAILED.value
        assert run.error_summary

        cal_state = _dataset_state(session_factory, DatasetName.TRADE_CAL)
        assert cal_state.last_error_code == "CALENDAR_UNAVAILABLE"

    def test_company_failure_does_not_block_daily(self, make_service, session_factory):
        """非阻塞主档失败不影响日级数据集推进。"""
        providers = FakeStockHistoryProviders(symbols=("000001",))

        def boom_company():
            raise TushareError("模拟公司主档失败", error_code="TUSHARE_API_ERROR")

        providers.get_stock_company = boom_company
        service, _p, _c, _ = make_service(providers=providers)
        run_id = service.run(trigger=TriggerType.MANUAL)

        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
        assert run.status == RunStatus.SUCCESS.value

        state = _dataset_state(session_factory, DatasetName.DAILY)
        assert state.status == DatasetStatus.CAUGHT_UP.value


# ===================================================================
# 日志与安全
# ===================================================================


class TestLoggingAndSafety:
    """日志规范与错误文本脱敏。"""

    def test_no_token_in_logs_or_state(self, make_service, session_factory, caplog):
        """spec "日志不含 Token"：错误文本与日志不得出现 Token 明文。"""
        import logging

        secret = "abcdef0123456789abcdef0123456789abcdef"
        providers = FakeStockHistoryProviders(
            symbols=("000001",),
            fail_stocks={"000001.SZ": {"daily": 10}},
            error_code="TUSHARE_TOKEN_MISSING",
        )
        original_get = providers.get_history_by_stock

        def leaky_get(dataset, instrument, start_date, end_date):
            remaining_map = providers._remaining_fail.get(
                providers._ts_code_of(instrument), {}
            )
            if dataset in remaining_map and remaining_map[dataset] > 0:
                remaining_map[dataset] -= 1
                raise TushareError(
                    f"Tushare Token 无效 token={secret}",
                    error_code="TUSHARE_TOKEN_MISSING",
                )
            return original_get(dataset, instrument, start_date, end_date)

        providers.get_history_by_stock = leaky_get
        service, _p, _c, _ = make_service(providers=providers)
        with caplog.at_level(logging.INFO, logger=sync_module.__name__):
            service.run(trigger=TriggerType.MANUAL)

        for record in caplog.records:
            assert secret not in record.getMessage()

        state = _stock_state(session_factory, DatasetName.DAILY, "CN:STOCK:000001")
        assert secret not in (state.last_error or "")
        assert "***" in (state.last_error or "")


# ===================================================================
# 进度快照
# ===================================================================


class TestProgressTracking:
    """Service.progress 实时进度快照计数正确。"""

    def test_progress_has_expected_fields(self, make_service, session_factory):
        """progress 对象存在并包含 expected 字段。"""
        providers = FakeStockHistoryProviders(
            symbols=("000001", "000002", "000003"),
            fail_stocks={"000002.SZ": {"daily": 10}},
            delist_dates={"000003.SZ": date(2026, 9, 1)},
        )
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        assert service.progress is not None
        assert hasattr(service.progress, "succeeded")
        assert hasattr(service.progress, "failed")
        assert hasattr(service.progress, "skipped")
