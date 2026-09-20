"""HistorySyncService 核心集成测试（a-share-historical-data，tasks 5.8）。

真实临时 DuckDB（conftest ``session_factory``）+ 完全离线的 mock Provider /
mock 严格日历，覆盖技术方案 §70 列出的场景：

- §70.1 首次三天同步（事实/ledger/水位/record_count）；
- §70.2 中间失败绝不跳过（01-06 从未被请求）；
- §70.3 下次从失败日恢复；
- §70.4 数据集独立推进；
- §70.5 DELETE 后 / INSERT 后 / state 更新前注入异常的回滚；
- §70.6 重复运行不重复计数；
- §70.7 6000 截断 fallback 与"fallback 失败则不推进"；
- §70.8 stale run 恢复（RUNNING→INTERRUPTED、state 恢复、catch-up 续跑）；
- §34 空结果（历史 EMPTY_RESULT 失败 / 当期 WAITING_SOURCE 不推进）。

"现在"由 ``_now`` monkeypatch 固定，测试不依赖真实时钟。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import AppConfig
from app.models.history_fact import HISTORY_FACT_TABLES
from app.models.history_sync import (
    DatasetName,
    DatasetStatus,
    HistorySyncState,
    RunDatasetStatus,
    RunStatus,
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
from app.providers.history.tushare import UnknownInstrumentError
from app.providers.tushare_common import DAILY_ROW_CAP, TushareError
from app.repositories.history_master import HistoryMasterRepository
from app.repositories.history_sync import (
    HistoryDayStatusRepository,
    HistorySyncRunDatasetRepository,
    HistorySyncRunRepository,
    HistorySyncStateRepository,
)
from app.services.history import sync_service as sync_module
from app.services.history.sync_service import HistorySyncService

BEIJING = ZoneInfo("Asia/Shanghai")

# 测试日历：2026-09-14/15/16 为交易日（09-12/13 周末休市）
CALENDAR_DAYS = [date(2026, 9, 10) + timedelta(days=i) for i in range(0, 10)]
OPEN_DAYS = [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)]
# 冻结的"现在"：2026-09-16 21:00（四个数据集 cutoff 全部已过，含 moneyflow 20:30
# → 目标均指向 09-16）
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


# ---- mock 历史 Provider ----


class FakeHistoryProviders:
    """离线 mock：按交易日返回构造好的内部标准模型记录。

    ``fail_dates``     : {dataset: {date: 失败次数}} —— 指定日期前 N 次请求抛错
    ``empty_dates``    : {dataset: {date}} —— 指定日期返回 0 行
    ``truncate_dates`` : {dataset: {date}} —— 主路径返回恰 6000 行（触发 fallback）
    ``fallback_fails`` : {dataset: {date}} —— fallback 仍返回 6000 行
    """

    source = "tushare"

    def __init__(
        self,
        *,
        symbols: tuple[str, ...] = ("000001",),
        fail_dates: dict[str, dict[date, int]] | None = None,
        empty_dates: dict[str, set[date]] | None = None,
        truncate_dates: dict[str, set[date]] | None = None,
        fallback_fails: dict[str, set[date]] | None = None,
        error_code: str = "TUSHARE_TIMEOUT",
    ):
        self.symbols = symbols
        self.fail_dates = fail_dates or {}
        self.empty_dates = empty_dates or {}
        self.truncate_dates = truncate_dates or {}
        self.fallback_fails = fallback_fails or {}
        self.error_code = error_code
        self.calls: list[tuple[str, date]] = []
        self.fallback_calls: list[tuple[str, date]] = []
        # daily_basic 补齐时 Service 传入的缺失代码（None = 未指定，按全部逐只）
        self.daily_basic_missing_calls: list[list[str] | None] = []
        # daily_basic 主路径截断时"实际返回"的证券（模拟被上限截掉的批次）
        self.daily_basic_primary_symbols: tuple[str, ...] | None = None
        # 补齐恒返回 0 行（真实 daily_basic 多代码静默空的行为）
        self.daily_basic_supplement_empty = False
        # 补齐中抛错（逐只请求中途失败）
        self.daily_basic_supplement_error = False
        self.stock_basic_calls = 0
        self._remaining = {
            ds: dict(dates) for ds, dates in (fail_dates or {}).items()
        }

    # -- 主档 --

    def get_stock_basic(self) -> ProviderBatch:
        self.stock_basic_calls += 1
        records = [
            StockBasicRecord(
                ts_code=f"{s}.SZ", symbol=s, instrument_id=f"CN:STOCK:{s}",
                name=f"股票{s}", exchange="SZSE", list_status="L",
            )
            for s in self.symbols
        ]
        return ProviderBatch(records=records, source=self.source, raw_row_count=len(records))

    def get_stock_company(self) -> ProviderBatch:
        records = [
            StockCompanyRecord(
                ts_code=f"{s}.SZ", instrument_id=f"CN:STOCK:{s}", com_name=f"公司{s}",
            )
            for s in self.symbols
        ]
        return ProviderBatch(records=records, source=self.source, raw_row_count=len(records))

    def get_name_changes(self, *, ts_code=None, start_date=None, end_date=None) -> ProviderBatch:
        symbol = (ts_code or "000001.SZ").split(".")[0]
        # 窗口请求（start_date 非空）返回窗口内事件——与真实 Provider 语义一致，
        # 否则窗口替换会撞上 bootstrap 写入的同键旧行（主键 event_key 冲突）。
        event_start = date(1991, 4, 3) if start_date is None else start_date
        records = [
            StockNameChangeRecord(
                ts_code=f"{symbol}.SZ", instrument_id=f"CN:STOCK:{symbol}",
                name="旧名称", start_date=event_start,
            )
        ]
        return ProviderBatch(records=records, source=self.source, raw_row_count=len(records))

    # -- 日级 --

    def _maybe_fail(self, dataset: str, trade_date: date) -> None:
        self.calls.append((dataset, trade_date))
        remaining = self._remaining.get(dataset, {})
        if trade_date in remaining and remaining[trade_date] > 0:
            remaining[trade_date] -= 1
            raise TushareError("模拟上游失败（测试注入）", error_code=self.error_code)

    def _rows(self, dataset: str, trade_date: date, build) -> list:
        return [build(f"CN:STOCK:{s}", trade_date) for s in self.symbols]

    def _primary(self, dataset: str, trade_date: date, build) -> ProviderBatch:
        self._maybe_fail(dataset, trade_date)
        if trade_date in self.empty_dates.get(dataset, set()):
            return ProviderBatch(records=[], source=self.source, raw_row_count=0)
        if trade_date in self.truncate_dates.get(dataset, set()):
            # 主路径命中上限：records 为空、truncation_risk=True（Service 必须 fallback）
            return ProviderBatch(records=[], source=self.source, raw_row_count=DAILY_ROW_CAP,
                                 truncation_risk=True)
        records = self._rows(dataset, trade_date, build)
        return ProviderBatch(records=records, source=self.source, raw_row_count=len(records))

    def _fallback(self, dataset: str, trade_date: date, build) -> ProviderBatch:
        self.fallback_calls.append((dataset, trade_date))
        if trade_date in self.fallback_fails.get(dataset, set()):
            return ProviderBatch(records=[], source=self.source, raw_row_count=DAILY_ROW_CAP,
                                 truncation_risk=True)
        records = self._rows(dataset, trade_date, build)
        return ProviderBatch(records=records, source=self.source, raw_row_count=len(records))

    def get_daily(self, trade_date, instruments):
        return self._primary("daily", trade_date, _daily)

    def get_daily_for_instruments(self, trade_date, instruments):
        return self._fallback("daily", trade_date, _daily)

    def get_adj_factors(self, trade_date, instruments):
        return self._primary("adj_factor", trade_date, _adj_factor)

    def get_daily_basic(self, trade_date, instruments):
        self._maybe_fail("daily_basic", trade_date)
        if trade_date in self.empty_dates.get("daily_basic", set()):
            return ProviderBatch(records=[], source=self.source, raw_row_count=0)
        if trade_date in self.truncate_dates.get("daily_basic", set()):
            # 截断批次：只回一部分证券（daily_basic_primary_symbols 指定），
            # 其余交由候选集补齐路径取回
            symbols = (
                self.daily_basic_primary_symbols
                if self.daily_basic_primary_symbols is not None
                else self.symbols
            )
            records = [_daily_basic(f"CN:STOCK:{s}", trade_date) for s in symbols]
            return ProviderBatch(records=records, source=self.source,
                                 raw_row_count=DAILY_ROW_CAP, truncation_risk=True)
        return self._primary("daily_basic", trade_date, _daily_basic)

    def get_daily_basic_for_instruments(self, trade_date, instruments, *, missing_ts_codes=None):
        """逐只补齐 Service 指名的缺失证券（daily_basic 不支持多代码参数）。"""
        self.daily_basic_missing_calls.append(missing_ts_codes)
        if missing_ts_codes is None:
            return self._fallback("daily_basic", trade_date, _daily_basic)
        self.fallback_calls.append(("daily_basic", trade_date))
        if self.daily_basic_supplement_error:
            raise TushareError("模拟补齐请求失败（测试注入）", error_code="TUSHARE_TIMEOUT")
        if self.daily_basic_supplement_empty or trade_date in self.fallback_fails.get(
            "daily_basic", set()
        ):
            return ProviderBatch(records=[], source=self.source, raw_row_count=0)
        records = [
            _daily_basic(f"CN:STOCK:{code.split('.')[0]}", trade_date)
            for code in missing_ts_codes
        ]
        return ProviderBatch(records=records, source=self.source, raw_row_count=len(records))

    def get_moneyflow(self, trade_date, instruments):
        return self._primary("moneyflow", trade_date, _moneyflow)

    def get_moneyflow_for_instruments(self, trade_date, instruments):
        return self._fallback("moneyflow", trade_date, _moneyflow)


class LateListingProviders(FakeHistoryProviders):
    """新上市证券场景（§35.1）：主档首次缺少 000002，daily 已返回它。

    第二次 ``get_stock_basic`` 才包含该证券——模拟"事实数据早于主档刷新"。
    """

    def __init__(self, **kwargs):
        super().__init__(symbols=("000001",), **kwargs)

    def get_stock_basic(self) -> ProviderBatch:
        self.stock_basic_calls += 1
        symbols = ("000001", "000002") if self.stock_basic_calls > 1 else ("000001",)
        records = [
            StockBasicRecord(
                ts_code=f"{s}.SZ", symbol=s, instrument_id=f"CN:STOCK:{s}",
                name=f"股票{s}", exchange="SZSE", list_status="L",
            )
            for s in symbols
        ]
        return ProviderBatch(records=records, source=self.source, raw_row_count=len(records))

    def get_daily(self, trade_date, instruments):
        self._maybe_fail("daily", trade_date)
        records = [_daily(f"CN:STOCK:{s}", trade_date) for s in ("000001", "000002")]
        return ProviderBatch(records=records, source=self.source, raw_row_count=len(records))


class NeverListedProviders(FakeHistoryProviders):
    """刷新主档后仍未知的证券：daily 恒返回主档中不存在的 CN:STOCK:999999。"""

    def get_daily(self, trade_date, instruments):
        self._maybe_fail("daily", trade_date)
        return ProviderBatch(
            records=[_daily("CN:STOCK:999999", trade_date)],
            source=self.source,
            raw_row_count=1,
        )


def _daily(instrument_id: str, trade_date: date) -> DailyBar:
    return DailyBar(
        instrument_id=instrument_id, ts_code=f"{instrument_id[-6:]}.SZ", trade_date=trade_date,
        open=10.0, high=11.0, low=9.5, close=10.5, vol=100.0, amount=200.0,
    )


def _adj_factor(instrument_id: str, trade_date: date) -> AdjFactor:
    return AdjFactor(
        instrument_id=instrument_id, ts_code=f"{instrument_id[-6:]}.SZ",
        trade_date=trade_date, adj_factor=1.0,
    )


def _daily_basic(instrument_id: str, trade_date: date) -> DailyBasic:
    return DailyBasic(
        instrument_id=instrument_id, ts_code=f"{instrument_id[-6:]}.SZ",
        trade_date=trade_date, close=10.5,
    )


def _moneyflow(instrument_id: str, trade_date: date) -> MoneyFlow:
    return MoneyFlow(
        instrument_id=instrument_id, ts_code=f"{instrument_id[-6:]}.SZ",
        trade_date=trade_date, net_mf_amount=5.0,
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
        providers: FakeHistoryProviders | None = None,
        calendar: FakeCalendarProvider | None = None,
        start_date: date = date(2026, 9, 10),
        max_attempts: int = 10,
    ) -> tuple[HistorySyncService, FakeHistoryProviders, FakeCalendarProvider, list[float]]:
        config = AppConfig()
        config.history.start_date = start_date
        config.history.max_attempts = max_attempts
        fake_providers = providers if providers is not None else FakeHistoryProviders()
        fake_calendar = calendar if calendar is not None else FakeCalendarProvider()
        slept: list[float] = []
        service = HistorySyncService(
            config, session_factory, fake_providers, fake_calendar,
            sleep=slept.append, random_fn=lambda: 0.5,
        )
        return service, fake_providers, fake_calendar, slept

    return _make


def _state(session_factory, dataset: DatasetName):
    with session_factory() as session:
        return HistorySyncStateRepository(session).get(dataset)


def _fact_rows(session_factory, dataset: DatasetName, trade_date: date) -> int:
    with session_factory() as session:
        from app.repositories.history_fact import HistoryFactRepository

        return HistoryFactRepository(session).count_for_date(dataset, trade_date)


def _ledger_rows(session_factory, dataset: DatasetName) -> list:
    with session_factory() as session:
        from sqlalchemy import select

        from app.models.history_sync import HistoryDayStatus

        return list(
            session.scalars(
                select(HistoryDayStatus).where(HistoryDayStatus.dataset == dataset.value)
                .order_by(HistoryDayStatus.trade_date)
            )
        )


# ---- §70.1 首次三天同步 ----


class TestFirstSync:
    def test_three_day_backfill_facts_ledger_watermark_count(self, make_service, session_factory):
        service, _providers, _calendar, _slept = make_service()
        run_id = service.run(trigger=TriggerType.MANUAL)

        for dataset in (
            DatasetName.DAILY, DatasetName.ADJ_FACTOR,
            DatasetName.DAILY_BASIC, DatasetName.MONEYFLOW,
        ):
            state = _state(session_factory, dataset)
            assert state.latest_complete_trade_date == date(2026, 9, 16)
            assert state.record_count == 3, "三交易日 × 1 证券"
            assert state.data_min_date == date(2026, 9, 14)
            assert state.data_max_date == date(2026, 9, 16)
            assert state.status == DatasetStatus.CAUGHT_UP.value

            ledger = _ledger_rows(session_factory, dataset)
            assert [row.trade_date for row in ledger] == OPEN_DAYS
            assert all(row.row_count == 1 for row in ledger)

            for day in OPEN_DAYS:
                assert _fact_rows(session_factory, dataset, day) == 1

        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
            assert run.status == RunStatus.SUCCESS.value
            assert run.finished_at is not None
            assert run.trigger_type == TriggerType.MANUAL.value

    def test_watermark_null_starts_from_first_open_day(self, make_service, session_factory):
        """§21.1：水位为 NULL 时从 history_start_date 起的第一个 open day 开始。"""
        service, _providers, _calendar, _ = make_service()
        service.run(trigger=TriggerType.SCHEDULED)
        state = _state(session_factory, DatasetName.DAILY)
        assert state.latest_complete_trade_date == date(2026, 9, 16)

    def test_no_work_is_noop(self, make_service, session_factory):
        """§19 Scenario "无事可做"：追平后再次触发为 NOOP，不写事实数据。"""
        service, providers, _calendar, _ = make_service()
        service.run(trigger=TriggerType.MANUAL)
        calls_before = len(providers.calls)

        run_id = service.run(trigger=TriggerType.MANUAL)
        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
            assert run.status == RunStatus.NOOP.value
        assert len(providers.calls) == calls_before, "NOOP 轮不得请求日级数据"

    def test_master_datasets_recorded_in_run_dataset(self, make_service, session_factory):
        """§20：run×dataset 执行详情覆盖主档数据集（页面"最近执行记录"数据源）。"""
        service, _providers, _calendar, _ = make_service()
        run_id = service.run(trigger=TriggerType.MANUAL)
        with session_factory() as session:
            rows = {
                row.dataset: row
                for row in HistorySyncRunDatasetRepository(session).list_for_run(run_id)
            }
        assert set(rows) == {ds.value for ds in DatasetName}
        for dataset in (DatasetName.TRADE_CAL, DatasetName.STOCK_BASIC):
            assert rows[dataset.value].status == RunDatasetStatus.SUCCESS.value


# ---- §70.2/§70.3 失败不跳日与恢复 ----


class TestFailureNeverSkips:
    def test_middle_failure_never_requests_later_day(self, make_service, session_factory):
        """§70.2（最重要的回归）：01-05 连续失败 → 水位停 01-04，01-06 从未被请求。"""
        providers = FakeHistoryProviders(
            fail_dates={"daily": {date(2026, 9, 15): 10}}
        )
        service, _p, _c, slept = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        state = _state(session_factory, DatasetName.DAILY)
        assert state.latest_complete_trade_date == date(2026, 9, 14)
        assert state.status == DatasetStatus.FAILED.value
        assert state.last_error_code == "TUSHARE_TIMEOUT"
        assert state.current_trade_date == date(2026, 9, 15)

        daily_calls = [d for ds, d in providers.calls if ds == "daily"]
        assert daily_calls.count(date(2026, 9, 15)) == 10, "该日恰好重试 10 次"
        assert date(2026, 9, 16) not in daily_calls, "失败日之后的日期绝不能被请求"
        assert len(slept) == 9, "10 次尝试之间最多 9 次等待"

        # 失败日的 ledger 不得写入，事实不得写入
        assert _fact_rows(session_factory, DatasetName.DAILY, date(2026, 9, 15)) == 0
        assert [row.trade_date for row in _ledger_rows(session_factory, DatasetName.DAILY)] == [
            date(2026, 9, 14)
        ]

    def test_other_datasets_unaffected_by_daily_failure(self, make_service, session_factory):
        """§70.2/§40.2：daily 失败不阻塞其他日级数据集，run=PARTIAL。"""
        providers = FakeHistoryProviders(fail_dates={"daily": {date(2026, 9, 15): 10}})
        service, _p, _c, _ = make_service(providers=providers)
        run_id = service.run(trigger=TriggerType.MANUAL)

        assert _state(session_factory, DatasetName.ADJ_FACTOR).latest_complete_trade_date == date(2026, 9, 16)
        assert _state(session_factory, DatasetName.DAILY_BASIC).latest_complete_trade_date == date(2026, 9, 16)
        assert _state(session_factory, DatasetName.MONEYFLOW).latest_complete_trade_date == date(2026, 9, 16)
        with session_factory() as session:
            assert HistorySyncRunRepository(session).get(run_id).status == RunStatus.PARTIAL.value

    def test_next_run_resumes_from_failed_day(self, make_service, session_factory):
        """§70.3：下一次运行从失败日继续并追平，最终连续完整。"""
        providers = FakeHistoryProviders(fail_dates={"daily": {date(2026, 9, 15): 10}})
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)
        assert _state(session_factory, DatasetName.DAILY).latest_complete_trade_date == date(2026, 9, 14)

        run_id = service.run(trigger=TriggerType.SCHEDULED)
        state = _state(session_factory, DatasetName.DAILY)
        assert state.latest_complete_trade_date == date(2026, 9, 16)
        assert state.status == DatasetStatus.CAUGHT_UP.value
        assert [row.trade_date for row in _ledger_rows(session_factory, DatasetName.DAILY)] == OPEN_DAYS
        with session_factory() as session:
            assert HistorySyncRunRepository(session).get(run_id).status == RunStatus.SUCCESS.value

    def test_config_error_fails_fast_without_exhausting_attempts(self, make_service, session_factory):
        """§29.3：配置类错误立即终态，不睡满 10 轮。"""
        providers = FakeHistoryProviders(
            fail_dates={"daily": {date(2026, 9, 14): 10}}, error_code="TUSHARE_PERMISSION_DENIED"
        )
        service, _p, _c, slept = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        daily_calls = [d for ds, d in providers.calls if ds == "daily"]
        assert daily_calls.count(date(2026, 9, 14)) == 1, "配置类错误只尝试一次"
        assert slept == [], "配置类错误不进入退避等待"
        state = _state(session_factory, DatasetName.DAILY)
        assert state.status == DatasetStatus.FAILED.value
        assert state.last_error_code == "TUSHARE_PERMISSION_DENIED"

    def test_retry_then_success_advances_watermark(self, make_service, session_factory):
        """第 3 次尝试成功：水位正常推进、retry_count 记为 2。"""
        providers = FakeHistoryProviders(fail_dates={"daily": {date(2026, 9, 14): 2}})
        service, _p, _c, slept = make_service(providers=providers)
        run_id = service.run(trigger=TriggerType.MANUAL)
        assert _state(session_factory, DatasetName.DAILY).latest_complete_trade_date == date(2026, 9, 16)
        assert len(slept) == 2
        with session_factory() as session:
            row = HistorySyncRunDatasetRepository(session).get(run_id, DatasetName.DAILY)
            assert row.retry_count == 2
            assert row.request_count >= 5, "3 天 + 2 次重试请求"


# ---- §70.4 数据集独立 ----


class TestUnknownInstrumentRecovery:
    """§35.1：事实数据出现主档未知 ts_code → 刷新一次 stock_basic → 重新映射。"""

    def test_refresh_and_remap_recovers_day(self, make_service, session_factory):
        providers = LateListingProviders()
        service, _p, _c, _slept = make_service(providers=providers)
        run_id = service.run(trigger=TriggerType.MANUAL)

        # 主档第一次没有 000002，daily 却返回它 → 触发一次 stock_basic 刷新后放行。
        # 刷新结果必须被同一数据集的后续交易日复用（§35.1 只"刷新一次"）：
        # 首个前置刷新 + 未知证券恢复刷新 = 2 次，不是每个交易日各一次。
        assert providers.stock_basic_calls == 2, "未知证券应恰好触发一次主档刷新"
        assert _fact_rows(session_factory, DatasetName.DAILY, date(2026, 9, 16)) == 2
        assert _state(session_factory, DatasetName.DAILY).latest_complete_trade_date == date(
            2026, 9, 16
        )
        with session_factory() as session:
            run_dataset = HistorySyncRunDatasetRepository(session).get(
                run_id, DatasetName.DAILY
            )
            assert run_dataset.status == RunDatasetStatus.SUCCESS.value
        # 恢复靠刷新主档，绝不创建 CN:STOCK:UNKNOWN 之类占位证券
        with session_factory() as session:
            ids = {i.instrument_id for i in HistoryMasterRepository(session)
                   .list_cn_stock_instruments()}
        assert "CN:STOCK:000002" in ids
        assert not [i for i in ids if "UNKNOWN" in i]

    def test_still_unknown_after_refresh_does_not_advance(
        self, make_service, session_factory
    ):
        """刷新后仍未知 → UNKNOWN_INSTRUMENT 终态，该交易日不推进水位（§35.1）。"""
        providers = NeverListedProviders()
        service, _p, _c, _slept = make_service(providers=providers)
        run_id = service.run(trigger=TriggerType.MANUAL)

        state = _state(session_factory, DatasetName.DAILY)
        assert state.last_error_code == "UNKNOWN_INSTRUMENT"
        assert state.latest_complete_trade_date is None, "首日即失败：水位不得推进"
        assert state.current_trade_date == date(2026, 9, 14)
        # 失败日之后的日子从未被请求（§70.2 不跳过原则）
        assert [d for ds, d in providers.calls if ds == "daily"] == [date(2026, 9, 14)] * 2
        for day in OPEN_DAYS:
            assert _fact_rows(session_factory, DatasetName.DAILY, day) == 0
        with session_factory() as session:
            row = HistorySyncRunDatasetRepository(session).get(run_id, DatasetName.DAILY)
            assert row.status == RunDatasetStatus.FAILED.value
            assert row.failed_trade_date == date(2026, 9, 14)
        assert providers.stock_basic_calls >= 2, "至少尝试过一次主档刷新"
        with session_factory() as session:
            ids = {i.instrument_id for i in HistoryMasterRepository(session)
                   .list_cn_stock_instruments()}
        assert not [i for i in ids if "999999" in i], "不得为未知证券建档"


class ProviderRaisesUnknownProviders(LateListingProviders):
    """§35.1 的另一种触发路径：未知证券在 Provider 边界（normalize 阶段）抛出。

    真实 Tushare Provider 无法把 ts_code 映射到主档时，会在 _build_daily_row
    里抛 UnknownInstrumentError（TushareError 子类），而不是走到校验层。
    该错误码属于 CONFIG_ERROR_CODES，若不识别这条路径，第 1 次尝试就会
    终态失败、水位不推进——恢复路径形同虚设。
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.unknown_raised = False

    def get_daily(self, trade_date, instruments):
        self._maybe_fail("daily", trade_date)
        self.calls.append(("daily", trade_date))
        if not self.unknown_raised:
            # 首日首次：Provider 侧映射失败（真实实现即在此抛）
            self.unknown_raised = True
            raise UnknownInstrumentError("ts_code 无法映射至证券主档: 000002.SZ")
        records = [_daily(f"CN:STOCK:{s}", trade_date) for s in ("000001", "000002")]
        return ProviderBatch(records=records, source=self.source, raw_row_count=len(records))


