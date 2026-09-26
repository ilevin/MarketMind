"""历史数据管理 API（per-stock-history-sync，design D11/D12、tasks 8.1~8.3）。

- Router 层统一 ``require_admin``：未登录 401、普通用户 403；POST 走既有
  CSRF 中间件（不新增绕过 CSRF 的写接口）。
- summary / runs / stocks / tasks 只读同步小表与主档，SHALL NOT 扫描事实
  大表，也 SHALL NOT 触发网络请求（D21）。
- 手动同步：HTTP 不等待回填完成，202 立即返回；已有任务运行时 409 附当前
  run_id。``requested_by_user_id`` 由服务端从当前认证用户取得，绝不接受
  客户端传值。

v0.4.0 升级（个股口径）：
- summary.daily_datasets[] 新增 stock_count / up_to_date_count /
  lagging_count / today_success_count / today_failed_count / completion_rate；
  overall_status 按 RUNNING → ERROR（系统级）→ LAGGING（个股缺口）→ HEALTHY。
- 新增 GET /stocks（dataset 必填，仅日级数据集 422；分页/筛选/搜索）。
- 新增 GET /tasks/{task_id}（按 id 直查 + JOIN 主档补名称）。
- /runs 与 /runs/{run_id} 响应补充个股统计列与运行中 progress 快照。
- 顺手修复 requested_by_user_id int→str 类型瑕疵。
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, func, or_, select, text

from app.auth.dependencies import require_admin
from app.auth.session import CurrentUser
from app.config import BUSINESS_TZ_NAME, AppConfig
from app.models.history_market import CnStockBasic
from app.models.history_sync import (
    DatasetName,
    DatasetStatus,
    RunStatus,
    StockSyncState,
    SyncTask,
)
from app.repositories.history_sync import (
    HistorySyncRunDatasetRepository,
    HistorySyncRunRepository,
    HistorySyncStateRepository,
    StockSyncStateRepository,
    SyncTaskRepository,
)
from app.repositories.trading_calendar import TradingCalendarRepository
from app.schemas.history_admin import (
    ActiveRunSummary,
    DailyDatasetSummary,
    HistorySummaryResponse,
    MasterDatasetSummary,
    RunDatasetDetail,
    RunDetailResponse,
    RunItem,
    RunListResponse,
    StockListItem,
    StockListResponse,
    StockPagination,
    StockStats,
    StockSyncProgressSchema,
    SyncConflictResponse,
    SyncStartResponse,
    TaskDetailResponse,
)
from app.services.history.availability import AvailabilityPolicy
from app.services.history.sync_service import DAY_LEVEL_DATASETS
from app.services.market_session_service import now_beijing

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/admin/history-data",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

_BEIJING = ZoneInfo(BUSINESS_TZ_NAME)
CN_MARKET = "CN"
CALENDAR_SOURCE = "tushare"

# 后台手动同步任务的强引用：create_task 只保留弱引用，不留引用可能被 GC 中断
_BACKGROUND_TASKS: set[asyncio.Task] = set()

DISPLAY_NAMES: dict[str, str] = {
    DatasetName.DAILY.value: "日线行情",
    DatasetName.ADJ_FACTOR.value: "复权因子",
    DatasetName.DAILY_BASIC.value: "每日指标",
    DatasetName.MONEYFLOW.value: "资金流",
    DatasetName.STOCK_BASIC.value: "股票基础信息",
    DatasetName.TRADE_CAL.value: "交易日历",
    DatasetName.NAMECHANGE.value: "证券改名记录",
    DatasetName.STOCK_COMPANY.value: "公司基本信息",
}

MASTER_DATASETS: tuple[DatasetName, ...] = (
    DatasetName.STOCK_BASIC,
    DatasetName.TRADE_CAL,
    DatasetName.NAMECHANGE,
    DatasetName.STOCK_COMPANY,
)

# 硬前置主档：失败应升级整体异常级别（§84）
CORE_MASTER_DATASETS = (DatasetName.TRADE_CAL.value, DatasetName.STOCK_BASIC.value)

# 缓存严格日历超过该天数未覆盖到近期 → 判定过期（远超 A 股最长连续休市）
CALENDAR_STALE_DAYS = 30

OVERALL_RUNNING = "RUNNING"
OVERALL_ERROR = "ERROR"
OVERALL_LAGGING = "LAGGING"
OVERALL_WAITING = "WAITING"
OVERALL_HEALTHY = "HEALTHY"
OVERALL_UNINITIALIZED = "UNINITIALIZED"

# /stocks 分页固定页大小（design D12）
STOCKS_PAGE_SIZE = 100
STOCK_STATUS_OPTIONS = {"all", "success", "failed"}

# 日级数据集值集合（用于 /stocks 的 422 校验）
_DAY_LEVEL_DATASET_VALUES = {ds.value for ds in DAY_LEVEL_DATASETS}


def _iso(dt: datetime | None) -> str | None:
    """北京时间带时区 ISO（与 app/api/status.py 既有约定一致）。

    同步表时间戳来源混合（now_beijing 的 aware Beijing 与仓储 _utcnow 的
    aware UTC），统一换算为北京时间；naive 按北京时间解释。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_BEIJING)
    return dt.astimezone(_BEIJING).isoformat()


