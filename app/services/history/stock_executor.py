"""单股单数据集同步执行器（per-stock-history-sync，design D7）。

``StockSyncExecutor`` 封装单股单数据集一次同步的完整生命周期：
写锁事务①创建 sync_task=running → 最多 max_retries+1 次 attempt
（锁外 fetch → 锁外 validate → 写锁内原子提交/失败记录）→
退避 sleep 前后检查 cancellation。终态：success（水位推进）或
failed（水位不动）。

网络请求与校验全部在写锁外完成；仅任务创建、成功提交、失败记录
三个短事务占用 WriteCoordinator（design D7"事务中途中断回滚"）。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable

from sqlalchemy.exc import SQLAlchemyError

from app.config import AppConfig
from app.db import write_coordinator
from app.models.history_sync import (
    DatasetName,
    TASK_STATUS_FAILED,
    TASK_STATUS_SUCCESS,
)
from app.models.instrument import Instrument
from app.providers.base import ProviderBatch
from app.providers.eastmoney_common import EastmoneyProviderError
from app.providers.history import HistoryProviderRegistry
from app.providers.tushare_common import TushareError
from app.providers.trading_calendar.provider import CalendarUnavailableError
from app.repositories.history_fact import HistoryFactRepository
from app.repositories.history_sync import (
    HistorySyncStateRepository,
    HistorySyncRunDatasetRepository,
    StockSyncStateRepository,
    SyncTaskRepository,
)
from app.services.history.retry import RetryPolicy, is_config_error
from app.services.history.validation import (
    HistoryValidationError,
    validate_batch,
)
from app.services.market_session_service import now_beijing

logger = logging.getLogger(__name__)

# 错误文本脱敏（与 sync_service._safe_error_text 同口径，executor 独立复用）
_TOKEN_PATTERN = re.compile(r"(?i)(token[\"'=:\s]*)([A-Za-z0-9]{16,})")


def _safe_error_text(exc: BaseException, *, limit: int = 500) -> str:
    """错误文本入库/入日志前的脱敏与截断（同步日志规范 spec）。"""
    text = _TOKEN_PATTERN.sub(r"\1***", str(exc))
    return text[:limit]


def _elapsed_ms(started: float) -> int:
    return max(int((time.monotonic() - started) * 1000), 0)


# 业务层可识别的同步异常（与 sync_service.SYNC_ERRORS 同口径）
SYNC_ERRORS: tuple[type[BaseException], ...] = (
    TushareError,
    HistoryValidationError,
    EastmoneyProviderError,
    CalendarUnavailableError,
    TimeoutError,
)


def _error_code_of(exc: BaseException) -> str:
    """异常 -> 标准化错误码（与 sync_service._error_code_of 同口径）。"""
    code = getattr(exc, "error_code", None)
    if isinstance(code, str):
        return code
    if isinstance(exc, TimeoutError):
        return "TUSHARE_TIMEOUT"
    return "INTERNAL_ERROR"


@dataclass
class TaskOutcome:
    """单股一次同步的结果摘要（供 Service 统计与日志）。"""

    status: str  # TASK_STATUS_SUCCESS / TASK_STATUS_FAILED
    task_id: int | None
    ts_code: str
    start_date: date
    end_date: date
    records_written: int
    records_fetched: int
    retry_count: int
    attempt_count: int
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: int = 0


class StockSyncExecutor:
    """单股单数据集同步的完整生命周期（design D7）。

    依赖注入：
        session_factory: SQLAlchemy sessionmaker
        providers: HistoryProviderRegistry
        retry_policy: RetryPolicy（已绑定 sleep/random 支持测试不真实等待）
        config: AppConfig（读取 history.start_date 等）
    """

    def __init__(
        self,
        config: AppConfig,
        session_factory,
        providers: HistoryProviderRegistry,
        retry_policy: RetryPolicy,
    ):
        self.config = config
        self.session_factory = session_factory
        self.providers = providers
        self.retry_policy = retry_policy

    # ---- 公开入口 ----

    def execute(
        self,
        *,
        run_id: str,
        dataset: DatasetName,
        instrument: Instrument,
        ts_code: str,
        start_date: date,
        end_date: date,
        list_date: date | None,
        delist_date: date | None,
        cancellation_event: threading.Event | None = None,
    ) -> TaskOutcome:
        """执行单股单数据集一次同步；返回 TaskOutcome。

        正常终态为 success 或 failed（均含 task_id）；数据库/框架级异常
        向上传播，由调用方决定是否终止整个 Run（个股失败隔离 spec：
        系统级异常终止 Run，个股业务异常只影响该股）。

        参数：
            run_id: 所属同步任务 ID
            dataset: 数据集名
            instrument: 证券快照（来自主档）
            ts_code: 本次请求使用的规范代码（冗余落 task）
            start_date: 请求区间起点（严格交易日）
            end_date: 请求区间终点（严格交易日）
            list_date: 上市日（用于 validate 生命周期校验；None 不约束）
            delist_date: 退市日（用于 validate 生命周期校验；None 不约束）
            cancellation_event: 停机信号；每次 attempt 边界与 sleep 前后检查
        """
        started_at = now_beijing()
        started_monotonic = time.monotonic()

        # ---- 写锁事务①：创建 running 状态 task ----
        task_id = self._create_task(
            run_id=run_id,
            dataset=dataset,
            instrument=instrument,
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            started_at=started_at,
        )

        retry_count = 0
        total_records_fetched = 0
        last_error_code: str | None = None
        last_error_msg: str | None = None

        for attempt in range(1, self.retry_policy.max_attempts + 1):
            # attempt 边界检查 cancellation
            if cancellation_event is not None and cancellation_event.is_set():
                # 收到停机信号：标记失败并返回（调用方会终止 Run）
                # 注意：这里不把 task 置 interrupted（那是恢复时的批量动作），
                # 而是直接 failed——当前 attempt 还没发出请求，干净返回。
                break

            attempt_started = time.monotonic()
            try:
                # ---- 锁外：fetch ----
                batch: ProviderBatch = self.providers.get_history_by_stock(
                    dataset.value, instrument, start_date, end_date
                )
                total_records_fetched = batch.raw_row_count

                # ---- 锁外：validate（区间模式）----
                validate_batch(
                    dataset,
                    batch,
                    date_range=(start_date, end_date),
                    lifecycle=(list_date, delist_date),
                    known_instrument_ids={instrument.instrument_id},
                )

                # ---- 写锁事务②：原子提交 ----
                finished_at = now_beijing()
                duration_ms = _elapsed_ms(started_monotonic)
                records_written = self._commit_success(
                    run_id=run_id,
                    dataset=dataset,
                    instrument_id=instrument.instrument_id,
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=end_date,
                    batch=batch,
                    task_id=task_id,
                    retry_count=retry_count,
                    attempt_count=attempt,
                    records_fetched=batch.raw_row_count,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                )

                logger.info(
                    "个股同步成功 run_id=%s dataset=%s ts_code=%s "
                    "row_count=%d elapsed_ms=%d attempt=%d",
                    run_id, dataset.value, ts_code,
                    records_written, duration_ms, attempt,
                )
                return TaskOutcome(
                    status=TASK_STATUS_SUCCESS,
                    task_id=task_id,
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=end_date,
                    records_written=records_written,
                    records_fetched=batch.raw_row_count,
                    retry_count=retry_count,
                    attempt_count=attempt,
                    duration_ms=duration_ms,
                )

            except SYNC_ERRORS as exc:
                error_code = _error_code_of(exc)
                error_msg = _safe_error_text(exc)
                last_error_code = error_code
                last_error_msg = error_msg
                elapsed_ms = _elapsed_ms(attempt_started)

                # 配置类错误：首试即终态，不睡满退避轮次
                if is_config_error(error_code):
                    logger.error(
                        "个股同步配置类错误，快速失败 run_id=%s dataset=%s "
                        "ts_code=%s attempt=%d error_code=%s elapsed_ms=%d: %s",
                        run_id, dataset.value, ts_code, attempt,
                        error_code, elapsed_ms, error_msg,
                    )
                    break

                # 已达最大尝试次数
                if attempt >= self.retry_policy.max_attempts:
                    logger.error(
                        "个股同步重试耗尽 run_id=%s dataset=%s ts_code=%s "
                        "attempt=%d error_code=%s elapsed_ms=%d: %s",
                        run_id, dataset.value, ts_code, attempt,
                        error_code, elapsed_ms, error_msg,
                    )
                    break

                # 可重试：WARNING 日志 + 退避等待
                retry_count = attempt  # 下一次就是第 attempt 次重试
                logger.warning(
                    "个股同步尝试失败，将重试 run_id=%s dataset=%s ts_code=%s "
                    "attempt=%d error_code=%s elapsed_ms=%d: %s",
                    run_id, dataset.value, ts_code, attempt,
                    error_code, elapsed_ms, error_msg,
                )

                if cancellation_event is not None and cancellation_event.is_set():
                    break
                self.retry_policy.sleep_before_retry(attempt)
                if cancellation_event is not None and cancellation_event.is_set():
                    break
                continue

            except SQLAlchemyError as exc:
                # 写锁事务失败：可重试（沿旧日级模型"单日事务失败有限重试"口径）
                error_code = "DATABASE_ERROR"
                error_msg = _safe_error_text(exc)
                last_error_code = error_code
                last_error_msg = error_msg
                elapsed_ms = _elapsed_ms(attempt_started)

                if attempt >= self.retry_policy.max_attempts:
                    logger.error(
                        "个股同步数据库错误重试耗尽 run_id=%s dataset=%s "
                        "ts_code=%s attempt=%d elapsed_ms=%d: %s",
                        run_id, dataset.value, ts_code, attempt,
                        elapsed_ms, error_msg,
                    )
                    break

                retry_count = attempt
                logger.warning(
                    "个股同步数据库错误，将重试 run_id=%s dataset=%s ts_code=%s "
                    "attempt=%d elapsed_ms=%d: %s",
                    run_id, dataset.value, ts_code, attempt,
                    elapsed_ms, error_msg,
                )
                if cancellation_event is not None and cancellation_event.is_set():
                    break
                self.retry_policy.sleep_before_retry(attempt)
                if cancellation_event is not None and cancellation_event.is_set():
                    break
                continue

        # ---- 失败终态：写锁事务③记录失败 ----
        finished_at = now_beijing()
        duration_ms = _elapsed_ms(started_monotonic)
        self._record_failure(
            run_id=run_id,
            dataset=dataset,
            instrument_id=instrument.instrument_id,
            ts_code=ts_code,
            task_id=task_id,
            retry_count=retry_count,
            attempt_count=min(self.retry_policy.max_attempts, retry_count + 1),
            records_fetched=total_records_fetched,
            error_code=last_error_code or "INTERNAL_ERROR",
            error_type=None,
            error_message=last_error_msg,
            finished_at=finished_at,
            duration_ms=duration_ms,
        )

        return TaskOutcome(
            status=TASK_STATUS_FAILED,
            task_id=task_id,
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            records_written=0,
            records_fetched=total_records_fetched,
            retry_count=retry_count,
            attempt_count=min(self.retry_policy.max_attempts, retry_count + 1),
            error_code=last_error_code or "INTERNAL_ERROR",
            error_message=last_error_msg,
            duration_ms=duration_ms,
        )

    # ---- 内部事务方法 ----

    def _create_task(
        self,
        *,
        run_id: str,
        dataset: DatasetName,
        instrument: Instrument,
        ts_code: str,
        start_date: date,
        end_date: date,
        started_at,
    ) -> int:
        """写锁事务①：创建 running 状态 sync_task。"""
        with write_coordinator.write():
            with self.session_factory() as session:
                task_repo = SyncTaskRepository(session)
                task = task_repo.create(
                    run_id=run_id,
                    dataset=dataset,
                    instrument_id=instrument.instrument_id,
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=end_date,
                    started_at=started_at,
                )
                session.commit()
                return task.id

    def _commit_success(
        self,
        *,
        run_id: str,
        dataset: DatasetName,
        instrument_id: str,
        ts_code: str,
        start_date: date,
        end_date: date,
        batch: ProviderBatch,
        task_id: int,
        retry_count: int,
        attempt_count: int,
        records_fetched: int,
        finished_at,
        duration_ms: int | None,
    ) -> int:
        """写锁事务②：成功原子提交。

        区间替换 + 水位推进 + task 终态 + run_dataset 计数 + 数据集级
        record_count 增减，全部在同一事务内。任何一步失败整体回滚。
        """
        with write_coordinator.write():
            with self.session_factory() as session:
                fetched_at = now_beijing()

                # 1. 事实区间替换
                fact_repo = HistoryFactRepository(session)
                old_count, inserted = fact_repo.replace_for_instrument_range(
                    dataset, instrument_id, start_date, end_date, batch.records,
                    source=self.providers.source, fetched_at=fetched_at,
                )

                # 2. 个股水位推进（单调不下降校验在 advance_watermark 内）
                stock_repo = StockSyncStateRepository(session)
                stock_repo.advance_watermark(
                    dataset, instrument_id,
                    new_watermark=end_date,
                    ts_code=ts_code,
                    last_task_id=task_id,
                    success_at=finished_at,
                )

                # 3. task 终态（duration_ms 为整任务时长，含全部重试）
                task_repo = SyncTaskRepository(session)
                task_repo.finish_success(
                    task_id,
                    retry_count=retry_count,
                    attempt_count=attempt_count,
                    records_fetched=records_fetched,
                    records_written=inserted,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                )

                # 4. run_dataset 计数（processed / succeeded / rows / requests / retries）
                run_repo = HistorySyncRunDatasetRepository(session)
                if run_repo.get(run_id, dataset) is not None:
                    run_repo.add_counts(
                        run_id, dataset,
                        processed=1,
                        succeeded=1,
                        rows=inserted,
                        requests=1,
                        retries=retry_count,
                    )

                # 5. 数据集级 record_count / data_min/max_date 增量维护
                state_repo = HistorySyncStateRepository(session)
                batch_dates = [r.trade_date for r in batch.records] if batch.records else []
                data_min = min(batch_dates) if batch_dates else None
                data_max = max(batch_dates) if batch_dates else None
                state_repo.apply_stock_range_delta(
                    dataset,
                    rows_delta=inserted - old_count,
                    data_min_date=data_min,
                    data_max_date=data_max,
                )

                session.commit()
                return inserted

    def _record_failure(
        self,
        *,
        run_id: str,
        dataset: DatasetName,
        instrument_id: str,
        ts_code: str,
        task_id: int,
        retry_count: int,
        attempt_count: int,
        records_fetched: int,
        error_code: str,
        error_type: str | None,
        error_message: str | None,
        finished_at,
        duration_ms: int | None,
    ) -> None:
        """写锁事务③：失败记录（水位绝不动）。"""
        with write_coordinator.write():
            with self.session_factory() as session:
                # 1. task 置 failed
                task_repo = SyncTaskRepository(session)
                task_repo.finish_failed(
                    task_id,
                    retry_count=retry_count,
                    attempt_count=attempt_count,
                    records_fetched=records_fetched,
                    error_code=error_code,
                    error_type=error_type,
                    error_message=error_message,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                )

                # 2. 个股 state 失败快照（水位不动）
                stock_repo = StockSyncStateRepository(session)
                stock_repo.record_failure(
                    dataset, instrument_id,
                    ts_code=ts_code,
                    last_task_id=task_id,
                    error_code=error_code,
                    error=error_message,
                    attempt_at=finished_at,
                )

                # 3. run_dataset 计数（processed / failed / requests / retries）
                run_repo = HistorySyncRunDatasetRepository(session)
                if run_repo.get(run_id, dataset) is not None:
                    run_repo.add_counts(
                        run_id, dataset,
                        processed=1,
                        failed=1,
                        requests=1,
                        retries=retry_count,
                    )

                session.commit()