class TestProviderLevelUnknownInstrument:
    def test_provider_unknown_triggers_remap_and_recovers(
        self, make_service, session_factory
    ):
        """Provider 抛 UNKNOWN_INSTRUMENT 也必须走 §35.1 刷新+重映射，而非直接终态。"""
        providers = ProviderRaisesUnknownProviders()
        service, _p, _c, _slept = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        state = _state(session_factory, DatasetName.DAILY)
        assert state.last_error_code is None, (
            "Provider 边界未知证券应被刷新后的重映射恢复，不该终态失败"
        )
        assert state.latest_complete_trade_date == date(2026, 9, 16)
        assert providers.stock_basic_calls == 2, "恰好一次恢复刷新"

    def test_remap_on_final_attempt_still_records_terminal_failure(
        self, make_service, session_factory
    ):
        """回归：§35.1 恢复路径在最后一次尝试上不得留下非终态 state。

        旧实现刷新成功后无条件 ``continue``，在最后一次尝试上循环自然结束，
        直接落到裸 ``return "FAILED"``——state 停在 RETRYING/SYNCING、
        last_error_code=None，run_dataset 也只有 FAILED 无错误码，违反
        spec"10 次仍失败 SHALL 标记 FAILED（记录 failed_trade_date 与最后错误）"。
        """
        providers = ProviderRaisesUnknownProviders()
        service, _p, _c, _slept = make_service(providers=providers, max_attempts=1)
        run_id = service.run(trigger=TriggerType.MANUAL)

        state = _state(session_factory, DatasetName.DAILY)
        assert state.status == DatasetStatus.FAILED.value, (
            f"最后一次尝试出现未知证券必须终态失败，实际 {state.status}"
        )
        assert state.last_error_code == "UNKNOWN_INSTRUMENT"
        assert state.latest_complete_trade_date is None, "未推进不得动水位"
        with session_factory() as session:
            row = HistorySyncRunDatasetRepository(session).get(run_id, DatasetName.DAILY)
            assert row.status == RunDatasetStatus.FAILED.value
            assert row.last_error_code == "UNKNOWN_INSTRUMENT"
            assert row.failed_trade_date is not None

    def test_validate_phase_remap_on_final_attempt_records_terminal_failure(
        self, make_service, session_factory
    ):
        """校验层触发路径同样不得在最后一次尝试上留下非终态（与请求层对称）。"""
        providers = LateListingProviders()  # 校验层才会发现 000002 未知
        service, _p, _c, _slept = make_service(providers=providers, max_attempts=1)
        run_id = service.run(trigger=TriggerType.MANUAL)

        state = _state(session_factory, DatasetName.DAILY)
        assert state.status == DatasetStatus.FAILED.value
        assert state.last_error_code == "UNKNOWN_INSTRUMENT"
        assert state.latest_complete_trade_date is None
        with session_factory() as session:
            row = HistorySyncRunDatasetRepository(session).get(run_id, DatasetName.DAILY)
            assert row.status == RunDatasetStatus.FAILED.value
            assert row.last_error_code == "UNKNOWN_INSTRUMENT"


