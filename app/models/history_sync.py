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

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
