"""历史数据管理 API Schema（a-share-historical-data，技术方案 §52、tasks 7.1）。

首个按域拆分的 Schema 模块（此前集中于 ``app/schemas/__init__.py``）：历史
数据管理的 summary / sync / runs 模型字段较多且仅本域使用，集中于此更清晰。

时间字段统一为北京时间带时区 ISO（与 ``app/api/status.py`` 的既有约定一致）。
"""

from __future__ import annotations

from pydantic import BaseModel


# ---- summary（§52.1） ----


class DailyDatasetSummary(BaseModel):
    """日级数据集状态（字段与 §52.1 一一对应）。"""

    dataset: str
    display_name: str
    status: str
    history_start_date: str | None = None
    data_min_date: str | None = None
    data_max_date: str | None = None
    latest_complete_trade_date: str | None = None
    latest_expected_trade_date: str | None = None
    next_trade_date: str | None = None
    lag_trade_days: int = 0
    record_count: int = 0
    current_trade_date: str | None = None
    current_attempt: int = 0
    last_success_at: str | None = None
    last_error_code: str | None = None
    last_error: str | None = None


class MasterDatasetSummary(BaseModel):
    """主档数据集状态（§54.3）：主档无交易日水位语义，不伪造水位字段。"""

    dataset: str
    display_name: str
    status: str
    record_count: int = 0
    last_success_at: str | None = None
    master_cursor: str | None = None
    bootstrap_complete: bool = False
    last_error_code: str | None = None
    last_error: str | None = None


class ActiveRunSummary(BaseModel):
    """当前运行中任务的简要信息（无 active run 时 summary.active_run 为 null）。"""

    run_id: str
    trigger_type: str
    status: str
    started_at: str


class HistorySummaryResponse(BaseModel):
    overall_status: str
    history_start_date: str
    latest_market_trade_date: str | None = None
    active_run: ActiveRunSummary | None = None
    daily_datasets: list[DailyDatasetSummary] = []
    master_datasets: list[MasterDatasetSummary] = []


# ---- 手动同步（§52.2） ----


class SyncStartResponse(BaseModel):
    """202：已启动（HTTP 不等待回填完成）。"""

    run_id: str
    status: str = "RUNNING"


class SyncConflictResponse(BaseModel):
    """409：已有任务在运行，附当前 run_id 与说明。"""

    run_id: str | None = None
    status: str = "RUNNING"
    message: str = "历史数据同步正在运行"


# ---- 执行记录（§52.3/§52.4） ----


class RunDatasetDetail(BaseModel):
    dataset: str
    display_name: str
    status: str
    start_watermark: str | None = None
    target_trade_date: str | None = None
    end_watermark: str | None = None
    start_cursor: str | None = None
    end_cursor: str | None = None
    dates_completed: int = 0
    rows_written: int = 0
    request_count: int = 0
    retry_count: int = 0
    failed_trade_date: str | None = None
    last_error_code: str | None = None
    last_error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class RunItem(BaseModel):
    run_id: str
    trigger_type: str
    status: str
    requested_by_user_id: str | None = None
    started_at: str
    finished_at: str | None = None
    duration_ms: int | None = None
    error_summary: str | None = None


class RunListResponse(BaseModel):
    items: list[RunItem] = []


class RunDetailResponse(BaseModel):
    """run 详情：供页面轮询当前进度（run + 每个 dataset 的执行详情）。"""

    run: RunItem
    datasets: list[RunDatasetDetail] = []