class MasterWriteFailsProviders(FakeHistoryProviders):
    """非阻塞主档落库阶段抛 SQLAlchemyError（如外键违约）的替身。"""

    def get_stock_company(self):
        from sqlalchemy.exc import IntegrityError

        raise IntegrityError("INSERT INTO cn_stock_company", {}, Exception("FK 违约"))


class TestNonBlockingMasterNeverBlocksDaily:
    def test_master_write_failure_does_not_abort_run(self, make_service, session_factory):
        """§40.1：stock_company/namechange 失败（含落库异常）不得阻塞日级数据集。

        回归：落库阶段的 SQLAlchemyError 曾穿透 run()，让整轮 run 被误记为
        "主档硬前置失败"，四个日级数据集一行未写。
        """
        providers = MasterWriteFailsProviders()
        service, _p, _c, _slept = make_service(providers=providers)
        run_id = service.run(trigger=TriggerType.MANUAL)

        assert _state(session_factory, DatasetName.DAILY).latest_complete_trade_date == date(
            2026, 9, 16
        ), "非阻塞主档失败不得阻止日级数据集推进"
        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
            company = HistorySyncStateRepository(session).get(DatasetName.STOCK_COMPANY)
        assert company.status == DatasetStatus.FAILED.value
        assert run.error_summary is None or "硬前置" not in (run.error_summary or ""), (
            "硬前置已成功，不得归因为硬前置失败"
        )


