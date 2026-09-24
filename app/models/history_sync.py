"""历史同步控制模型与常量（a-share-historical-data，技术方案 §17~§20、§51）。

四张小表：
- ``history_sync_state``：每数据集一行的水位与状态（dataset 双类：日级连续 + 主档）；
- ``history_day_status``：日级数据集的"连续完成证明"账本，仅记 COMPLETE；
- ``history_sync_run``：每次统一同步任务一条；
- ``history_sync_run_dataset``：每 run × dataset 的执行详情。

dataset 名称固定为代码常量（§17.1），不散落任意字符串。
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Integer,
    Sequence,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DatasetName(str, Enum):
    """数据集名称（§17.1，第一阶段固定八个）。"""

    STOCK_BASIC = "stock_basic"
    TRADE_CAL = "trade_cal"
    NAMECHANGE = "namechange"
    STOCK_COMPANY = "stock_company"
    DAILY = "daily"
    ADJ_FACTOR = "adj_factor"
    DAILY_BASIC = "daily_basic"
    MONEYFLOW = "moneyflow"


class DatasetKind(str, Enum):
    """数据集类别（§17.1）。"""

    DAILY_CONTIGUOUS = "DAILY_CONTIGUOUS"
    MASTER = "MASTER"


class DatasetStatus(str, Enum):
    """数据集状态（§51.1）。"""

    UNINITIALIZED = "UNINITIALIZED"
    CHECKING = "CHECKING"
    SYNCING = "SYNCING"
    RETRYING = "RETRYING"
    CAUGHT_UP = "CAUGHT_UP"
    LAGGING = "LAGGING"
    FAILED = "FAILED"
    WAITING_SOURCE = "WAITING_SOURCE"


class TriggerType(str, Enum):
    """同步触发方式（§19）。"""

    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"
    STARTUP = "STARTUP"


class RunStatus(str, Enum):
    """同步任务整体状态（§19）。"""

    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    NOOP = "NOOP"


class RunDatasetStatus(str, Enum):
    """run × dataset 执行状态（§20）。"""

    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    NOOP = "NOOP"


DAY_STATUS_COMPLETE = "COMPLETE"
"""history_day_status 第一阶段仅持久化 COMPLETE（§18）；失败详情在 state/run_dataset。"""


class HistorySyncState(Base):
    """每数据集一行的同步水位与状态（技术方案 §17.1）。"""

    __tablename__ = "history_sync_state"

    dataset: Mapped[str] = mapped_column(String(32), primary_key=True)
    dataset_kind: Mapped[str] = mapped_column(String(32), nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False)

    history_start_date: Mapped[date | None] = mapped_column(Date)

    latest_complete_trade_date: Mapped[date | None] = mapped_column(Date)
    latest_expected_trade_date: Mapped[date | None] = mapped_column(Date)
    current_trade_date: Mapped[date | None] = mapped_column(Date)
    current_attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    master_cursor: Mapped[str | None] = mapped_column(String(64))
    bootstrap_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    record_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    data_min_date: Mapped[date | None] = mapped_column(Date)
    data_max_date: Mapped[date | None] = mapped_column(Date)

    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HistoryDayStatus(Base):
    """日级数据集的连续完成账本（技术方案 §18），仅记 COMPLETE。"""

    __tablename__ = "history_day_status"

    dataset: Mapped[str] = mapped_column(String(32), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_by_run_id: Mapped[str] = mapped_column(String(64), nullable=False)


class HistorySyncRun(Base):
    """每次统一同步任务（技术方案 §19）。"""

    __tablename__ = "history_sync_run"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by_user_id: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(String(16), nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HistorySyncRunDataset(Base):
    """每 run × dataset 的执行详情（技术方案 §20），管理员"最近执行记录"数据源。"""

    __tablename__ = "history_sync_run_dataset"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(32), primary_key=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False)

    start_watermark: Mapped[date | None] = mapped_column(Date)
    target_trade_date: Mapped[date | None] = mapped_column(Date)
    end_watermark: Mapped[date | None] = mapped_column(Date)

    start_cursor: Mapped[str | None] = mapped_column(String(64))
    end_cursor: Mapped[str | None] = mapped_column(String(64))

    dates_completed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    rows_written: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    failed_trade_date: Mapped[date | None] = mapped_column(Date)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)

    # —— 个股同步模式统计列（v0.4.0，processed/success/failed/skipped）
    processed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    task_success_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    task_failed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    skipped_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# —— 任务状态常量（sync_task.status 与 stock_sync_state.last_status 共用子集） ——
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_INTERRUPTED = "interrupted"


class StockSyncState(Base):
    """个股水位与状态（per-stock-history-sync，design D1/D2）。

    逻辑唯一键 ``(dataset, instrument_id)``，不设数据库 UNIQUE 约束
    （项目全库惯例，唯一性由写锁内 get-or-create 保证）；
    无 FK、无二级索引。初始水位统一为 NULL，首轮 run 按 universe 批量补建。
    """

    __tablename__ = "stock_sync_state"

    dataset: Mapped[str] = mapped_column(String(32), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # 冗余展示列：成功同步时刷新为当前主档规范代码
    ts_code: Mapped[str | None] = mapped_column(String(16))

    watermark_date: Mapped[date | None] = mapped_column(Date)
    last_task_id: Mapped[int | None] = mapped_column(BigInteger)
    last_status: Mapped[str | None] = mapped_column(String(16))  # success / failed

    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)

    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SyncTask(Base):
    """单股单数据集同步任务流水（per-stock-history-sync，design D2）。

    每次启动一只股票的同步创建一行，包含首次执行与全部重试的汇总；
    ``attempt_count`` 为总尝试次数（1 + retry_count），终态为 success/failed/interrupted。
    无 UNIQUE 约束、无 FK、无二级索引；id 由显式 sequence ``seq_sync_task_id`` 生成。
    """

    __tablename__ = "sync_task"

    id: Mapped[int] = mapped_column(
        BigInteger, Sequence("seq_sync_task_id"), primary_key=True
    )

    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    records_fetched: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    records_written: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

    error_code: Mapped[str | None] = mapped_column(String(64))
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
