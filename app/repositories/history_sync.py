"""历史同步控制仓储（a-share-historical-data §17~§21、§25；per-stock-history-sync D2）。

控制表的读写：
- ``HistorySyncStateRepository``：state 读写与数据集级运营字段（个股模式下
  日级数据集的 record_count 按单股区间 new-old 增减维护）；
- ``HistoryDayStatusRepository``：day ledger upsert 与 reconcile 扫描（旧日级
  模型遗留，个股模式停写、历史行保留只读）；
- ``HistorySyncRunRepository`` / ``HistorySyncRunDatasetRepository``：run 与
  run×dataset 生命周期，含 stale RUNNING 恢复标记（§19 INTERRUPTED）与
  个股口径统计列（processed/success/failed/skipped）；
- ``StockSyncStateRepository``（v0.4.0）：个股水位表——写锁内 get-or-create、
  水位单调不下降推进、批量补建、落后优先 universe 查询；
- ``SyncTaskRepository``（v0.4.0）：个股任务流水——create/终态/中断批量标记。

全部方法在调用方事务内执行（单股区间原子提交的事务边界由
StockSyncExecutor 在 WriteCoordinator 内组合调用）。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Iterable

from sqlalchemy import nulls_first, select, update
from sqlalchemy.orm import Session

from app.models.history_sync import (
    DAY_STATUS_COMPLETE,
    DatasetKind,
    DatasetName,
    DatasetStatus,
    HistoryDayStatus,
    HistorySyncRun,
    HistorySyncRunDataset,
    HistorySyncState,
    RunDatasetStatus,
    RunStatus,
    StockSyncState,
    SyncTask,
    TASK_STATUS_FAILED,
    TASK_STATUS_INTERRUPTED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCESS,
    TriggerType,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _key(value: DatasetName | str) -> str:
    """枚举安全转主键字符串（str-mixin Enum 的 str() 含类名，不可直接用）。"""
    return value.value if isinstance(value, DatasetName) else str(value)


def _enum_value(value) -> str:
    return value.value if isinstance(value, Enum) else str(value)


class HistorySyncStateRepository:
    """每数据集一行的水位与状态。"""

    def __init__(self, session: Session):
        self.session = session

    def get(self, dataset: DatasetName | str) -> HistorySyncState | None:
        return self.session.get(HistorySyncState, _key(dataset))

    def ensure(
        self,
        dataset: DatasetName | str,
        *,
        dataset_kind: DatasetKind,
        history_start_date: date | None = None,
    ) -> HistorySyncState:
        """不存在则建 UNINITIALIZED 行（幂等）。"""
        state = self.get(dataset)
        if state is None:
            state = HistorySyncState(
                dataset=_key(dataset),
                dataset_kind=_enum_value(dataset_kind),
                status=DatasetStatus.UNINITIALIZED.value,
                history_start_date=history_start_date,
                updated_at=_utcnow(),
            )
            self.session.add(state)
            # flush 使行立即可见（同事务内后续 update 定位）
            self.session.flush()
        return state

    def set_expected(self, dataset: DatasetName | str, expected: date) -> None:
        """记录本轮目标（latest_expected_trade_date，页面展示用）。"""
        self.session.execute(
            update(HistorySyncState)
            .where(HistorySyncState.dataset == _key(dataset))
            .values(latest_expected_trade_date=expected, updated_at=_utcnow())
        )

    def complete_day(
        self,
        dataset: DatasetName | str,
        trade_date: date,
        *,
        rows_delta: int,
        status: DatasetStatus,
    ) -> None:
        """单日成功事务（§22 步骤 10）：推水位、清当前尝试、累计计数。"""
        state = self.get(dataset)
        if state is None:
            raise ValueError(f"history_sync_state 缺少数据集 {dataset}，须先 ensure")
        state.latest_complete_trade_date = trade_date
        state.current_trade_date = None
        state.current_attempt = 0
        state.record_count += rows_delta
        state.data_min_date = (
            trade_date
            if state.data_min_date is None
            else min(state.data_min_date, trade_date)
        )
        state.data_max_date = (
            trade_date
            if state.data_max_date is None
            else max(state.data_max_date, trade_date)
        )
        state.status = _enum_value(status)
        state.updated_at = _utcnow()
        self.session.flush()

    def begin_attempt(
        self,
        dataset: DatasetName | str,
        trade_date: date,
        attempt: int,
        *,
        status: DatasetStatus,
    ) -> None:
        """重试编排状态（§29）：当前日、尝试次数、RETRYING。"""
        self.session.execute(
            update(HistorySyncState)
            .where(HistorySyncState.dataset == _key(dataset))
            .values(
                current_trade_date=trade_date,
                current_attempt=attempt,
                status=_enum_value(status),
                updated_at=_utcnow(),
            )
        )

    def finish_error(
        self,
        dataset: DatasetName | str,
        *,
        error_code: str,
        error: str,
        status: DatasetStatus = DatasetStatus.FAILED,
    ) -> None:
        """失败终态（10 次失败 ERROR，§29.4）；current_trade_date 保留供页面展示。"""
        now = _utcnow()
        self.session.execute(
            update(HistorySyncState)
            .where(HistorySyncState.dataset == _key(dataset))
            .values(
                status=_enum_value(status),
                last_error_at=now,
                last_error_code=error_code,
                last_error=error,
                updated_at=now,
            )
        )

    def finish_success(self, dataset: DatasetName | str, *, status: DatasetStatus) -> None:
        """数据集本轮成功（追平目标或已追上），并清除上一次失败留下的错误字段。

        失败后再成功时，``last_error_code``/``last_error`` 会被管理员页面
        当作"最后错误"展示，若不清理就会出现 "CAUGHT_UP + 陈旧错误码" 的
        自相矛盾状态（现场实测：stock_basic 已追平却仍显示 TUSHARE_TIMEOUT）。
        ``last_error_at`` 同为失败快照的一部分，一并清除避免留下"有错误时刻、
        无错误码"的幽灵记录。

        历史错误不丢：``history_sync_run_dataset`` 在失败终态时已抄写
        ``last_error_code``/``last_error`` 并带 ``finished_at`` 时间戳，仍可查询。
        注意与 ``job_status`` 表的语义相反——那张表的 ``record_success`` 按
        job-status spec 刻意保留 ``last_error``（"失败不清除最近成功"的另一面），
        两者是不同表的独立契约，勿一并"修正"。

        ``current_trade_date``/``current_attempt`` 也在此回到无在途状态：
        它们是"当前正在处理哪一天、第几次尝试"的展示字段，``complete_day``
        在单日推进时已做同样清理，追平后两者都应归零而非停留在最后一次
        失败的日期上。
        """
        now = _utcnow()
        self.session.execute(
            update(HistorySyncState)
            .where(HistorySyncState.dataset == _key(dataset))
            .values(
                status=_enum_value(status),
                last_success_at=now,
                last_error_at=None,
                last_error_code=None,
                last_error=None,
                current_trade_date=None,
                current_attempt=0,
                updated_at=now,
            )
        )

    def apply_stock_range_delta(
        self,
        dataset: DatasetName | str,
        *,
        rows_delta: int,
        data_min_date: date | None,
        data_max_date: date | None,
    ) -> None:
        """单股区间替换后的数据集级统计增减（per-stock-history-sync，historical-data-storage spec）。

        个股模式下日级数据集的 ``record_count`` 按每笔区间替换的
        ``new_count - old_count`` 累计（可为负——上游删数据时收缩）；
        ``data_min_date``/``data_max_date`` 保守扩张不收缩（与旧
        ``complete_day`` 的 min/max 语义一致）。由 StockSyncExecutor 在
        单股成功提交事务内调用。
        """
        state = self.get(dataset)
        if state is None:
            raise ValueError(f"history_sync_state 缺少数据集 {dataset}，须先 ensure")
        state.record_count += rows_delta
        if data_min_date is not None:
            state.data_min_date = (
                data_min_date
                if state.data_min_date is None
                else min(state.data_min_date, data_min_date)
            )
        if data_max_date is not None:
            state.data_max_date = (
                data_max_date
                if state.data_max_date is None
                else max(state.data_max_date, data_max_date)
            )
        state.updated_at = _utcnow()
        self.session.flush()

    def update_master(
        self,
        dataset: DatasetName | str,
        *,
        record_count: int | None = None,
        data_min_date: date | None = None,
        data_max_date: date | None = None,
        master_cursor: str | None = None,
        bootstrap_complete: bool | None = None,
    ) -> None:
        """主档数据集刷新结果（None 字段不更新）。"""
        values: dict = {"updated_at": _utcnow()}
        if record_count is not None:
            values["record_count"] = record_count
        if data_min_date is not None:
            values["data_min_date"] = data_min_date
        if data_max_date is not None:
            values["data_max_date"] = data_max_date
        if master_cursor is not None:
            values["master_cursor"] = master_cursor
        if bootstrap_complete is not None:
            values["bootstrap_complete"] = bootstrap_complete
        self.session.execute(
            update(HistorySyncState)
            .where(HistorySyncState.dataset == _key(dataset))
            .values(**values)
        )

    def mark_started(self, dataset: DatasetName | str, *, status: DatasetStatus) -> None:
        self.session.execute(
            update(HistorySyncState)
            .where(HistorySyncState.dataset == _key(dataset))
            .values(
                status=_enum_value(status),
                last_started_at=_utcnow(),
                updated_at=_utcnow(),
            )
        )

    def all_states(self) -> list[HistorySyncState]:
        return list(self.session.scalars(select(HistorySyncState)))


class HistoryDayStatusRepository:
    """日级连续完成账本（§18，仅记 COMPLETE）。"""

    def __init__(self, session: Session):
        self.session = session

    def upsert_complete(
        self,
        dataset: DatasetName | str,
        trade_date: date,
        *,
        row_count: int,
        run_id: str,
        fetched_at: datetime,
    ) -> None:
        """重跑同日覆盖（整日替换语义）。"""
        row = self.session.get(HistoryDayStatus, (_key(dataset), trade_date))
        if row is None:
            self.session.add(
                HistoryDayStatus(
                    dataset=_key(dataset),
                    trade_date=trade_date,
                    status=DAY_STATUS_COMPLETE,
                    row_count=row_count,
                    fetched_at=fetched_at,
                    completed_at=_utcnow(),
                    completed_by_run_id=run_id,
                )
            )
            self.session.flush()
        else:
            row.status = DAY_STATUS_COMPLETE
            row.row_count = row_count
            row.fetched_at = fetched_at
            row.completed_at = _utcnow()
            row.completed_by_run_id = run_id
            self.session.flush()

    def has_complete(self, dataset: DatasetName | str, trade_date: date) -> bool:
        row = self.session.get(HistoryDayStatus, (_key(dataset), trade_date))
        return row is not None and row.status == DAY_STATUS_COMPLETE

    def completed_dates(
        self,
        dataset: DatasetName | str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> set[date]:
        """reconcile 扫描（§25.1）：范围内已有 COMPLETE 账本的日期集合。"""
        stmt = select(HistoryDayStatus.trade_date).where(
            HistoryDayStatus.dataset == _key(dataset),
            HistoryDayStatus.status == DAY_STATUS_COMPLETE,
        )
        if start is not None:
            stmt = stmt.where(HistoryDayStatus.trade_date >= start)
        if end is not None:
            stmt = stmt.where(HistoryDayStatus.trade_date <= end)
        return set(self.session.scalars(stmt))


class HistorySyncRunRepository:
    """同步任务（§19）。"""

    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        run_id: str,
        *,
        trigger_type: TriggerType,
        requested_by_user_id: str | None,
        started_at: datetime,
    ) -> HistorySyncRun:
        run = HistorySyncRun(
            run_id=run_id,
            trigger_type=_enum_value(trigger_type),
            requested_by_user_id=requested_by_user_id,
            status=RunStatus.RUNNING.value,
            started_at=started_at,
            created_at=started_at,
        )
        self.session.add(run)
        self.session.flush()
        return run

    def get(self, run_id: str) -> HistorySyncRun | None:
        return self.session.get(HistorySyncRun, run_id)

    def finish(
        self,
        run_id: str,
        *,
        status: RunStatus,
        finished_at: datetime,
        error_summary: str | None = None,
    ) -> None:
        run = self.get(run_id)
        if run is None:
            raise ValueError(f"history_sync_run 不存在: {run_id}")
        run.status = _enum_value(status)
        run.finished_at = finished_at
        run.error_summary = error_summary
        self.session.flush()

    def find_stale_running(self) -> list[HistorySyncRun]:
        """启动恢复（§19）：仍为 RUNNING 的历史 run（进程内单写者，出现即 stale）。"""
        return list(
            self.session.scalars(
                select(HistorySyncRun).where(
                    HistorySyncRun.status == RunStatus.RUNNING.value
                )
            )
        )

    def mark_interrupted(self, run_id: str, *, finished_at: datetime) -> None:
        run = self.get(run_id)
        if run is None:
            raise ValueError(f"history_sync_run 不存在: {run_id}")
        run.status = RunStatus.INTERRUPTED.value
        run.finished_at = finished_at
        self.session.flush()

    def list_recent(self, limit: int = 20) -> list[HistorySyncRun]:
        return list(
            self.session.scalars(
                select(HistorySyncRun).order_by(HistorySyncRun.started_at.desc()).limit(limit)
            )
        )


class HistorySyncRunDatasetRepository:
    """run × dataset 执行详情（§20）。"""

    def __init__(self, session: Session):
        self.session = session

    def start(
        self,
        run_id: str,
        dataset: DatasetName | str,
        *,
        start_watermark: date | None,
        target_trade_date: date | None,
        start_cursor: str | None = None,
        started_at: datetime,
    ) -> HistorySyncRunDataset:
        row = HistorySyncRunDataset(
            run_id=run_id,
            dataset=_key(dataset),
            status=RunDatasetStatus.RUNNING.value,
            start_watermark=start_watermark,
            target_trade_date=target_trade_date,
            start_cursor=start_cursor,
            started_at=started_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, run_id: str, dataset: DatasetName | str) -> HistorySyncRunDataset | None:
        return self.session.get(HistorySyncRunDataset, (run_id, _key(dataset)))

    def add_counts(
        self,
        run_id: str,
        dataset: DatasetName | str,
        *,
        dates: int = 0,
        rows: int = 0,
        requests: int = 0,
        retries: int = 0,
        processed: int = 0,
        succeeded: int = 0,
        failed: int = 0,
        skipped: int = 0,
    ) -> None:
        """单日成功事务内的增量累计（§22 步骤 11）。

        v0.4.0 个股口径：``processed``/``succeeded``/``failed``/``skipped``
        为 ``processed_count``/``task_success_count``/``task_failed_count``/
        ``skipped_count`` 的增量（StockSyncExecutor 每股事务内累计）。
        """
        values: dict = {}
        if dates:
            values["dates_completed"] = HistorySyncRunDataset.dates_completed + dates
        if rows:
            values["rows_written"] = HistorySyncRunDataset.rows_written + rows
        if requests:
            values["request_count"] = HistorySyncRunDataset.request_count + requests
        if retries:
            values["retry_count"] = HistorySyncRunDataset.retry_count + retries
        if processed:
            values["processed_count"] = HistorySyncRunDataset.processed_count + processed
        if succeeded:
            values["task_success_count"] = (
                HistorySyncRunDataset.task_success_count + succeeded
            )
        if failed:
            values["task_failed_count"] = (
                HistorySyncRunDataset.task_failed_count + failed
            )
        if skipped:
            values["skipped_count"] = HistorySyncRunDataset.skipped_count + skipped
        if not values:
            return
        self.session.execute(
            update(HistorySyncRunDataset)
            .where(
                HistorySyncRunDataset.run_id == run_id,
                HistorySyncRunDataset.dataset == _key(dataset),
            )
            .values(**values)
        )

    def finish(
        self,
        run_id: str,
        dataset: DatasetName | str,
        *,
        status: RunDatasetStatus,
        finished_at: datetime,
        end_watermark: date | None = None,
        end_cursor: str | None = None,
        failed_trade_date: date | None = None,
        last_error_code: str | None = None,
        last_error: str | None = None,
    ) -> None:
        values: dict = {
            "status": _enum_value(status),
            "finished_at": finished_at,
        }
        if end_watermark is not None:
            values["end_watermark"] = end_watermark
        if end_cursor is not None:
            values["end_cursor"] = end_cursor
        if failed_trade_date is not None:
            values["failed_trade_date"] = failed_trade_date
        if last_error_code is not None:
            values["last_error_code"] = last_error_code
        if last_error is not None:
            values["last_error"] = last_error
        self.session.execute(
            update(HistorySyncRunDataset)
            .where(
                HistorySyncRunDataset.run_id == run_id,
                HistorySyncRunDataset.dataset == _key(dataset),
            )
            .values(**values)
        )

    def list_for_run(self, run_id: str) -> list[HistorySyncRunDataset]:
        return list(
            self.session.scalars(
                select(HistorySyncRunDataset).where(
                    HistorySyncRunDataset.run_id == run_id
                )
            )
        )


class StockSyncStateRepository:
    """个股水位与状态（per-stock-history-sync，design D1/D2）。

    逻辑唯一键 ``(dataset, instrument_id)`` 的唯一性由 WriteCoordinator 写锁
    内的 get-or-create 保证（表无 UNIQUE 约束，项目 DuckDB 惯例）；
    ``watermark_date`` 单调不下降由 ``advance_watermark`` 在提交事务内校验。
    """

    def __init__(self, session: Session):
        self.session = session

    def get(
        self, dataset: DatasetName | str, instrument_id: str
    ) -> StockSyncState | None:
        return self.session.get(StockSyncState, (_key(dataset), instrument_id))

    def get_or_create(
        self,
        dataset: DatasetName | str,
        instrument_id: str,
        *,
        ts_code: str | None,
    ) -> StockSyncState:
        """写锁内 get-or-create（唯一性保证的落点，幂等）。"""
        state = self.get(dataset, instrument_id)
        if state is None:
            now = _utcnow()
            state = StockSyncState(
                dataset=_key(dataset),
                instrument_id=instrument_id,
                ts_code=ts_code,
                created_at=now,
                updated_at=now,
            )
            self.session.add(state)
            self.session.flush()
        return state

    def bulk_ensure_missing(
        self,
        dataset: DatasetName | str,
        entries: Iterable[tuple[str, str | None]],
    ) -> int:
        """按 run 批量补建缺失状态行（design D7/D3：首轮 2.4 万行一次写事务）。

        ``entries`` 为 ``(instrument_id, ts_code)`` 对（Service 从主档快照构造）；
        已存在的行不动（水位/last_* 保持），仅补建缺失行。返回新建行数。
        """
        key = _key(dataset)
        existing = set(
            self.session.scalars(
                select(StockSyncState.instrument_id).where(
                    StockSyncState.dataset == key
                )
            )
        )
        now = _utcnow()
        created = 0
        pending: list[StockSyncState] = []
        for instrument_id, ts_code in entries:
            if instrument_id in existing:
                continue
            existing.add(instrument_id)
            pending.append(
                StockSyncState(
                    dataset=key,
                    instrument_id=instrument_id,
                    ts_code=ts_code,
                    created_at=now,
                    updated_at=now,
                )
            )
            created += 1
        if pending:
            self.session.add_all(pending)
            self.session.flush()
        return created

    def universe(self, dataset: DatasetName | str) -> list[StockSyncState]:
        """落后优先处理顺序（自动补偿 spec）：``watermark_date 升序
        （NULL 视为最旧）→ ts_code 升序``。"""
        stmt = (
            select(StockSyncState)
            .where(StockSyncState.dataset == _key(dataset))
            .order_by(
                nulls_first(StockSyncState.watermark_date.asc()),
                StockSyncState.ts_code.asc(),
            )
        )
        return list(self.session.scalars(stmt))

    def advance_watermark(
        self,
        dataset: DatasetName | str,
        instrument_id: str,
        new_watermark: date,
        *,
        ts_code: str | None,
        last_task_id: int | None,
        success_at: datetime,
    ) -> None:
        """成功提交事务内的水位推进（单调不下降校验 + last_* 全套刷新）。

        新水位早于既有水位即抛错——调用方事务整体回滚，水位保持原值
        （个股独立连续水位线 spec："水位绝不回退" Scenario）。
        """
        state = self.get_or_create(dataset, instrument_id, ts_code=ts_code)
        if (
            state.watermark_date is not None
            and new_watermark < state.watermark_date
        ):
            raise ValueError(
                f"stock_sync_state 水位回退被拒绝: dataset={_key(dataset)} "
                f"instrument_id={instrument_id} "
                f"{state.watermark_date} -> {new_watermark}"
            )
        state.watermark_date = new_watermark
        state.ts_code = ts_code  # 冗余展示列刷新为当前主档规范代码（OQ3 定稿）
        state.last_task_id = last_task_id
        state.last_status = TASK_STATUS_SUCCESS
        state.last_success_at = success_at
        state.last_attempt_at = success_at
        state.last_error_code = None  # 成功后清除失败快照（同 finish_success 语义）
        state.last_error = None
        state.updated_at = _utcnow()
        self.session.flush()

    def record_failure(
        self,
        dataset: DatasetName | str,
        instrument_id: str,
        *,
        ts_code: str | None,
        last_task_id: int | None,
        error_code: str | None,
        error: str | None,
        attempt_at: datetime,
    ) -> None:
        """失败记录（水位绝不动）：last_status=failed 与错误快照。"""
        state = self.get_or_create(dataset, instrument_id, ts_code=ts_code)
        state.ts_code = ts_code
        state.last_task_id = last_task_id
        state.last_status = TASK_STATUS_FAILED
        state.last_error_code = error_code
        state.last_error = error
        state.last_attempt_at = attempt_at
        state.updated_at = _utcnow()
        self.session.flush()


class SyncTaskRepository:
    """个股任务流水（per-stock-history-sync，design D2/D10）。

    每次某数据集某股票实际启动一次同步即新增一行，不覆盖历史
    （"任务流水不覆盖" Scenario）；``id`` 由 ``seq_sync_task_id`` 生成。
    """

    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        *,
        run_id: str,
        dataset: DatasetName | str,
        instrument_id: str,
        ts_code: str,
        start_date: date,
        end_date: date,
        started_at: datetime,
    ) -> SyncTask:
        task = SyncTask(
            run_id=run_id,
            dataset=_key(dataset),
            instrument_id=instrument_id,
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            status=TASK_STATUS_RUNNING,
            started_at=started_at,
            created_at=started_at,
        )
        self.session.add(task)
        self.session.flush()
        return task

    def find_by_id(self, task_id: int) -> SyncTask | None:
        return self.session.get(SyncTask, task_id)

    def finish_success(
        self,
        task_id: int,
        *,
        retry_count: int,
        attempt_count: int,
        records_fetched: int,
        records_written: int,
        finished_at: datetime,
        duration_ms: int | None,
    ) -> None:
        task = self.find_by_id(task_id)
        if task is None:
            raise ValueError(f"sync_task 不存在: {task_id}")
        task.status = TASK_STATUS_SUCCESS
        task.retry_count = retry_count
        task.attempt_count = attempt_count
        task.records_fetched = records_fetched
        task.records_written = records_written
        task.finished_at = finished_at
        task.duration_ms = duration_ms
        self.session.flush()

    def finish_failed(
        self,
        task_id: int,
        *,
        retry_count: int,
        attempt_count: int,
        records_fetched: int,
        error_code: str | None,
        error_type: str | None,
        error_message: str | None,
        finished_at: datetime,
        duration_ms: int | None,
    ) -> None:
        task = self.find_by_id(task_id)
        if task is None:
            raise ValueError(f"sync_task 不存在: {task_id}")
        task.status = TASK_STATUS_FAILED
        task.retry_count = retry_count
        task.attempt_count = attempt_count
        task.records_fetched = records_fetched
        task.error_code = error_code
        task.error_type = error_type
        task.error_message = error_message
        task.finished_at = finished_at
        task.duration_ms = duration_ms
        self.session.flush()

    def interrupt_running_for_runs(
        self, run_ids: list[str], *, finished_at: datetime
    ) -> int:
        """启动恢复（进程中断与恢复 spec）：把已中断 Run 的 running 任务批量
        置 interrupted（补 finished_at）。对应水位本就未提交、绝不动。
        返回标记行数。
        """
        if not run_ids:
            return 0
        # DuckDB 驱动 update 的 rowcount 返回 -1，先查后改拿到准确行数
        task_ids = list(
            self.session.scalars(
                select(SyncTask.id).where(
                    SyncTask.run_id.in_(run_ids),
                    SyncTask.status == TASK_STATUS_RUNNING,
                )
            )
        )
        if not task_ids:
            return 0
        self.session.execute(
            update(SyncTask)
            .where(SyncTask.id.in_(task_ids))
            .values(
                status=TASK_STATUS_INTERRUPTED,
                finished_at=finished_at,
            )
        )
        return len(task_ids)