class TestDatasetIndependence:
    def test_each_dataset_has_own_watermark(self, make_service, session_factory):
        """§70.4：daily_basic 失败、其余三个正常推进到目标。"""
        providers = FakeHistoryProviders(
            fail_dates={"daily_basic": {date(2026, 9, 15): 10}}
        )
        service, _p, _c, _ = make_service(providers=providers)
        run_id = service.run(trigger=TriggerType.MANUAL)

        assert _state(session_factory, DatasetName.DAILY).latest_complete_trade_date == date(2026, 9, 16)
        assert _state(session_factory, DatasetName.ADJ_FACTOR).latest_complete_trade_date == date(2026, 9, 16)
        assert _state(session_factory, DatasetName.MONEYFLOW).latest_complete_trade_date == date(2026, 9, 16)
        basic = _state(session_factory, DatasetName.DAILY_BASIC)
        assert basic.latest_complete_trade_date == date(2026, 9, 14)
        assert basic.status == DatasetStatus.FAILED.value

        with session_factory() as session:
            assert HistorySyncRunRepository(session).get(run_id).status == RunStatus.PARTIAL.value
            row = HistorySyncRunDatasetRepository(session).get(run_id, DatasetName.DAILY_BASIC)
            assert row.failed_trade_date == date(2026, 9, 15)
            assert row.last_error_code == "TUSHARE_TIMEOUT"
            assert row.end_watermark == date(2026, 9, 14)


# ---- §70.5 事务回滚 ----


class TestAtomicRollback:
    """DELETE 后 / INSERT 后 / state 更新前注入异常：旧事实保留、水位不动。

    注入点在 Service 内部辅助对象的方法上（与 §70.5 的三处一一对应）。
    """

    def _seed_previous_run(self, service, session_factory):
        """先跑一轮把 09-14 落库，作为"旧数据"基线。"""
        service.run(trigger=TriggerType.MANUAL)

    def _rebuild_pending(self, session_factory, dataset: DatasetName = DatasetName.DAILY):
        """把水位回退一天，使 09-15 重新成为待处理日（模拟重跑同一日期）。"""
        with session_factory() as session:
            state = HistorySyncStateRepository(session).get(dataset)
            state.latest_complete_trade_date = date(2026, 9, 14)
            state.data_max_date = date(2026, 9, 14)
            session.commit()

    @pytest.mark.parametrize("point", ["after_delete", "after_insert", "before_state"])
    def test_exception_rolls_back_day_transaction(
        self, point, make_service, session_factory, monkeypatch
    ):
        service, _p, _c, _ = make_service()
        self._seed_previous_run(service, session_factory)
        self._rebuild_pending(session_factory)

        target = date(2026, 9, 15)
        rows_before = _fact_rows(session_factory, DatasetName.DAILY, target)
        assert rows_before == 1, "前置：该日已有旧事实"
        ledger_before = [row.trade_date for row in _ledger_rows(session_factory, DatasetName.DAILY)]

        from app.repositories.history_fact import HistoryFactRepository

        original_delete = HistoryFactRepository.delete_for_date
        original_insert = HistoryFactRepository.insert_records
        original_complete = HistorySyncStateRepository.complete_day

        if point == "after_delete":
            def boom_delete(self_, dataset, trade_date):
                original_delete(self_, dataset, trade_date)
                raise RuntimeError("注入：DELETE 之后失败")

            monkeypatch.setattr(HistoryFactRepository, "delete_for_date", boom_delete)
        elif point == "after_insert":
            def boom_insert(self_, dataset, records, **kwargs):
                original_insert(self_, dataset, records, **kwargs)
                raise RuntimeError("注入：INSERT 之后失败")

            monkeypatch.setattr(HistoryFactRepository, "insert_records", boom_insert)
        else:
            def boom_complete(self_, dataset, trade_date, **kwargs):
                raise RuntimeError("注入：state 更新前失败")

            monkeypatch.setattr(HistorySyncStateRepository, "complete_day", boom_complete)

        run_id = service.run(trigger=TriggerType.MANUAL)

        monkeypatch.undo()

        # 旧事实原样保留、该日不得写入新数据、水位与 ledger 不动
        assert _fact_rows(session_factory, DatasetName.DAILY, target) == 1
        state = _state(session_factory, DatasetName.DAILY)
        assert state.latest_complete_trade_date == date(2026, 9, 14)
        # ledger 一个字节都不动（§70.5：ledger 不错误推进）——失败日未被
        # 追加 COMPLETE，已存在的行也未被改写
        assert [
            row.trade_date for row in _ledger_rows(session_factory, DatasetName.DAILY)
        ] == ledger_before

        # 非预期异常不得逃出 run：run 必须落终态（不留 RUNNING），该数据集记
        # INTERNAL_ERROR，其余数据集不受影响（design.md 第 10 节"互不阻塞"）。
        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
            assert run.status != RunStatus.RUNNING.value
            assert run.finished_at is not None
        assert state.last_error_code == "INTERNAL_ERROR"
        # 其余数据集不受影响：daily 之外的三个仍推进到目标
        for other in (DatasetName.ADJ_FACTOR, DatasetName.DAILY_BASIC, DatasetName.MONEYFLOW):
            assert _state(
                session_factory, other
            ).latest_complete_trade_date == date(2026, 9, 16), f"{other.value} 应不受 daily 异常影响"

    def test_row_level_append_only_after_commit(self, make_service, session_factory):
        """成功提交后事实行数等于当日行数（无残留、无重复）。"""
        service, _p, _c, _ = make_service()
        service.run(trigger=TriggerType.MANUAL)
        for day in OPEN_DAYS:
            assert _fact_rows(session_factory, DatasetName.DAILY, day) == 1


