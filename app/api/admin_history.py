"""历史数据管理 API（a-share-historical-data，技术方案 §52、tasks 7.2）。

- Router 层统一 ``require_admin``：未登录 401、普通用户 403；POST 走既有
  CSRF 中间件（不新增绕过 CSRF 的写接口）。
- summary / runs 只读 ``history_sync_state`` / ``history_sync_run*`` 与已缓存
  的交易日历等同步小表，SHALL NOT 扫描事实大表，也 SHALL NOT 触发网络请求
  （§52.1、D21：GET 接口不得因读取而拉取上游数据）。
- 手动同步：HTTP 不等待回填完成，202 立即返回；已有任务运行时 409 附当前
  run_id（§52.2）。``requested_by_user_id`` 由服务端从当前认证用户取得，
  绝不接受客户端传值。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.auth.dependencies import require_admin
from app.auth.session import CurrentUser
from app.config import BUSINESS_TZ_NAME, AppConfig
from app.models.history_sync import (
    DatasetName,
    DatasetStatus,
    RunStatus,
)
from app.repositories.history_sync import (
    HistorySyncRunDatasetRepository,
    HistorySyncRunRepository,
    HistorySyncStateRepository,
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
    SyncConflictResponse,
    SyncStartResponse,
)
from app.services.history.availability import AvailabilityPolicy
from app.services.history.planner import HistorySyncPlanner
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


def _daily_summary(
    state, *, open_days: list[date], policy: AvailabilityPolicy
) -> DailyDatasetSummary:
    dataset = DatasetName(state.dataset)
    target = policy.latest_expected_trade_date(
        dataset, now=now_beijing(), strict_open_days=open_days
    ) or state.latest_expected_trade_date
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
        lag_trade_days=HistorySyncPlanner.lag_days(
            latest_complete=state.latest_complete_trade_date,
            latest_expected=target,
            open_days=open_days,
        ),
        record_count=state.record_count or 0,
        current_trade_date=_ds(state.current_trade_date),
        current_attempt=state.current_attempt or 0,
        last_success_at=_iso(state.last_success_at),
        last_error_code=state.last_error_code,
        last_error=state.last_error,
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
    """整体状态（§84）：RUNNING > ERROR > LAGGING > WAITING > HEALTHY。

    核心主档（trade_cal/stock_basic）的 FAILED 必须先于"尚未开始"判定：
    硬前置失败时日级数据集根本无法推进，此时显示 ERROR 才是可行动的
    （去修 Token/网络），显示 UNINITIALIZED 会误导为"还没开始"。
    """
    statuses = {item.dataset: item.status for item in daily}
    # 硬前置失败升级整体异常（日历/主档失败时日级数据集根本无法推进）
    for item in master:
        if item.dataset in CORE_MASTER_DATASETS and item.status == DatasetStatus.FAILED.value:
            return OVERALL_ERROR
    if not [s for s in statuses.values() if s != DatasetStatus.UNINITIALIZED.value]:
        return OVERALL_UNINITIALIZED
    if DatasetStatus.FAILED.value in statuses.values():
        return OVERALL_ERROR

    lagging = [item for item in daily if item.lag_trade_days > 0]
    if lagging:
        # 落后但全部在等数据源发布 → WAITING（而非 LAGGING）
        if all(item.status == DatasetStatus.WAITING_SOURCE.value for item in lagging):
            return OVERALL_WAITING
        return OVERALL_LAGGING
    # 无落后：等发布 → WAITING；推进中/已追平 → HEALTHY（SYNCING/RETRYING/
    # CHECKING 属合法非终态，未落后就不该报 LAGGING，否则页面自相矛盾：
    # 同一响应里四个数据集 lag_trade_days 全为 0 却显示"落后"）
    if DatasetStatus.WAITING_SOURCE.value in statuses.values():
        return OVERALL_WAITING
    healthy = {
        DatasetStatus.CAUGHT_UP.value,
        DatasetStatus.SYNCING.value,
        DatasetStatus.RETRYING.value,
        DatasetStatus.CHECKING.value,
    }
    return OVERALL_HEALTHY if all(s in healthy for s in statuses.values()) else OVERALL_LAGGING


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
        start_watermark=_ds(row.start_watermark),
        target_trade_date=_ds(row.target_trade_date),
        end_watermark=_ds(row.end_watermark),
        start_cursor=row.start_cursor,
        end_cursor=row.end_cursor,
        dates_completed=row.dates_completed or 0,
        rows_written=row.rows_written or 0,
        request_count=row.request_count or 0,
        retry_count=row.retry_count or 0,
        failed_trade_date=_ds(row.failed_trade_date),
        last_error_code=row.last_error_code,
        last_error=row.last_error,
        started_at=_iso(row.started_at),
        finished_at=_iso(row.finished_at),
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

    daily = [
        _daily_summary(states[dataset.value], open_days=open_days, policy=policy)
        if dataset.value in states
        else DailyDatasetSummary(
            dataset=dataset.value,
            display_name=DISPLAY_NAMES[dataset.value],
            status=DatasetStatus.UNINITIALIZED.value,
            history_start_date=config.history.start_date.isoformat(),
        )
        for dataset in DAY_LEVEL_DATASETS
    ]
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
        # 日历过期时 lag_trade_days 恒为 0，不能据此断言"已追平"（§84）：
        # 未初始化仍显示未初始化，已开始则报落后（数据实际可能落后数月）。
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
    task = asyncio.create_task(
        asyncio.to_thread(job.run_manual, current_user.user_id, run_id=run_id),
        name="history-sync-manual",
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    logger.info(
        "管理员触发历史数据同步 user_id=%s run_id=%s", current_user.user_id, run_id
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
    """run 详情 + 每个 dataset 的执行详情（页面轮询当前进度用，§52.4）。"""
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        run = HistorySyncRunRepository(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="执行记录不存在")
        rows = HistorySyncRunDatasetRepository(session).list_for_run(run_id)
        return RunDetailResponse(
            run=_run_item(run), datasets=[_dataset_detail(row) for row in rows]
        )
