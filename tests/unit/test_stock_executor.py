"""StockSyncExecutor 单测（per-stock-history-sync，design D7）。

覆盖关键路径：
- 成功推进水位（单次尝试成功）
- 重试耗尽失败，水位不动
- 配置类错误（UNKNOWN_INSTRUMENT）首试即终态
- 空结果（0 行）合法成功，推进水位

使用真实 session_factory（DuckDB 临时文件）+ mock provider registry。
预置 instrument 与 cn_stock_basic 主档行。
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from app.config import AppConfig
from app.db import write_coordinator
from app.models.history_sync import (
    DatasetKind,
    DatasetName,
    StockSyncState,
    SyncTask,
    TASK_STATUS_FAILED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCESS,
)
from app.models.history_market import CnStockBasic
from app.models.instrument import Instrument
from app.providers.base import DailyBar, ProviderBatch
from app.providers.tushare_common import TushareError
from app.repositories.history_master import HistoryMasterRepository
from app.repositories.history_sync import (
    HistorySyncStateRepository,
    HistorySyncRunDatasetRepository,
    StockSyncStateRepository,
    SyncTaskRepository,
    HistorySyncRunRepository,
)
from app.services.history.retry import RetryPolicy
from app.services.history.stock_executor import StockSyncExecutor, TaskOutcome
from app.models.history_sync import TriggerType, RunStatus
from app.services.market_session_service import now_beijing


@pytest.fixture()
def test_instrument(session):
    inst = Instrument(
        instrument_id="CN:STOCK:600519",
        symbol="600519",
        name="贵州茅台",
        market="CN",
        asset_type="STOCK",
        currency="CNY",
        exchange="SSE",
        is_active=True,
    )
    session.add(inst)
    session.commit()
    return inst


@pytest.fixture()
def test_stock_basic(session, test_instrument):
    basic = CnStockBasic(
        instrument_id=test_instrument.instrument_id,
        ts_code="600519.SH",
        symbol="600519",
        name="贵州茅台",
        list_date=date(2001, 8, 27),
        delist_date=None,
        list_status="L",
        source="tushare",
        fetched_at=now_beijing(),
        source_last_seen_at=now_beijing(),
    )
    session.add(basic)
    session.commit()
    return basic


@pytest.fixture()
def run_id(session):
    """创建一个测试 run，供 run_dataset 计数用。"""
    from app.services.market_session_service import now_beijing

    rid = "test-run-" + "x" * 12
    with write_coordinator.write():
        repo = HistorySyncRunRepository(session)
        repo.create(
            rid,
            trigger_type=TriggerType.MANUAL,
            requested_by_user_id=None,
            started_at=now_beijing(),
        )
        # 预置 dataset 的 run_dataset 行（executor 才会 add_counts）
        rd_repo = HistorySyncRunDatasetRepository(session)
        rd_repo.start(
            rid, DatasetName.DAILY,
            start_watermark=None,
            target_trade_date=None,
            started_at=now_beijing(),
        )
        # 预置 history_sync_state 行（apply_stock_range_delta 需要）
        s_repo = HistorySyncStateRepository(session)
        s_repo.ensure(
            DatasetName.DAILY,
            dataset_kind=DatasetKind.DAILY_CONTIGUOUS,
            history_start_date=date(2010, 1, 1),
        )
        session.commit()
    return rid


class FakeNoSleepRetryPolicy(RetryPolicy):
    """RetypPolicy：sleep 不真实等待，便于快速测试重试逻辑。"""

    def __init__(self, config: AppConfig):
        super().__init__(config, sleep=lambda s: None, random_fn=lambda: 1.0)


class TestStockSyncExecutorSuccess:
    def test_success_advances_watermark_and_records_task(
        self, session_factory, test_instrument, test_stock_basic, run_id
    ):
        """单次尝试成功：水位推进到 end_date，task 成功，run_dataset 计数。"""
        config = AppConfig()
        mock_providers = MagicMock()
        mock_providers.source = "tushare"
        mock_providers.get_history_by_stock.return_value = ProviderBatch(
            records=[
                DailyBar(
                    instrument_id="CN:STOCK:600519",
                    ts_code="600519.SH",
                    trade_date=date(2026, 9, 15),
                    open=100.0, high=105.0, low=99.0, close=103.0,
                    vol=100000, amount=10000,
                ),
                DailyBar(
                    instrument_id="CN:STOCK:600519",
                    ts_code="600519.SH",
                    trade_date=date(2026, 9, 16),
                    open=103.0, high=106.0, low=102.0, close=105.0,
                    vol=120000, amount=12500,
                ),
            ],
            source="tushare",
            raw_row_count=2,
            truncation_risk=False,
        )

        retry = FakeNoSleepRetryPolicy(config)
        executor = StockSyncExecutor(config, session_factory, mock_providers, retry)

        outcome = executor.execute(
            run_id=run_id,
            dataset=DatasetName.DAILY,
            instrument=test_instrument,
            ts_code="600519.SH",
            start_date=date(2026, 9, 15),
            end_date=date(2026, 9, 16),
            list_date=date(2001, 8, 27),
            delist_date=None,
        )

        # 结果断言
        assert isinstance(outcome, TaskOutcome)
        assert outcome.status == TASK_STATUS_SUCCESS
        assert outcome.records_written == 2
        assert outcome.records_fetched == 2
        assert outcome.attempt_count == 1
        assert outcome.retry_count == 0
        assert outcome.task_id is not None

        # 水位断言
        with session_factory() as s:
            state = StockSyncStateRepository(s).get(
                DatasetName.DAILY, test_instrument.instrument_id
            )
            assert state is not None
            assert state.watermark_date == date(2026, 9, 16)
            assert state.last_status == TASK_STATUS_SUCCESS
            assert state.last_task_id == outcome.task_id

        # task 断言
        with session_factory() as s:
            task = SyncTaskRepository(s).find_by_id(outcome.task_id)
            assert task is not None
            assert task.status == TASK_STATUS_SUCCESS
            assert task.records_written == 2
            assert task.records_fetched == 2
            assert task.attempt_count == 1
            assert task.retry_count == 0
            assert task.error_code is None
            assert task.duration_ms is not None  # 整任务时长落库（非 NULL）

        # run_dataset 计数断言
        with session_factory() as s:
            rd = HistorySyncRunDatasetRepository(s).get(run_id, DatasetName.DAILY)
            assert rd is not None
            assert rd.processed_count == 1
            assert rd.task_success_count == 1
            assert rd.task_failed_count == 0
            assert rd.rows_written == 2


class TestStockSyncExecutorRetryExhaustion:
    def test_retry_exhaustion_fails_and_watermark_unchanged(
        self, session_factory, test_instrument, test_stock_basic, run_id
    ):
        """重试耗尽：task 失败、水位不动、记录错误码。"""
        config = AppConfig()
        # 确认 max_retries=3 → 总尝试 4 次
        assert config.history.max_retries == 3

        mock_providers = MagicMock()
        mock_providers.source = "tushare"
        mock_providers.get_history_by_stock.side_effect = TushareError(
            "上游超时", error_code="TUSHARE_TIMEOUT"
        )

        retry = FakeNoSleepRetryPolicy(config)
        executor = StockSyncExecutor(config, session_factory, mock_providers, retry)

        outcome = executor.execute(
            run_id=run_id,
            dataset=DatasetName.DAILY,
            instrument=test_instrument,
            ts_code="600519.SH",
            start_date=date(2026, 9, 15),
            end_date=date(2026, 9, 16),
            list_date=date(2001, 8, 27),
            delist_date=None,
        )

        assert outcome.status == TASK_STATUS_FAILED
        assert outcome.error_code == "TUSHARE_TIMEOUT"
        assert outcome.attempt_count == 4  # max_retries + 1
        assert outcome.retry_count == 3

        # 水位不动（初始无水位 → 仍为 None）
        with session_factory() as s:
            state = StockSyncStateRepository(s).get(
                DatasetName.DAILY, test_instrument.instrument_id
            )
            assert state is not None
            assert state.watermark_date is None
            assert state.last_status == TASK_STATUS_FAILED
            assert state.last_error_code == "TUSHARE_TIMEOUT"

        # task 断言
        with session_factory() as s:
            task = SyncTaskRepository(s).find_by_id(outcome.task_id)
            assert task is not None
            assert task.status == TASK_STATUS_FAILED
            assert task.error_code == "TUSHARE_TIMEOUT"
            assert task.attempt_count == 4
            assert task.retry_count == 3
            assert task.duration_ms is not None

        # provider 被调用次数 = 总尝试次数
        assert mock_providers.get_history_by_stock.call_count == 4


class TestStockSyncExecutorConfigError:
    def test_config_error_terminal_on_first_attempt(
        self, session_factory, test_instrument, test_stock_basic, run_id
    ):
        """配置类错误（UNKNOWN_INSTRUMENT）：首试即终态，只请求一次。"""
        config = AppConfig()
        mock_providers = MagicMock()
        mock_providers.source = "tushare"
        mock_providers.get_history_by_stock.side_effect = TushareError(
            "未知证券", error_code="UNKNOWN_INSTRUMENT"
        )

        retry = FakeNoSleepRetryPolicy(config)
        executor = StockSyncExecutor(config, session_factory, mock_providers, retry)

        outcome = executor.execute(
            run_id=run_id,
            dataset=DatasetName.DAILY,
            instrument=test_instrument,
            ts_code="999999.SH",
            start_date=date(2026, 9, 15),
            end_date=date(2026, 9, 16),
            list_date=date(2001, 8, 27),
            delist_date=None,
        )

        assert outcome.status == TASK_STATUS_FAILED
        assert outcome.error_code == "UNKNOWN_INSTRUMENT"
        assert outcome.attempt_count == 1
        assert outcome.retry_count == 0

        # 只请求了一次（不睡满退避轮次）
        assert mock_providers.get_history_by_stock.call_count == 1

        # 水位不动
        with session_factory() as s:
            state = StockSyncStateRepository(s).get(
                DatasetName.DAILY, test_instrument.instrument_id
            )
            assert state.watermark_date is None
            assert state.last_error_code == "UNKNOWN_INSTRUMENT"


class TestStockSyncExecutorEmptyResult:
    def test_empty_batch_is_valid_success(
        self, session_factory, test_instrument, test_stock_basic, run_id
    ):
        """空结果（0 行无异常）：合法成功，推进水位，records_fetched=0。"""
        config = AppConfig()
        mock_providers = MagicMock()
        mock_providers.source = "tushare"
        mock_providers.get_history_by_stock.return_value = ProviderBatch(
            records=[],
            source="tushare",
            raw_row_count=0,
            truncation_risk=False,
        )

        retry = FakeNoSleepRetryPolicy(config)
        executor = StockSyncExecutor(config, session_factory, mock_providers, retry)

        outcome = executor.execute(
            run_id=run_id,
            dataset=DatasetName.DAILY,
            instrument=test_instrument,
            ts_code="600519.SH",
            start_date=date(2026, 9, 15),
            end_date=date(2026, 9, 16),
            list_date=date(2001, 8, 27),
            delist_date=None,
        )

        assert outcome.status == TASK_STATUS_SUCCESS
        assert outcome.records_fetched == 0
        assert outcome.records_written == 0
        assert outcome.attempt_count == 1

        # 水位推进到区间终点
        with session_factory() as s:
            state = StockSyncStateRepository(s).get(
                DatasetName.DAILY, test_instrument.instrument_id
            )
            assert state.watermark_date == date(2026, 9, 16)
            assert state.last_status == TASK_STATUS_SUCCESS

        # task 成功
        with session_factory() as s:
            task = SyncTaskRepository(s).find_by_id(outcome.task_id)
            assert task.status == TASK_STATUS_SUCCESS
            assert task.records_fetched == 0
            assert task.records_written == 0