# ---- §70.6 重复运行幂等 ----


class TestIdempotency:
    def test_rerun_same_day_does_not_double_count(self, make_service, session_factory):
        """§70.6 + §45：整日替换语义下 record_count += new - old，不翻倍。"""
        service, _p, _c, _ = make_service()
        service.run(trigger=TriggerType.MANUAL)
        before = _state(session_factory, DatasetName.DAILY)
        assert before.record_count == 3

        # 回退水位使 09-16 重新进入待处理列表（模拟人工重跑）
        with session_factory() as session:
            state = HistorySyncStateRepository(session).get(DatasetName.DAILY)
            state.latest_complete_trade_date = date(2026, 9, 15)
            state.data_max_date = date(2026, 9, 15)
            session.commit()

        service.run(trigger=TriggerType.MANUAL)
        state = _state(session_factory, DatasetName.DAILY)
        assert state.record_count == 3, "record_count 不得因重跑翻倍"
        assert _fact_rows(session_factory, DatasetName.DAILY, date(2026, 9, 16)) == 1
        assert len(_ledger_rows(session_factory, DatasetName.DAILY)) == len(OPEN_DAYS)

    def test_upstream_revision_replaces_day(self, make_service, session_factory):
        """§23：整日 DELETE+INSERT 吸收上游对历史日期的修订。"""
        service, providers, _c, _ = make_service()
        service.run(trigger=TriggerType.MANUAL)

        # 修订：该日证券集合变化（1 → 2 只）
        providers.symbols = ("000001", "600519")
        with session_factory() as session:
            from sqlalchemy import update

            from app.models.history_sync import HistorySyncState

            session.execute(
                update(HistorySyncState)
                .where(HistorySyncState.dataset == DatasetName.DAILY.value)
                .values(latest_complete_trade_date=date(2026, 9, 15))
            )
            session.commit()

        service.run(trigger=TriggerType.MANUAL)
        assert _fact_rows(session_factory, DatasetName.DAILY, date(2026, 9, 16)) == 2
        assert _state(session_factory, DatasetName.DAILY).record_count == 4


# ---- §70.7 截断 fallback ----


class TestTruncationFallback:
    def test_truncation_risk_triggers_fallback_and_commits(self, make_service, session_factory):
        """§33.2：主路径命中上限 → 不直接提交，走 fallback 后成功。"""
        providers = FakeHistoryProviders(truncate_dates={"daily": {date(2026, 9, 15)}})
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        assert ("daily", date(2026, 9, 15)) in providers.fallback_calls
        assert _fact_rows(session_factory, DatasetName.DAILY, date(2026, 9, 15)) == 1
        assert _state(session_factory, DatasetName.DAILY).latest_complete_trade_date == date(2026, 9, 16)

    def test_fallback_still_truncated_does_not_advance(self, make_service, session_factory):
        """§33.2/§70.7：fallback 仍不完整 → 按可重试错误重试，10 次后水位不动。"""
        providers = FakeHistoryProviders(
            truncate_dates={"daily": {date(2026, 9, 15)}},
            fallback_fails={"daily": {date(2026, 9, 15)}},
        )
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        state = _state(session_factory, DatasetName.DAILY)
        assert state.latest_complete_trade_date == date(2026, 9, 14)
        assert state.status == DatasetStatus.FAILED.value
        assert state.last_error_code == "TRUNCATION_RISK"
        assert len(providers.fallback_calls) == 10, "每次尝试都走 fallback"


# ---- daily_basic 候选集补齐（daily_basic 不支持多代码参数） ----


class TestDailyBasicCandidateFallback:
    """daily_basic 截断补齐只查"候选集 - 已返回代码"的缺失证券。

    回归（真实发现）：daily_basic 对逗号分隔的多 ts_code 会**静默返回空**
    （不报错），若复用了其他接口的 multi-code fallback，缺失交易日会被判成
    正常完成、水位照常推进，且没有任何错误码暴露问题。
    """

    def test_missing_instruments_are_supplied_by_candidate_set(
        self, make_service, session_factory
    ):
        """主路径只回 1 只、候选集有 3 只 → 只逐只补 2 只缺失，全并入库。"""
        providers = FakeHistoryProviders(
            symbols=("000001", "000002", "000003"),
            truncate_dates={"daily_basic": {date(2026, 9, 15)}},
        )
        # 主路径截断时只返回 000001（模拟被上限截掉的批次）
        providers.daily_basic_primary_symbols = ("000001",)
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        # 只对缺失的 000002/000003 逐只查询，不重复查已返回的 000001
        assert providers.daily_basic_missing_calls == [["000002.SZ", "000003.SZ"]]
        assert _fact_rows(
            session_factory, DatasetName.DAILY_BASIC, date(2026, 9, 15)
        ) == 3
        assert _state(
            session_factory, DatasetName.DAILY_BASIC
        ).latest_complete_trade_date == date(2026, 9, 16)

    def test_all_candidates_already_returned_skips_extra_requests(
        self, make_service, session_factory
    ):
        """候选集已全部返回（无缺失）→ 不逐只补查，水位照常推进。"""
        providers = FakeHistoryProviders(
            symbols=("000001",),
            truncate_dates={"daily_basic": {date(2026, 9, 15)}},
        )
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        assert providers.daily_basic_missing_calls == [[]]
        assert _fact_rows(
            session_factory, DatasetName.DAILY_BASIC, date(2026, 9, 15)
        ) == 1
        assert _state(
            session_factory, DatasetName.DAILY_BASIC
        ).latest_complete_trade_date == date(2026, 9, 16)

    def test_single_instrument_empty_result_is_explicit_and_completes(
        self, make_service, session_factory
    ):
        """补齐某只证券返回 0 行 = "明确空结果"，该日仍可 COMPLETE。

        §33.2：证券级自然缺失（停牌等）不等于日期级缺口。只有当补齐**请求
        本身异常**时才判该日不 COMPLETE（见下一个用例）。多代码静默空之所
        以危险，是因为它把"没查"伪装成"查了"——用逐只请求就不存在这个歧义。
        """
        providers = FakeHistoryProviders(
            symbols=("000001", "000002"),
            truncate_dates={"daily_basic": {date(2026, 9, 15)}},
        )
        providers.daily_basic_primary_symbols = ("000001",)
        providers.daily_basic_supplement_empty = True  # 该缺失证券当日无记录
        service, _p, _c, slept = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        state = _state(session_factory, DatasetName.DAILY_BASIC)
        assert state.latest_complete_trade_date == date(2026, 9, 16)
        assert state.status == DatasetStatus.CAUGHT_UP.value
        assert slept == [], "明确空结果不是错误，不进入退避重试"
        # 补齐是逐只请求：缺失证券被问过（得到明确空结果），不是被跳过
        assert providers.daily_basic_missing_calls == [["000002.SZ"]]
        assert _fact_rows(
            session_factory, DatasetName.DAILY_BASIC, date(2026, 9, 15)
        ) == 1  # 只有主路径返回的 000001 有记录

    def test_supplement_request_error_does_not_advance_watermark(
        self, make_service, session_factory
    ):
        """补齐中任一请求异常 → 该交易日不 COMPLETE、水位不推进。

        补齐是逐只串行请求，中途失败必须直接向上抛（不吞、不跳过），由重试
        编排按错误码重试；绝不允许把"没查完"当成"查完了"。
        """
        providers = FakeHistoryProviders(
            symbols=("000001", "000002"),
            truncate_dates={"daily_basic": {date(2026, 9, 15)}},
        )
        providers.daily_basic_primary_symbols = ("000001",)
        providers.daily_basic_supplement_error = True
        service, _p, _c, slept = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        state = _state(session_factory, DatasetName.DAILY_BASIC)
        assert state.latest_complete_trade_date == date(2026, 9, 14)
        assert state.status == DatasetStatus.FAILED.value
        assert state.last_error_code == "TUSHARE_TIMEOUT"
        assert len(slept) == 9
        # 补齐失败的日期不得留下半截事实数据
        assert _fact_rows(
            session_factory, DatasetName.DAILY_BASIC, date(2026, 9, 15)
        ) == 0

    def test_truncation_fallback_success_keeps_watermark_semantics(
        self, make_service, session_factory
    ):
        """补齐成功时水位语义不变：连续推进到目标日、ledger 全 COMPLETE。"""
        providers = FakeHistoryProviders(
            symbols=("000001", "000002"),
            truncate_dates={"daily_basic": {date(2026, 9, 15)}},
        )
        providers.daily_basic_primary_symbols = ("000001",)
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        ledger = _ledger_rows(session_factory, DatasetName.DAILY_BASIC)
        assert all(row.status == "COMPLETE" for row in ledger)
        assert _state(
            session_factory, DatasetName.DAILY_BASIC
        ).latest_complete_trade_date == date(2026, 9, 16)
        for day in (date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)):
            assert _fact_rows(session_factory, DatasetName.DAILY_BASIC, day) == 2