def _ds(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _duration_ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    if start.tzinfo is None and end.tzinfo is not None:
        start = start.replace(tzinfo=end.tzinfo)
    elif end.tzinfo is None and start.tzinfo is not None:
        end = end.replace(tzinfo=start.tzinfo)
    return int((end - start).total_seconds() * 1000)


def _cached_open_days(request: Request) -> list[date]:
    """已缓存的严格交易日历（source='tushare'）升序 open day 列表。

    只读小表、不发网络请求：严格日历首次拉取由同步任务负责，本接口只消费
    已落库结果；缺失年份自然表现为目标日期退回 state 表已记录值。
    """
    config: AppConfig = request.app.state.config
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        rows = TradingCalendarRepository(session).get_days_between(
            CN_MARKET, config.history.start_date, now_beijing().date()
        )
    return [row.trade_date for row in rows if row.is_open and row.source == CALENDAR_SOURCE]


def _latest_open_day(open_days: list[date]) -> date | None:
    """最新市场交易日（已缓存的最近严格交易日）。"""
    today = now_beijing().date()
    candidates = [day for day in open_days if day <= today]
    return candidates[-1] if candidates else None


def _calendar_stale(latest_open_day: date | None) -> bool:
    """已缓存严格日历是否已过期（无法据此判断是否落后）。

    A 股最长连续休市（春节）不超过约两周，故"最近交易日早于 30 天前"只可能
    是日历未再刷新（长期停机 / 同步持续失败），不可能是正常休市。此判定的
    意义：日历过期时 lag_trade_days 恒为 0，若照常报 HEALTHY 会让管理员
    在数据实际落后数月时看到"一切正常"（§84 的 LAGGING 分支失去前提）。
    """
    if latest_open_day is None:
        return True
    return (now_beijing().date() - latest_open_day).days > CALENDAR_STALE_DAYS


def _next_trade_date(
    watermark: date | None, *, open_days: list[date], target: date | None
) -> date | None:
    """下一个待处理交易日：水位之后、目标以内的第一个交易日。"""
    if target is None or not open_days:
        return None
    for day in open_days:
        if day > target:
            break
        if watermark is None or day > watermark:
            return day
    return None


def _dataset_target(
    dataset: DatasetName, *, policy: AvailabilityPolicy, open_days: list[date]
) -> date | None:
    """数据集目标日（AvailabilityPolicy 计算，失败退回 None）。"""
    return policy.latest_expected_trade_date(
        dataset, now=now_beijing(), strict_open_days=open_days
    )


# ---- 个股统计聚合（summary 与 /stocks 共用，SQL 内一次 GROUP BY） ----


def _aggregate_stock_stats(
    session, dataset: str, target: date | None
) -> dict:
    """对 stock_sync_state 按 (dataset=?) 聚合个股口径统计。

    返回 dict：stock_count / up_to_date_count / lagging_count /
    today_success_count / today_failed_count / completion_rate。

    一次 SQL 完成全部聚合（2.4 万行小表，DuckDB 毫秒级）。
    """
    # up_to_date: watermark_date IS NOT NULL AND watermark_date >= target
    # lagging: 其余（无水位 或 水位 < target）
    # today_*: last_attempt_at 的上海日期 = 今天 且 last_status = ?
    sql = text(
        """
        SELECT
            COUNT(*) AS stock_count,
            SUM(CASE WHEN watermark_date IS NOT NULL AND watermark_date >= :target
                     THEN 1 ELSE 0 END) AS up_to_date_count,
            SUM(CASE WHEN watermark_date IS NULL OR watermark_date < :target
                     THEN 1 ELSE 0 END) AS lagging_count,
            SUM(CASE WHEN CAST(last_attempt_at AS DATE)
                          = CAST(now() AT TIME ZONE 'Asia/Shanghai' AS DATE)
                     AND last_status = 'success' THEN 1 ELSE 0 END) AS today_success_count,
            SUM(CASE WHEN CAST(last_attempt_at AS DATE)
                          = CAST(now() AT TIME ZONE 'Asia/Shanghai' AS DATE)
                     AND last_status = 'failed' THEN 1 ELSE 0 END) AS today_failed_count
        FROM stock_sync_state
        WHERE dataset = :dataset
        """
    )
    row = session.execute(sql, {"dataset": dataset, "target": target}).fetchone()
    stock_count = int(row.stock_count or 0)
    up_to_date_count = int(row.up_to_date_count or 0)
    lagging_count = int(row.lagging_count or 0)
    today_success = int(row.today_success_count or 0)
    today_failed = int(row.today_failed_count or 0)
    completion_rate = (up_to_date_count / stock_count) if stock_count > 0 else 0.0
    return {
        "stock_count": stock_count,
        "up_to_date_count": up_to_date_count,
        "lagging_count": lagging_count,
        "today_success_count": today_success,
        "today_failed_count": today_failed,
        "completion_rate": round(completion_rate, 6),
    }


# ---- summary（8.1） ----


def _daily_summary(
    state,
    *,
    open_days: list[date],
    policy: AvailabilityPolicy,
    stock_stats: dict | None,
) -> DailyDatasetSummary:
    dataset = DatasetName(state.dataset)
    target = _dataset_target(dataset, policy=policy, open_days=open_days)
    stats = stock_stats or {}
    return DailyDatasetSummary(
        dataset=state.dataset,
        display_name=DISPLAY_NAMES.get(state.dataset, state.dataset),
        status=state.status,
        history_start_date=_ds(state.history_start_date),
        data_min_date=_ds(state.data_min_date),
        data_max_date=_ds(state.data_max_date),
        latest_complete_trade_date=_ds(state.latest_complete_trade_date),
        latest_expected_trade_date=_ds(target),
        next_trade_date=_ds(
            _next_trade_date(
                state.latest_complete_trade_date, open_days=open_days, target=target
            )
        ),
        # lag_trade_days 保留（旧字段兼容），个股模式下为数据集级旧水位列的展示
        lag_trade_days=0,  # 个股模式下此字段无实际语义，冻结为 0
        record_count=state.record_count or 0,
        current_trade_date=_ds(state.current_trade_date),
        current_attempt=state.current_attempt or 0,
        last_success_at=_iso(state.last_success_at),
        last_error_code=state.last_error_code,
        last_error=state.last_error,
        # —— 个股口径 ——
        stock_count=stats.get("stock_count", 0),
        up_to_date_count=stats.get("up_to_date_count", 0),
        lagging_count=stats.get("lagging_count", 0),
        today_success_count=stats.get("today_success_count", 0),
        today_failed_count=stats.get("today_failed_count", 0),
        completion_rate=float(stats.get("completion_rate", 0.0)),
    )


def _master_summary(state) -> MasterDatasetSummary:
    """主档摘要：主档无交易日水位语义，SHALL NOT 伪造水位字段（§54.3）。"""
    return MasterDatasetSummary(
        dataset=state.dataset,
        display_name=DISPLAY_NAMES.get(state.dataset, state.dataset),
        status=state.status,
        record_count=state.record_count or 0,
        last_success_at=_iso(state.last_success_at),
        master_cursor=state.master_cursor,
        bootstrap_complete=bool(state.bootstrap_complete),
        last_error_code=state.last_error_code,
        last_error=state.last_error,
    )


def _overall_status(
    daily: list[DailyDatasetSummary], master: list[MasterDatasetSummary]
) -> str:
    """整体状态（D12）：RUNNING > ERROR > LAGGING > WAITING > HEALTHY。

    v0.4.0 个股口径变化：
    - 系统级 FAILED（数据集级 status=FAILED）才升级 ERROR；
    - 个股失败（today_failed_count > 0 但数据集非 FAILED）不升级 ERROR，
      而是通过 lagging_count 体现为 LAGGING；
    - 无 FAILED 且 lagging_count > 0 → LAGGING；
    - 全部 up_to_date → HEALTHY。
    """
    statuses = {item.dataset: item.status for item in daily}
    # 硬前置失败升级整体异常（日历/主档失败时日级数据集根本无法推进）
    for item in master:
        if item.dataset in CORE_MASTER_DATASETS and item.status == DatasetStatus.FAILED.value:
            return OVERALL_ERROR
    # 完全未初始化
    if not [s for s in statuses.values() if s != DatasetStatus.UNINITIALIZED.value]:
        return OVERALL_UNINITIALIZED
    # 系统级失败：数据集状态为 FAILED（整数据集阻塞）
    if DatasetStatus.FAILED.value in statuses.values():
        return OVERALL_ERROR

    # 个股缺口：有 lagging 股票（无失败但有落后）
    any_lagging = any(item.lagging_count > 0 for item in daily if item.stock_count > 0)
    if any_lagging:
        # 全部在等数据源发布 → WAITING（而非 LAGGING）
        waiting_all = all(
            item.status == DatasetStatus.WAITING_SOURCE.value
            for item in daily
            if item.stock_count > 0 and item.lagging_count > 0
        )
        # 个股模式下 WAITING_SOURCE 状态基本不产生（target 恒为已发布日），
        # 但保留兼容逻辑
        if waiting_all:
            return OVERALL_WAITING
        return OVERALL_LAGGING

    # 无落后：等发布 → WAITING；推进中/已追平 → HEALTHY
    if DatasetStatus.WAITING_SOURCE.value in statuses.values():
        return OVERALL_WAITING
    healthy = {
        DatasetStatus.CAUGHT_UP.value,
        DatasetStatus.SYNCING.value,
        DatasetStatus.RETRYING.value,
        DatasetStatus.CHECKING.value,
    }
    return OVERALL_HEALTHY if all(s in healthy for s in statuses.values()) else OVERALL_LAGGING


# ---- runs ----


def _run_item(run) -> RunItem:
    return RunItem(
        run_id=run.run_id,
        trigger_type=run.trigger_type,
        status=run.status,
        requested_by_user_id=run.requested_by_user_id,
        started_at=_iso(run.started_at) or "",
        finished_at=_iso(run.finished_at),
        duration_ms=_duration_ms(run.started_at, run.finished_at),
        error_summary=run.error_summary,
    )


def _dataset_detail(row) -> RunDatasetDetail:
    return RunDatasetDetail(
        dataset=row.dataset,
        display_name=DISPLAY_NAMES.get(row.dataset, row.dataset),
        status=row.status,
        # 旧水位列（冻结兼容）
        start_watermark=_ds(row.start_watermark),
        target_trade_date=_ds(row.target_trade_date),
        end_watermark=_ds(row.end_watermark),
        start_cursor=row.start_cursor,
        end_cursor=row.end_cursor,
        dates_completed=row.dates_completed or 0,
        failed_trade_date=_ds(row.failed_trade_date),
        last_error_code=row.last_error_code,
        last_error=row.last_error,
        # 累计口径
        rows_written=row.rows_written or 0,
        request_count=row.request_count or 0,
        retry_count=row.retry_count or 0,
        # —— 个股口径统计（v0.4.0 新增） ——
        processed_count=row.processed_count or 0,
        task_success_count=row.task_success_count or 0,
        task_failed_count=row.task_failed_count or 0,
        skipped_count=row.skipped_count or 0,
        started_at=_iso(row.started_at),
        finished_at=_iso(row.finished_at),
    )


def _current_progress(request: Request) -> StockSyncProgressSchema | None:
    """从 app.state 读取运行中实时进度快照（design D10）。

    仅当 history_sync_service 存在且当前有活动数据集时返回；
    run 结束后 current_dataset=None，返回 None。
    """
    service = getattr(request.app.state, "history_sync_service", None)
    if service is None:
        return None
    progress = getattr(service, "progress", None)
    if progress is None or progress.current_dataset is None:
        return None
    return StockSyncProgressSchema(
        current_dataset=progress.current_dataset,
        current_ts_code=progress.current_ts_code,
        processed=progress.processed,
        succeeded=progress.succeeded,
        failed=progress.failed,
        skipped=progress.skipped,
    )


@router.get("/summary", response_model=HistorySummaryResponse)
def get_summary(request: Request):
    """历史数据总览：只读同步小表与缓存日历，不扫描事实大表（D21/§52.1）。"""
    config: AppConfig = request.app.state.config
    session_factory = request.app.state.session_factory
    policy = AvailabilityPolicy(config)

    open_days = _cached_open_days(request)
    with session_factory() as session:
        states = {s.dataset: s for s in HistorySyncStateRepository(session).all_states()}
        active = HistorySyncRunRepository(session).find_stale_running()

        # 个股口径统计：对每个日级数据集一次 SQL 聚合
        stock_stats_by_dataset: dict[str, dict] = {}
        for dataset in DAY_LEVEL_DATASETS:
            key = dataset.value
            if key in states:
                target = _dataset_target(dataset, policy=policy, open_days=open_days)
                stock_stats_by_dataset[key] = _aggregate_stock_stats(
                    session, key, target
                )

    daily = []
    for dataset in DAY_LEVEL_DATASETS:
        key = dataset.value
        if key in states:
            daily.append(
                _daily_summary(
                    states[key],
                    open_days=open_days,
                    policy=policy,
                    stock_stats=stock_stats_by_dataset.get(key),
                )
            )
        else:
            daily.append(
                DailyDatasetSummary(
                    dataset=key,
                    display_name=DISPLAY_NAMES[key],
                    status=DatasetStatus.UNINITIALIZED.value,
                    history_start_date=config.history.start_date.isoformat(),
                )
            )

    master = [
        _master_summary(states[dataset.value])
        if dataset.value in states
        else MasterDatasetSummary(
            dataset=dataset.value,
            display_name=DISPLAY_NAMES[dataset.value],
            status=DatasetStatus.UNINITIALIZED.value,
        )
        for dataset in MASTER_DATASETS
    ]

    active_run = active[0] if active else None
    latest_open_day = _latest_open_day(open_days)
    if active_run is not None:
        overall = OVERALL_RUNNING
    elif _calendar_stale(latest_open_day):
        # 日历过期时不能根据 lag 断言"已追平"（§84）
        overall = (
            OVERALL_UNINITIALIZED
            if _overall_status(daily, master) in (OVERALL_UNINITIALIZED, OVERALL_ERROR)
            else OVERALL_LAGGING
        )
    else:
        overall = _overall_status(daily, master)

    return HistorySummaryResponse(
        overall_status=overall,
        history_start_date=config.history.start_date.isoformat(),
        latest_market_trade_date=_ds(latest_open_day),
        active_run=(
            ActiveRunSummary(
                run_id=active_run.run_id,
                trigger_type=active_run.trigger_type,
                status=active_run.status,
                started_at=_iso(active_run.started_at) or "",
            )
            if active_run is not None
            else None
        ),
        daily_datasets=daily,
        master_datasets=master,
    )


@router.post(
    "/sync",
    status_code=202,
    response_model=SyncStartResponse,
    responses={409: {"model": SyncConflictResponse}},
)
async def start_sync(request: Request, current_user: CurrentUser = Depends(require_admin)):
    """启动一次手动同步：202 立即返回，不等待回填完成（§52.2）。"""
    job = getattr(request.app.state, "history_sync_job", None)
    if job is None:
        raise HTTPException(status_code=503, detail="历史数据同步未启用")

    # run_id 由服务端预留：HTTP 不等回填完成，否则无法在 202 前得知 run_id
    run_id = str(uuid.uuid4())
    if not job.try_begin(run_id=run_id):
        return JSONResponse(
            status_code=409,
            content=SyncConflictResponse(run_id=job.running_run_id()).model_dump(),
        )

    # requested_by_user_id 服务端取值：绝不接受客户端传入（spec 手动同步 API）
    # 顺手修复 int→str 类型瑕疵：CurrentUser.user_id 是 int，数据库列是 String(64)
    user_id_str = str(current_user.user_id)
    task = asyncio.create_task(
        asyncio.to_thread(job.run_manual, user_id_str, run_id=run_id),
        name="history-sync-manual",
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    logger.info(
        "管理员触发历史数据同步 user_id=%s run_id=%s", user_id_str, run_id
    )
    return SyncStartResponse(run_id=run_id, status=RunStatus.RUNNING.value)


@router.get("/runs", response_model=RunListResponse)
def list_runs(request: Request, limit: int = Query(default=20, ge=1, le=100)):
    """最近同步执行记录（默认 20 条，§52.3）。"""
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        rows = HistorySyncRunRepository(session).list_recent(limit)
        return RunListResponse(items=[_run_item(row) for row in rows])


@router.get("/runs/{run_id}", response_model=RunDetailResponse)
def get_run(request: Request, run_id: str):
    """run 详情 + 每个 dataset 的执行详情 + 运行中进度（页面轮询用，§52.4/D10）。"""
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        run = HistorySyncRunRepository(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="执行记录不存在")
        rows = HistorySyncRunDatasetRepository(session).list_for_run(run_id)

    # 运行中时附带实时 progress 快照（进程内内存，不扫库）
    progress = None
    if run.status == RunStatus.RUNNING.value:
        progress = _current_progress(request)

    return RunDetailResponse(
        run=_run_item(run),
        datasets=[_dataset_detail(row) for row in rows],
        progress=progress,
    )


# ---- 个股列表（8.2） ----


@router.get("/stocks", response_model=StockListResponse)
def list_stocks(
    request: Request,
    dataset: str = Query(..., description="数据集名称，仅支持日级数据集"),
    status: str = Query("all", description="筛选状态：all/success/failed"),
    q: str = Query("", description="名称或 ts_code 搜索关键字"),
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
):
    """个股历史同步状态列表（design D12，tasks 8.2）。

    - dataset 必填，仅四日级数据集，其余返回 422；
    - status=all|success|failed；
    - q 按 name / ts_code LIKE 模糊匹配；
    - page_size 服务端固定 100；
    - 默认排序：last_status='failed' DESC → watermark_date ASC NULLS FIRST → ts_code ASC。
    - 查询 = cn_stock_basic LEFT JOIN stock_sync_state ON instrument_id AND dataset=?，
      筛选/搜索/排序全在 SQL 完成，不加载全量到 Python。
    """
    if dataset not in _DAY_LEVEL_DATASET_VALUES:
        raise HTTPException(
            status_code=422,
            detail=f"dataset 必须是日级数据集之一：{sorted(_DAY_LEVEL_DATASET_VALUES)}",
        )
    if status not in STOCK_STATUS_OPTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"status 必须是 {sorted(STOCK_STATUS_OPTIONS)} 之一",
        )

    config: AppConfig = request.app.state.config
    session_factory = request.app.state.session_factory
    policy = AvailabilityPolicy(config)
    open_days = _cached_open_days(request)
    target = _dataset_target(DatasetName(dataset), policy=policy, open_days=open_days)

    with session_factory() as session:
        # 总条数（用于分页）
        count_stmt = select(func.count(CnStockBasic.instrument_id))
        count_stmt = count_stmt.outerjoin(
            StockSyncState,
            and_(
                StockSyncState.instrument_id == CnStockBasic.instrument_id,
                StockSyncState.dataset == dataset,
            ),
        )
        if status == "success":
            count_stmt = count_stmt.where(StockSyncState.last_status == "success")
        elif status == "failed":
            count_stmt = count_stmt.where(StockSyncState.last_status == "failed")
        if q:
            like = f"%{q}%"
            count_stmt = count_stmt.where(
                or_(CnStockBasic.name.ilike(like), CnStockBasic.ts_code.ilike(like))
            )
        total = int(session.scalar(count_stmt) or 0)

        total_pages = max(1, math.ceil(total / STOCKS_PAGE_SIZE))
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * STOCKS_PAGE_SIZE

        # 列表查询
        stmt = (
            select(
                CnStockBasic.ts_code,
                CnStockBasic.name,
                CnStockBasic.list_date,
                CnStockBasic.delist_date,
                StockSyncState.watermark_date,
                StockSyncState.last_status,
                StockSyncState.last_error_code,
                StockSyncState.last_error,
                StockSyncState.last_success_at,
                StockSyncState.last_attempt_at,
            )
            .select_from(CnStockBasic)
            .outerjoin(
                StockSyncState,
                and_(
                    StockSyncState.instrument_id == CnStockBasic.instrument_id,
                    StockSyncState.dataset == dataset,
                ),
            )
        )
        if status == "success":
            stmt = stmt.where(StockSyncState.last_status == "success")
        elif status == "failed":
            stmt = stmt.where(StockSyncState.last_status == "failed")
        if q:
            like = f"%{q}%"
            stmt = stmt.where(
                or_(CnStockBasic.name.ilike(like), CnStockBasic.ts_code.ilike(like))
            )
        # 排序：失败优先 → 水位升序（NULL 最前） → ts_code 升序
        stmt = stmt.order_by(
            text("CASE WHEN stock_sync_state.last_status = 'failed' THEN 0 ELSE 1 END"),
            text("stock_sync_state.watermark_date ASC NULLS FIRST"),
            CnStockBasic.ts_code.asc(),
        )
        stmt = stmt.limit(STOCKS_PAGE_SIZE).offset(offset)
        rows = session.execute(stmt).all()

        items = [
            StockListItem(
                ts_code=row.ts_code,
                name=row.name,
                list_date=_ds(row.list_date),
                delist_date=_ds(row.delist_date),
                watermark_date=_ds(row.watermark_date),
                last_status=row.last_status,
                last_error_code=row.last_error_code,
                last_error=row.last_error,
                last_success_at=_iso(row.last_success_at),
                last_attempt_at=_iso(row.last_attempt_at),
            )
            for row in rows
        ]

        # 统计块（复用 summary 口径）
        stats_dict = _aggregate_stock_stats(session, dataset, target)
        stats = StockStats(**stats_dict)

    return StockListResponse(
        items=items,
        stats=stats,
        pagination=StockPagination(
            page=page,
            page_size=STOCKS_PAGE_SIZE,
            total=total,
            total_pages=total_pages,
        ),
    )


# ---- 任务详情（8.3） ----


@router.get("/tasks/{task_id}", response_model=TaskDetailResponse)
def get_task(request: Request, task_id: int):
    """任务详情（按 id 直查 sync_task + JOIN 主档补 stock_name），只读。"""
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        task = SyncTaskRepository(session).find_by_id(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")

        # JOIN 主档补 stock_name
        stock_name = session.scalar(
            select(CnStockBasic.name).where(
                CnStockBasic.instrument_id == task.instrument_id
            )
        )

    return TaskDetailResponse(
        id=task.id,
        run_id=task.run_id,
        dataset=task.dataset,
        instrument_id=task.instrument_id,
        ts_code=task.ts_code,
        stock_name=stock_name,
        start_date=_ds(task.start_date) or "",
        end_date=_ds(task.end_date) or "",
        status=task.status,
        retry_count=task.retry_count or 0,
        attempt_count=task.attempt_count or 0,
        records_fetched=task.records_fetched or 0,
        records_written=task.records_written or 0,
        error_code=task.error_code,
        error_type=task.error_type,
        error_message=task.error_message,
        started_at=_iso(task.started_at) or "",
        finished_at=_iso(task.finished_at),
        duration_ms=task.duration_ms,
    )


@router.get("/datasets")
def list_datasets(request: Request):
    """数据集元信息（页面 chip 切换用，前端避免硬编码数据集列表）。"""
    return {
        "daily": [
            {"dataset": ds.value, "display_name": DISPLAY_NAMES[ds.value]}
            for ds in DAY_LEVEL_DATASETS
        ],
        "master": [
            {"dataset": ds.value, "display_name": DISPLAY_NAMES[ds.value]}
            for ds in MASTER_DATASETS
        ],
    }
