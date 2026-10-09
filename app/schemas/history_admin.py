"""历史数据管理 API Schema（a-share-historical-data，技术方案 §52、tasks 8.1~8.3）。

按域拆分的 Schema 模块：历史数据管理的 summary / sync / runs / stocks / tasks
模型字段较多且仅本域使用，集中于此更清晰。

时间字段统一为北京时间带时区 ISO（与 ``app/api/status.py`` 的既有约定一致）。

v0.4.0（per-stock-history-sync）升级：
- ``DailyDatasetSummary`` 新增个股口径字段（stock_count / up_to_date_count /
  lagging_count / today_success_count / today_failed_count / completion_rate）；
  旧水位字段保留输出（历史 run_dataset 行兼容，冻结为 NULL/0）。
- ``RunDatasetDetail`` 新增 processed_count / task_success_count /
  task_failed_count / skipped_count 四列（个股模式统计口径）。
- 新增 ``StockListItem`` / ``StockListResponse``（个股列表，§8.2）。
- 新增 ``TaskDetailResponse``（任务详情，§8.3）。
- 新增 ``StockSyncProgressSchema``（运行中实时进度，design D10）。
"""

from __future__ import annotations

from pydantic import BaseModel


# ---- summary（§52.1 / D12） ----


class DailyDatasetSummary(BaseModel):
    """日级数据集状态（v0.4.0 个股口径，design D11/D12）。

    个股口径字段：
        stock_count: 数据集范围内的股票总数（universe 大小）
        up_to_date_count: 水位 >= target 的股票数（已追平）
        lagging_count: 水位 < target 或无水位的股票数（有缺口）
        today_success_count: 今日（Asia/Shanghai）最后一次尝试成功的股票数
        today_failed_count: 今日最后一次尝试失败的股票数
        completion_rate: 完整度（0~1，up_to_date_count / stock_count）

    旧口径字段保留输出（兼容历史与冻结展示）：
        latest_complete_trade_date / lag_trade_days / current_trade_date /
        current_attempt 等，新写入下为冻结值。
    """

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
    # —— 个股口径（v0.4.0 新增） ——
    stock_count: int = 0
    up_to_date_count: int = 0
    lagging_count: int = 0
    today_success_count: int = 0
    today_failed_count: int = 0
    completion_rate: float = 0.0


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


class EtfUniverseSummary(BaseModel):
    """ETF universe 概况（etf-data-module，design D12）。

    active_count / total_count 来自 instrument 表（market='CN' AND
    asset_type='ETF'，含 inactive）；last_refreshed_at 为 etf_basic 数据集
    最近一次 universe 刷新成功时间。
    """

    active_count: int = 0
    total_count: int = 0
    last_refreshed_at: str | None = None


class EtfDatasetSummary(BaseModel):
    """ETF 数据集条目（etf-data-module，design D12）。

    etf_basic 按主档条目结构（MasterDatasetSummary 同构）；
    etf_daily / etf_adj_factor 按日级条目结构（DailyDatasetSummary 同构，
    stock_count 为 ETF 证券总数——与股票条目共用字段名保持同构）。
    同构字段平铺到本模型，避免前端处理多态。
    """

    # 主档条目字段（etf_basic 用）
    dataset: str
    display_name: str
    status: str
    record_count: int = 0
    last_success_at: str | None = None
    master_cursor: str | None = None
    bootstrap_complete: bool = False
    last_error_code: str | None = None
    last_error: str | None = None
    # 日级条目字段（etf_daily / etf_adj_factor 用；etf_basic 恒为默认值）
    history_start_date: str | None = None
    data_min_date: str | None = None
    data_max_date: str | None = None
    latest_complete_trade_date: str | None = None
    latest_expected_trade_date: str | None = None
    next_trade_date: str | None = None
    lag_trade_days: int = 0
    current_trade_date: str | None = None
    current_attempt: int = 0
    stock_count: int = 0
    up_to_date_count: int = 0
    lagging_count: int = 0
    today_success_count: int = 0
    today_failed_count: int = 0
    completion_rate: float = 0.0


class HistorySummaryResponse(BaseModel):
    """summary 响应（etf-data-module 新增 ETF 分组，design D12）。

    etf_universe / etf_datasets 在 history.etf_enabled=false 时为
    None / 空列表（不返回统计）。
    """

    overall_status: str
    history_start_date: str
    latest_market_trade_date: str | None = None
    active_run: ActiveRunSummary | None = None
    daily_datasets: list[DailyDatasetSummary] = []
    master_datasets: list[MasterDatasetSummary] = []
    etf_universe: EtfUniverseSummary | None = None
    etf_datasets: list[EtfDatasetSummary] = []


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


# ---- 执行记录（§52.3 / §52.4，v0.4.0 新增个股统计列） ----


class RunDatasetDetail(BaseModel):
    dataset: str
    display_name: str
    status: str
    # 旧水位列（冻结兼容，新 run_dataset 行为 NULL/0）
    start_watermark: str | None = None
    target_trade_date: str | None = None
    end_watermark: str | None = None
    start_cursor: str | None = None
    end_cursor: str | None = None
    dates_completed: int = 0
    failed_trade_date: str | None = None
    last_error_code: str | None = None
    last_error: str | None = None
    # 累计口径（个股模式下继续累计）
    rows_written: int = 0
    request_count: int = 0
    retry_count: int = 0
    # —— 个股口径统计（v0.4.0 新增） ——
    processed_count: int = 0
    task_success_count: int = 0
    task_failed_count: int = 0
    skipped_count: int = 0
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


class StockSyncProgressSchema(BaseModel):
    """运行中实时进度快照（design D10，API 只读展示）。"""

    current_dataset: str | None = None
    current_ts_code: str | None = None
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0


class RunDetailResponse(BaseModel):
    """run 详情：供页面轮询当前进度（run + 每个 dataset 的执行详情 + 实时 progress）。"""

    run: RunItem
    datasets: list[RunDatasetDetail] = []
    progress: StockSyncProgressSchema | None = None


# ---- 个股列表（§8.2 / D12） ----


class StockListItem(BaseModel):
    """个股列表行（cn_stock_basic LEFT JOIN stock_sync_state）。"""

    ts_code: str
    name: str | None = None
    list_date: str | None = None
    delist_date: str | None = None
    watermark_date: str | None = None
    last_status: str | None = None  # success / failed / None（从未尝试）
    last_error_code: str | None = None
    last_error: str | None = None
    last_success_at: str | None = None
    last_attempt_at: str | None = None


class StockStats(BaseModel):
    """个股列表页的统计块（复用 summary 口径）。"""

    stock_count: int = 0
    up_to_date_count: int = 0
    lagging_count: int = 0
    today_success_count: int = 0
    today_failed_count: int = 0
    completion_rate: float = 0.0


class StockPagination(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int


class StockListResponse(BaseModel):
    items: list[StockListItem] = []
    stats: StockStats
    pagination: StockPagination


# ---- 任务详情（§8.3 / D12） ----


class TaskDetailResponse(BaseModel):
    """sync_task 详情（JOIN 主档补 stock_name），只读。"""

    id: int
    run_id: str
    dataset: str
    instrument_id: str
    ts_code: str
    stock_name: str | None = None
    start_date: str
    end_date: str
    status: str
    retry_count: int
    attempt_count: int
    records_fetched: int
    records_written: int
    error_code: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    started_at: str
    finished_at: str | None = None
    duration_ms: int | None = None