# ---- §34 空结果 ----


class TestEmptyResults:
    def test_historical_empty_result_fails_without_advancing(self, make_service, session_factory):
        """§34.1：历史日期 0 行 → EMPTY_RESULT 重试 10 次 → FAILED、水位不动。"""
        providers = FakeHistoryProviders(empty_dates={"daily": {date(2026, 9, 15)}})
        service, _p, _c, slept = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        state = _state(session_factory, DatasetName.DAILY)
        assert state.latest_complete_trade_date == date(2026, 9, 14)
        assert state.last_error_code == "EMPTY_RESULT"
        assert len(slept) == 9

    def test_today_empty_result_is_waiting_source(self, make_service, session_factory, frozen_now):
        """§34.2 + Scenario：当前交易日尚未发布 → WAITING_SOURCE，不判失败。

        冻结为 2026-09-16 20:00，09-16 是当日；该日返回 0 行时应为
        WAITING_SOURCE（水位停 09-15），而不是 EMPTY_RESULT 失败。
        """
        providers = FakeHistoryProviders(empty_dates={"daily": {date(2026, 9, 16)}})
        service, _p, _c, slept = make_service(providers=providers)
        run_id = service.run(trigger=TriggerType.MANUAL)

        state = _state(session_factory, DatasetName.DAILY)
        assert state.latest_complete_trade_date == date(2026, 9, 15)
        assert state.status == DatasetStatus.WAITING_SOURCE.value
        assert state.last_error_code == "WAITING_SOURCE"
        assert slept == [], "WAITING_SOURCE 不是错误，不进入退避重试"

        with session_factory() as session:
            row = HistorySyncRunDatasetRepository(session).get(run_id, DatasetName.DAILY)
            assert row.status == RunDatasetStatus.NOOP.value, "等待数据源不等于失败"
            assert row.failed_trade_date is None
        # 其他数据集不受影响
        assert _state(session_factory, DatasetName.ADJ_FACTOR).latest_complete_trade_date == date(2026, 9, 16)

    def test_waiting_source_resumes_next_run(self, make_service, session_factory):
        """§34.2：下一次任务继续——数据到位后同日成功。"""
        providers = FakeHistoryProviders(empty_dates={"daily": {date(2026, 9, 16)}})
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.SCHEDULED)
        assert _state(session_factory, DatasetName.DAILY).latest_complete_trade_date == date(2026, 9, 15)

        providers.empty_dates.pop("daily")
        service.run(trigger=TriggerType.SCHEDULED)
        assert _state(session_factory, DatasetName.DAILY).latest_complete_trade_date == date(2026, 9, 16)

    def test_before_cutoff_target_excludes_today(self, make_service, session_factory, frozen_now):
        """Scenario "当日未发布等待"：cutoff 未到 → 目标为上一交易日，当日不计失败。"""
        frozen_now(datetime(2026, 9, 16, 10, 0, tzinfo=BEIJING))  # daily cutoff 16:30 未到
        service, providers, _c, _ = make_service()
        run_id = service.run(trigger=TriggerType.MANUAL)

        state = _state(session_factory, DatasetName.DAILY)
        assert state.latest_expected_trade_date == date(2026, 9, 15)
        assert state.latest_complete_trade_date == date(2026, 9, 15)
        assert date(2026, 9, 16) not in [d for ds, d in providers.calls if ds == "daily"]
        with session_factory() as session:
            assert HistorySyncRunRepository(session).get(run_id).error_summary is None


# ---- §70.8 stale run 恢复 ----


class TestStaleRunRecovery:
    def test_stale_running_marked_interrupted_and_state_recovered(
        self, make_service, session_factory
    ):
        """§70.8/§50：遗留 RUNNING run → INTERRUPTED；SYNCING state → 基于水位恢复。"""
        service, _p, _c, _ = make_service()
        service.run(trigger=TriggerType.MANUAL)  # 先建立水位

        # 预置"上次进程异常退出"的现场
        with session_factory() as session:
            run_repo = HistorySyncRunRepository(session)
            run_repo.create(
                "stale-run-0001", trigger_type=TriggerType.SCHEDULED,
                requested_by_user_id=None, started_at=FROZEN_NOW,
            )
            state_repo = HistorySyncStateRepository(session)
            state_repo.set_expected(DatasetName.DAILY, date(2026, 9, 16))
            state_repo.mark_started(DatasetName.DAILY, status=DatasetStatus.SYNCING)
            state_repo.begin_attempt(
                DatasetName.DAILY, date(2026, 9, 16), 3, status=DatasetStatus.RETRYING
            )
            session.commit()

        service.run(trigger=TriggerType.STARTUP)

        with session_factory() as session:
            stale = HistorySyncRunRepository(session).get("stale-run-0001")
            assert stale.status == RunStatus.INTERRUPTED.value
            assert stale.finished_at is not None
            assert not HistorySyncRunRepository(session).find_stale_running(), "无遗留 RUNNING"

        state = _state(session_factory, DatasetName.DAILY)
        assert state.status in (DatasetStatus.CAUGHT_UP.value, DatasetStatus.LAGGING.value)
        assert state.latest_complete_trade_date == date(2026, 9, 16), "完成依据仍是水位/ledger"

    def test_recovery_does_not_mark_own_run_interrupted(self, make_service, session_factory):
        """新 run 自己创建的 RUNNING 行不得被自己的 recover 误标（exclude_run_id）。"""
        service, _p, _c, _ = make_service()
        run_id = service.run(trigger=TriggerType.MANUAL)
        with session_factory() as session:
            assert HistorySyncRunRepository(session).get(run_id).status == RunStatus.SUCCESS.value

    def test_interrupted_ledger_basis_resumes_from_watermark(self, make_service, session_factory):
        """§70.8：崩溃恢复后 catch-up 从水位继续，不重复推进已完成日。"""
        providers = FakeHistoryProviders(fail_dates={"moneyflow": {date(2026, 9, 16): 10}})
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)
        assert _state(session_factory, DatasetName.MONEYFLOW).latest_complete_trade_date == date(2026, 9, 15)

        with session_factory() as session:
            HistorySyncRunRepository(session).create(
                "stale-run-0002", trigger_type=TriggerType.SCHEDULED,
                requested_by_user_id=None, started_at=FROZEN_NOW,
            )
            session.commit()

        providers.fail_dates.clear()
        service.run(trigger=TriggerType.STARTUP)
        assert _state(session_factory, DatasetName.MONEYFLOW).latest_complete_trade_date == date(2026, 9, 16)
        # 已完成日不得被重复请求
        assert len(_ledger_rows(session_factory, DatasetName.MONEYFLOW)) == len(OPEN_DAYS)


# ---- 主档前置与 cancellation ----


class TestMasterPrerequisites:
    def test_calendar_unavailable_blocks_day_level(self, make_service, session_factory):
        """Scenario "硬前置失败阻止日级推进"：日历不可用 → FAILED 且不请求日级数据。"""
        calendar = FakeCalendarProvider(unavailable=True)
        service, providers, _c, _ = make_service(calendar=calendar)
        run_id = service.run(trigger=TriggerType.MANUAL)

        assert providers.calls == [], "硬前置失败不得请求日级数据集"
        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
            assert run.status == RunStatus.FAILED.value
            assert run.error_summary
        state = _state(session_factory, DatasetName.TRADE_CAL)
        assert state.last_error_code == "CALENDAR_UNAVAILABLE"

    def test_master_failure_recorded_but_not_fatal_for_company(self, make_service, session_factory):
        """company/namechange 失败不阻塞日级数据集（spec "主档前置与刷新周期"）。"""
        providers = FakeHistoryProviders()
        providers.get_stock_company = lambda: (_ for _ in ()).throw(
            TushareError("模拟公司主档失败", error_code="TUSHARE_API_ERROR")
        )
        service, _p, _c, _ = make_service(providers=providers)
        run_id = service.run(trigger=TriggerType.MANUAL)

        with session_factory() as session:
            assert HistorySyncRunRepository(session).get(run_id).status == RunStatus.SUCCESS.value
            row = HistorySyncRunDatasetRepository(session).get(run_id, DatasetName.STOCK_COMPANY)
            assert row.status == RunDatasetStatus.FAILED.value
            assert row.last_error_code == "TUSHARE_API_ERROR"
        assert _state(session_factory, DatasetName.DAILY).latest_complete_trade_date == date(2026, 9, 16)

    def test_namechange_bootstrap_advances_cursor_over_multiple_runs(self, make_service, session_factory):
        """§10.2：bootstrap 逐只推进，游标可跨运行续跑，最终置 bootstrap_complete。"""
        providers = FakeHistoryProviders(symbols=tuple(f"{i:06d}" for i in range(1, 8)))
        service, _p, _c, _ = make_service(providers=providers)

        from app.providers.base import StockBasicRecord  # noqa: F401

        # 建 7 只证券；NAMECHANGE_BOOTSTRAP_CHUNK_SIZE=50，一批即可完成
        service.run(trigger=TriggerType.MANUAL)
        state = _state(session_factory, DatasetName.NAMECHANGE)
        assert state.bootstrap_complete is True
        assert state.master_cursor == "000007"
        assert state.status == DatasetStatus.CAUGHT_UP.value

    def test_namechange_bootstrap_resumable_when_chunk_limited(
        self, make_service, session_factory, monkeypatch
    ):
        """分片受限时游标应停在本批最后一只，下一轮从下一只继续。"""
        monkeypatch.setattr(sync_module, "NAMECHANGE_BOOTSTRAP_CHUNK_SIZE", 3)
        providers = FakeHistoryProviders(symbols=tuple(f"{i:06d}" for i in range(1, 8)))
        service, _p, _c, _ = make_service(providers=providers)

        service.run(trigger=TriggerType.MANUAL)
        state = _state(session_factory, DatasetName.NAMECHANGE)
        assert state.master_cursor == "000003"
        assert state.bootstrap_complete is False

        service.run(trigger=TriggerType.MANUAL)
        state = _state(session_factory, DatasetName.NAMECHANGE)
        assert state.master_cursor == "000006"
        assert state.bootstrap_complete is False

        service.run(trigger=TriggerType.MANUAL)
        state = _state(session_factory, DatasetName.NAMECHANGE)
        assert state.master_cursor == "000007"
        assert state.bootstrap_complete is True


class TestCancellation:
    def test_cancellation_before_first_day_stops_dataset(self, make_service, session_factory):
        """§49：交易日开始前收到停机信号 → 该数据集本轮不推进、run=INTERRUPTED。"""
        import threading

        service, providers, _c, _ = make_service()
        event = threading.Event()
        event.set()
        run_id = service.run(trigger=TriggerType.MANUAL, cancellation_event=event)

        assert providers.calls == [], "停机后不得发起日级请求"
        with session_factory() as session:
            assert HistorySyncRunRepository(session).get(run_id).status == RunStatus.INTERRUPTED.value
        # 主档仍应完成（硬前置不受影响）
        assert _state(session_factory, DatasetName.STOCK_BASIC).last_success_at is not None

    def test_cancellation_mid_sequence_keeps_completed_progress(
        self, make_service, session_factory
    ):
        """§49：中途停机时已完成交易日进度保留，不停在"半日"状态。"""
        import threading

        providers = FakeHistoryProviders()
        service, _p, _c, _ = make_service(providers=providers)

        event = threading.Event()
        original = providers.get_daily
        seen: list[date] = []

        def cancelling_get_daily(trade_date, instruments):
            seen.append(trade_date)
            result = original(trade_date, instruments)
            if len(seen) == 2:  # 完成两天后置停机信号
                event.set()
            return result

        providers.get_daily = cancelling_get_daily
        run_id = service.run(trigger=TriggerType.MANUAL, cancellation_event=event)

        state = _state(session_factory, DatasetName.DAILY)
        assert state.latest_complete_trade_date == date(2026, 9, 15), "已完成日保留"
        assert [row.trade_date for row in _ledger_rows(session_factory, DatasetName.DAILY)] == [
            date(2026, 9, 14), date(2026, 9, 15)
        ]
        with session_factory() as session:
            assert HistorySyncRunRepository(session).get(run_id).status == RunStatus.INTERRUPTED.value


# ---- 水位对账（§25） ----


class TestReconcile:
    def test_reconcile_rolls_back_watermark_on_ledger_gap(self, make_service, session_factory):
        """§25/Scenario "账本缺口的保守回退"：09-15 缺 COMPLETE → 水位回退到 09-14。"""
        service, _p, _c, _ = make_service()
        service.run(trigger=TriggerType.MANUAL)

        # 人为制造缺口（删除中段 ledger 行）+ 保留水位
        with session_factory() as session:
            from sqlalchemy import delete

            from app.models.history_sync import HistoryDayStatus

            session.execute(
                delete(HistoryDayStatus).where(
                    HistoryDayStatus.dataset == DatasetName.DAILY.value,
                    HistoryDayStatus.trade_date == date(2026, 9, 15),
                )
            )
            session.commit()

        service.reconcile_daily_watermarks()
        assert _state(session_factory, DatasetName.DAILY).latest_complete_trade_date == date(2026, 9, 14)

        # 下一轮从缺口日重新同步（整日替换，事实表该日行数仍为 1）
        service.run(trigger=TriggerType.SCHEDULED)
        assert _state(session_factory, DatasetName.DAILY).latest_complete_trade_date == date(2026, 9, 16)
        assert _fact_rows(session_factory, DatasetName.DAILY, date(2026, 9, 15)) == 1

    def test_reconcile_dataset_diagnostic_does_not_write(self, make_service, session_factory):
        """§25：Service SHALL 提供内部 reconcile_dataset(dataset) 诊断能力。"""
        service, _p, _c, _ = make_service()
        service.run(trigger=TriggerType.MANUAL)
        before = _state(session_factory, DatasetName.DAILY).latest_complete_trade_date
        assert service.reconcile_dataset(DatasetName.DAILY) == before
        assert _state(session_factory, DatasetName.DAILY).latest_complete_trade_date == before

    def test_reconcile_detects_gap_for_diagnostics(self, make_service, session_factory):
        service, _p, _c, _ = make_service()
        service.run(trigger=TriggerType.MANUAL)
        with session_factory() as session:
            from sqlalchemy import delete

            from app.models.history_sync import HistoryDayStatus

            session.execute(
                delete(HistoryDayStatus).where(
                    HistoryDayStatus.dataset == DatasetName.ADJ_FACTOR.value,
                    HistoryDayStatus.trade_date == date(2026, 9, 15),
                )
            )
            session.commit()
        assert service.reconcile_dataset(DatasetName.ADJ_FACTOR) == date(2026, 9, 14)


# ---- 日志与错误文本（§62/§63、spec "同步日志规范"） ----


class TestLoggingAndSafety:
    def test_log_fields_present_on_completion(self, make_service, caplog):
        """§62：正常完成 INFO 日志含 run_id/dataset/trade_date/row_count/elapsed_ms。"""
        import logging

        service, _p, _c, _ = make_service()
        with caplog.at_level(logging.INFO, logger=sync_module.__name__):
            service.run(trigger=TriggerType.MANUAL)
        completions = [r for r in caplog.records if "数据集单日完成" in r.getMessage()]
        assert completions, "缺单日完成概要日志"
        text = completions[0].getMessage()
        for field in ("dataset=daily", "trade_date=", "row_count=", "elapsed_ms=", "run_id="):
            assert field in text, f"日志缺字段 {field}: {text}"

    def test_retry_warning_and_exhaustion_error_levels(self, make_service, session_factory, caplog):
        """§63：重试 WARNING、10 次失败 ERROR。"""
        import logging

        providers = FakeHistoryProviders(fail_dates={"daily": {date(2026, 9, 14): 10}})
        service, _p, _c, _ = make_service(providers=providers)
        with caplog.at_level(logging.INFO, logger=sync_module.__name__):
            service.run(trigger=TriggerType.MANUAL)

        retry_records = [r for r in caplog.records if "数据集单日尝试失败" in r.getMessage()]
        assert len(retry_records) == 10
        assert all(r.levelno == logging.WARNING for r in retry_records)
        exhausted = [r for r in caplog.records if "数据集单日判定失败" in r.getMessage()]
        assert len(exhausted) == 1
        assert exhausted[0].levelno == logging.ERROR
        assert "error_code=TUSHARE_TIMEOUT" in exhausted[0].getMessage()

    def test_no_token_in_logs_or_state(self, make_service, session_factory, caplog):
        """spec "日志不含 Token"：错误文本与日志不得出现 Token 明文。"""
        import logging

        secret = "abcdef0123456789abcdef0123456789abcdef01"
        providers = FakeHistoryProviders(fail_dates={"daily": {date(2026, 9, 14): 1}})
        providers.error_code = "TUSHARE_TOKEN_MISSING"
        original = providers._maybe_fail

        def leaky_fail(dataset, trade_date):
            providers.calls.append((dataset, trade_date))
            remaining = providers._remaining.get(dataset, {})
            if trade_date in remaining and remaining[trade_date] > 0:
                remaining[trade_date] -= 1
                raise TushareError(
                    f"Tushare Token 无效 token={secret}", error_code="TUSHARE_TOKEN_MISSING"
                )

        providers._maybe_fail = leaky_fail
        service, _p, _c, _ = make_service(providers=providers)
        with caplog.at_level(logging.INFO, logger=sync_module.__name__):
            service.run(trigger=TriggerType.MANUAL)

        for record in caplog.records:
            assert secret not in record.getMessage()
        state = _state(session_factory, DatasetName.DAILY)
        assert secret not in (state.last_error or "")
        assert "***" in (state.last_error or "")


# ---- 超时与前置阶段异常（评审发现的两处非预期异常穿透） ----


class TestUnexpectedExceptionContainment:
    """Provider 框架级超时与前置阶段异常都不得让 run 停在非终态。"""

    def test_builtin_timeout_is_retried_not_escaped(
        self, make_service, session_factory, monkeypatch
    ):
        """§27/§29：call_with_metrics 超时抛内建 TimeoutError（非 TushareError）。

        该异常必须被重试循环捕获并按 TUSHARE_TIMEOUT 计入，不得穿透 run()。
        """
        service, providers, _c, slept = make_service()

        def timeout_daily(trade_date, instruments):
            raise TimeoutError("请求超过 15 秒未完成")

        monkeypatch.setattr(providers, "get_daily", timeout_daily)
        run_id = service.run(trigger=TriggerType.MANUAL)

        assert len(slept) == service.retry_policy.max_attempts - 1, "应有 9 次退避"
        state = _state(session_factory, DatasetName.DAILY)
        assert state.status == DatasetStatus.FAILED.value
        assert state.last_error_code == "TUSHARE_TIMEOUT"
        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
            assert run.status != RunStatus.RUNNING.value
            assert run.finished_at is not None
        # 其余数据集不受超时影响
        assert _state(
            session_factory, DatasetName.MONEYFLOW
        ).latest_complete_trade_date == date(2026, 9, 16)

    def test_reconcile_failure_is_not_recorded_as_noop(
        self, make_service, session_factory, monkeypatch
    ):
        """评审发现：reconcile 阶段崩溃不得因"无数据集结果"被判成 NOOP。"""
        service, providers, _c, _ = make_service()

        def boom_reconcile():
            raise CalendarUnavailableError("严格交易日历不可用（对账阶段注入）")

        monkeypatch.setattr(service, "reconcile_daily_watermarks", boom_reconcile)
        run_id = service.run(trigger=TriggerType.MANUAL)

        assert providers.calls == [], "对账失败后不得请求日级数据"
        with session_factory() as session:
            run = HistorySyncRunRepository(session).get(run_id)
            assert run.status == RunStatus.FAILED.value
            assert run.error_summary
            assert run.finished_at is not None

    def test_unknown_exchange_does_not_block_day_level(self, make_service, session_factory):
        """namechange bootstrap 遇到脏 exchange 只记该主档失败，不中止整轮。

        评审发现：_exchange_suffix 曾抛裸 ValueError，穿透 ensure_master_
        prerequisites 的 SYNC_ERRORS 捕获，把非阻塞主档失败升级成整轮中止。
        """
        providers = FakeHistoryProviders()
        service, _p, _c, _ = make_service(providers=providers)

        # 主档里塞一只 exchange 为 NULL 的证券（Provider 与事实数据集不受影响）
        with session_factory() as session:
            session.add(
                Instrument(
                    instrument_id="CN:STOCK:999999", symbol="999999",
                    name="脏数据", market="CN", asset_type="STOCK", exchange=None,
                )
            )
            session.commit()

        run_id = service.run(trigger=TriggerType.MANUAL)

        # 四个日级数据集照常推进（namechange 失败不阻塞）
        for dataset in (DatasetName.DAILY, DatasetName.ADJ_FACTOR,
                        DatasetName.DAILY_BASIC, DatasetName.MONEYFLOW):
            assert _state(
                session_factory, dataset
            ).latest_complete_trade_date == date(2026, 9, 16), f"{dataset.value} 应照常推进"
        nc = _state(session_factory, DatasetName.NAMECHANGE)
        assert nc.last_error_code == "UNKNOWN_INSTRUMENT"
        with session_factory() as session:
            row = HistorySyncRunDatasetRepository(session).get(run_id, DatasetName.NAMECHANGE)
            assert row.status == RunDatasetStatus.FAILED.value

    def test_master_rows_record_request_and_row_counts(self, make_service, session_factory):
        """§20：主档 run_dataset 行必须记录真实行数/请求数，不能恒为 0。"""
        providers = FakeHistoryProviders(symbols=("000001", "600519", "000002"))
        service, _p, _c, _ = make_service(providers=providers)
        run_id = service.run(trigger=TriggerType.MANUAL)

        with session_factory() as session:
            basic = HistorySyncRunDatasetRepository(session).get(run_id, DatasetName.STOCK_BASIC)
            assert basic.rows_written == 3, "stock_basic 应记 3 行"
            assert basic.request_count >= 1
            cal = HistorySyncRunDatasetRepository(session).get(run_id, DatasetName.TRADE_CAL)
            assert cal.status == RunDatasetStatus.SUCCESS.value
            # namechange bootstrap 逐只替换：每只 1 条改名事件，同样必须计数
            nc = HistorySyncRunDatasetRepository(session).get(run_id, DatasetName.NAMECHANGE)
            assert nc is not None, "namechange 执行过就必须有 run_dataset 行"
            assert nc.rows_written == 3, "namechange 应记 3 行（每只证券 1 条）"
            assert nc.request_count == 3, "namechange 应记 3 次逐只请求"

class TestNonBlockingMasterRunCounts:
    def test_namechange_window_refresh_records_counts(self, make_service, session_factory):
        """§20：bootstrap 完成后的 7 天窗口增量同样写 run_dataset 计数。"""
        providers = FakeHistoryProviders(symbols=("000001",))
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)  # 首次：bootstrap

        state = _state(session_factory, DatasetName.NAMECHANGE)
        assert state.bootstrap_complete is True
        service._namechange_window_refresh("run-window", state)  # 直接驱动窗口分支

        with session_factory() as session:
            row = HistorySyncRunDatasetRepository(session).get(
                "run-window", DatasetName.NAMECHANGE
            )
            assert row is not None, "窗口增量必须留下 run_dataset 行"
            assert row.rows_written == 1
            assert row.request_count == 1


# ---- 批量写入（§44/§5.1） ----


class TestBatchWrite:
    def test_multi_symbol_day_written_in_single_transaction(self, make_service, session_factory):
        """§44：一日多证券走 Core executemany，单事务完成。"""
        providers = FakeHistoryProviders(symbols=tuple(f"{i:06d}" for i in range(1, 51)))
        service, _p, _c, _ = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        for day in OPEN_DAYS:
            assert _fact_rows(session_factory, DatasetName.DAILY, day) == 50
        assert _state(session_factory, DatasetName.DAILY).record_count == 150

    def test_fact_tables_reject_unknown_dataset_key(self, session_factory):
        from app.repositories.history_fact import HistoryFactRepository

        with session_factory() as session:
            with pytest.raises(ValueError, match="未知的事实数据集"):
                HistoryFactRepository(session).count_for_date("stock_basic", date(2026, 9, 16))

    def test_enum_dataset_key_accepted_by_fact_repository(self, session_factory):
        """DatasetName（str-mixin Enum）必须能直接传入事实仓储。"""
        from app.repositories.history_fact import HistoryFactRepository

        with session_factory() as session:
            repo = HistoryFactRepository(session)
            assert repo.count_for_date(DatasetName.DAILY, date(2026, 9, 16)) == 0
            assert DatasetName.DAILY.value in HISTORY_FACT_TABLES
