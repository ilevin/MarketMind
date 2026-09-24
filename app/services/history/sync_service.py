"""历史同步统一编排（per-stock-history-sync，design D7/D9/D10，
spec.md"统一入口与触发方式"/"进程中断与恢复"/"个股失败隔离与缺口不跳过"/
"单股区间原子提交与幂等"/"股票生命周期边界"）。

``HistorySyncService.run`` 为唯一业务入口：

    recover_stale_runs -> ensure_master_prerequisites（trade_cal/stock_basic
    硬前置；company/namechange 非阻塞） -> 四个日级数据集顺序执行
    （universe 逐股：planner 算有效区间 → 无工作 skipped；有工作交
    StockSyncExecutor → 单股失败 continue） -> finalize_run

Job 层（进程级 single-flight、cancellation Event 构造、asyncio.to_thread）
不在本文件职责内——本 Service 只接受可选的 ``threading.Event`` 并在检查点
读取，不管理其生命周期。

``StockSyncProgress`` 为进程内进度快照对象（design D10）：高频变化的
"当前 ts_code"不逐股写库，聚合计数以 DB 为准；后续任务挂到 app.state。
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from sqlalchemy.exc import SQLAlchemyError

from app.config import AppConfig
from app.db import write_coordinator
from app.models.history_sync import (
    DatasetKind,
    DatasetName,
    DatasetStatus,
    RunDatasetStatus,
    RunStatus,
    TriggerType,
)
from app.providers.base import ProviderBatch
from app.providers.history import HistoryProviderRegistry
from app.providers.tushare_common import TushareError
from app.providers.trading_calendar.provider import (
    CalendarUnavailableError,
    TushareTradingCalendarProvider,
)
from app.repositories.history_fact import HistoryFactRepository
from app.repositories.history_master import HistoryMasterRepository
from app.repositories.history_sync import (
    HistorySyncRunDatasetRepository,
    HistorySyncRunRepository,
    HistorySyncStateRepository,
    StockSyncStateRepository,
    SyncTaskRepository,
)
from app.repositories.trading_calendar import TradingCalendarRepository
from app.services.history.availability import AvailabilityPolicy
from app.services.history.planner import HistorySyncPlanner
from app.services.history.retry import RetryPolicy, is_config_error
from app.services.history.stock_executor import StockSyncExecutor, TaskOutcome
from app.services.market_session_service import now_beijing

logger = logging.getLogger(__name__)

# 四个日级数据集的处理顺序（互不阻塞：一个 FAILED 不影响后续数据集推进）
DAY_LEVEL_DATASETS: tuple[DatasetName, ...] = (
    DatasetName.DAILY,
    DatasetName.ADJ_FACTOR,
    DatasetName.DAILY_BASIC,
    DatasetName.MONEYFLOW,
)

CN_MARKET = "CN"

# namechange bootstrap 每批处理的证券数（§10.2：逐只推进、可中断续跑；
# 批次边界同时是停机信号检查点，§49"每个 master 分片之间"）
NAMECHANGE_BOOTSTRAP_CHUNK_SIZE = 50

# instrument.exchange -> ts_code 后缀（与 tushare Provider 内部映射一致，§7.1）
_EXCHANGE_SUFFIX: dict[str, str] = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}


def _exchange_suffix(exchange: str | None) -> str:
    """instrument.exchange -> ts_code 后缀；未知/为空按 UNKNOWN_INSTRUMENT

    抛领域异常而非裸 ValueError：调用方按错误码记录并归入主档失败，不能因
    一个脏 exchange 值让 namechange（非阻塞主档）把整轮 run 拖垮。
    """
    if exchange not in _EXCHANGE_SUFFIX:
        raise TushareError(
            f"证券主档 exchange 无法映射为 ts_code 后缀: {exchange!r}",
            error_code="UNKNOWN_INSTRUMENT",
        )
    return _EXCHANGE_SUFFIX[exchange]


def _elapsed_ms(started: float) -> int:
    return max(int((time.monotonic() - started) * 1000), 0)


# 错误文本脱敏（§51.3：禁止 Token / 完整敏感请求对象 / 认证配置进入错误文本）。
# 上游 tushare_common 已保证自身构造的消息不含 Token，此处对可能透传的
# SDK 消息做最后一道过滤（属性名或形如 40 位十六进制 token 的片段）。
_TOKEN_PATTERN = re.compile(r"(?i)(token[\"'=:\s]*)([A-Za-z0-9]{16,})")


def _safe_error_text(exc: BaseException, *, limit: int = 500) -> str:
    """错误文本入库/入日志前的脱敏与截断（§51.3、§62）。"""
    text = _TOKEN_PATTERN.sub(r"\1***", str(exc))
    return text[:limit]


# 业务层可识别的异常：Provider 抛出的领域异常、日历不可用，加上超时。
# 三者都不是彼此的父类，也不是 TushareError 子类：
# - ``CalendarUnavailableError`` 继承 RuntimeError（日历 Provider）；
# - ``call_with_metrics`` 的线程级限时抛**内建** TimeoutError
#   （app/observability/provider_metrics.py）。
# 任何一个漏掉都会穿透重试循环，把整轮 run 拖成非终态（§27/§29）。
SYNC_ERRORS: tuple[type[BaseException], ...] = (
    TushareError,
    CalendarUnavailableError,
    TimeoutError,
)


def _error_code_of(exc: BaseException) -> str:
    """异常 -> 标准化错误码（§51.3）。

    领域异常自带 ``error_code``；裸 ``TimeoutError``（框架级超时）按
    TUSHARE_TIMEOUT 归类，与 Provider 侧 TushareTimeoutError 口径一致。
    """
    code = getattr(exc, "error_code", None)
    if isinstance(code, str):
        return code
    if isinstance(exc, TimeoutError):
        return "TUSHARE_TIMEOUT"
    return "INTERNAL_ERROR"


@dataclass
class StockSyncProgress:
    """进程内实时进度快照（design D10）。

    高频变化的"当前 ts_code"不逐股写库；聚合计数以 DB 为准，progress
    仅用于 API 实时展示。由 Service 在 run 期间更新，后续任务挂到
    app.state。

    字段：
        current_dataset: 当前处理的数据集（None = 未开始或已结束）
        current_ts_code: 当前处理的股票代码（None = 处理中但无该股信息）
        processed: 已处理股票数（成功 + 失败）
        succeeded: 成功股票数
        failed: 失败股票数
        skipped: 跳过（无工作）股票数
    """

    current_dataset: str | None = None
    current_ts_code: str | None = None
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0

    def reset(self) -> None:
        """数据集切换时重置计数。"""
        self.current_dataset = None
        self.current_ts_code = None
        self.processed = 0
        self.succeeded = 0
        self.failed = 0
        self.skipped = 0

    def record_outcome(self, outcome: TaskOutcome) -> None:
        """根据 TaskOutcome 更新计数。"""
        from app.models.history_sync import TASK_STATUS_SUCCESS, TASK_STATUS_FAILED

        self.processed += 1
        if outcome.status == TASK_STATUS_SUCCESS:
            self.succeeded += 1
        elif outcome.status == TASK_STATUS_FAILED:
            self.failed += 1

    def record_skip(self) -> None:
        self.skipped += 1


class HistorySyncService:
    """历史同步统一编排；``run`` 为唯一业务入口（§17~§29）。"""

    def __init__(
        self,
        config: AppConfig,
        session_factory,
        history_providers: HistoryProviderRegistry,
        calendar_provider: TushareTradingCalendarProvider,
        *,
        sleep: Callable[[float], None] | None = None,
        random_fn: Callable[[], float] | None = None,
    ):
        self.config = config
        self.session_factory = session_factory
        self.providers = history_providers
        self.calendar_provider = calendar_provider
        self.availability = AvailabilityPolicy(config)
        self.retry_policy = RetryPolicy(config, sleep=sleep, random_fn=random_fn)
        self.stock_executor = StockSyncExecutor(
            config=config,
            session_factory=session_factory,
            providers=history_providers,
            retry_policy=self.retry_policy,
        )
        # 进程内进度快照（design D10：实时展示用，DB 为权威计数）
        self.progress = StockSyncProgress()

    # ---- 唯一入口 ----

    def run(
        self,
        *,
        trigger: TriggerType,
        requested_by_user_id: str | None = None,
        cancellation_event: threading.Event | None = None,
        run_id: str | None = None,
    ) -> str:
        """执行一次统一同步；返回 run_id。异常仅在 run 记录创建失败时向上传播
        （该情形本身即无法记录到 history_sync_run，其余异常均被内部捕获归
        入该数据集 FAILED，不影响其余数据集/整体 run 记录）。

        任何非预期异常（数据库错误、代码缺陷）都在数据集边界被捕获并记为
        该数据集 INTERNAL_ERROR，随后仍进入 finalize——绝不让 run 永久停在
        RUNNING（否则页面永远显示"运行中"，且下次启动才被标 INTERRUPTED）。

        ``run_id`` 可由调用方预留（管理员 API 需在 202 响应中立即回传
        run_id，而回填不能阻塞 HTTP）。
        """
        run_id = run_id or str(uuid.uuid4())
        started_at = now_beijing()
        with write_coordinator.write():
            with self.session_factory() as session:
                HistorySyncRunRepository(session).create(
                    run_id,
                    trigger_type=trigger,
                    requested_by_user_id=requested_by_user_id,
                    started_at=started_at,
                )
                session.commit()

        outcomes: dict[DatasetName, str] = {}
        master_ok = False
        aborted = False
        try:
            self.recover_stale_runs(exclude_run_id=run_id)
            master_ok = self.ensure_master_prerequisites(
                run_id, cancellation_event=cancellation_event
            )
            if not master_ok:
                return run_id

            for dataset in DAY_LEVEL_DATASETS:
                if cancellation_event is not None and cancellation_event.is_set():
                    outcomes[dataset] = "CANCELLED"
                    continue
                try:
                    outcomes[dataset] = self._sync_stock_dataset(
                        run_id, dataset, cancellation_event=cancellation_event
                    )
                except Exception as exc:  # 非预期异常：记录并继续其他数据集
                    logger.exception(
                        "数据集出现非预期异常，本轮跳过该数据集 dataset=%s run_id=%s",
                        dataset.value, run_id,
                    )
                    self._record_unexpected_failure(dataset, run_id, exc)
                    outcomes[dataset] = "FAILED"
        finally:
            self._finalize_run(
                run_id,
                dataset_outcomes=outcomes if master_ok else {},
                master_ok=master_ok,
                aborted=aborted,
            )
            # 结束时清空 progress 当前状态
            self.progress.current_dataset = None
            self.progress.current_ts_code = None
        return run_id

    def _record_unexpected_failure(
        self, dataset: DatasetName, run_id: str, exc: BaseException
    ) -> None:
        """非预期异常的兜底落库：state 与 run_dataset 都记 INTERNAL_ERROR。

        自身失败不得再抛出——否则会掩盖原始异常并让 run 停在 RUNNING。
        """
        try:
            self._set_state_error(
                dataset, error_code="INTERNAL_ERROR",
                error=_safe_error_text(exc), status=DatasetStatus.FAILED,
            )
            with write_coordinator.write():
                with self.session_factory() as session:
                    run_repo = HistorySyncRunDatasetRepository(session)
                    if run_repo.get(run_id, dataset) is None:
                        run_repo.start(
                            run_id, dataset, start_watermark=None,
                            target_trade_date=None, started_at=now_beijing(),
                        )
                    run_repo.finish(
                        run_id, dataset, status=RunDatasetStatus.FAILED,
                        finished_at=now_beijing(),
                        last_error_code="INTERNAL_ERROR", last_error=_safe_error_text(exc),
                    )
                    session.commit()
        except Exception:  # pragma: no cover - 兜底路径自身失败只记录，不再传播
            logger.exception("记录非预期异常失败 dataset=%s run_id=%s", dataset.value, run_id)

    # ---- 启动恢复（§19，design D9）----

    def recover_stale_runs(self, *, exclude_run_id: str | None = None) -> None:
        """遗留 RUNNING run 标 INTERRUPTED；SYNCING/RETRYING/CHECKING 的
        state 恢复为 LAGGING/CAUGHT_UP（依据水位，不依据"曾经 RUNNING"猜测
        某日已完成）；遗留 running 状态的 sync_task 批量置 interrupted。

        per-stock-history-sync D9 扩展：把属于已中断 Run 的 running 状态
        sync_task 批量置 interrupted（补 finished_at）。**绝不推进对应水位**
        （task 未提交，水位本就未动）。全部动作在同一写锁事务内。
        """
        now = now_beijing()
        with write_coordinator.write():
            with self.session_factory() as session:
                run_repo = HistorySyncRunRepository(session)
                stale_runs = [
                    r for r in run_repo.find_stale_running() if r.run_id != exclude_run_id
                ]
                stale_run_ids = [r.run_id for r in stale_runs]
                for stale in stale_runs:
                    run_repo.mark_interrupted(stale.run_id, finished_at=now)
                    logger.warning(
                        "恢复遗留 RUNNING 任务为 INTERRUPTED: run_id=%s", stale.run_id
                    )

                # D9：遗留 running 的 sync_task 批量置 interrupted
                if stale_run_ids:
                    task_repo = SyncTaskRepository(session)
                    interrupted_count = task_repo.interrupt_running_for_runs(
                        stale_run_ids, finished_at=now
                    )
                    if interrupted_count:
                        logger.warning(
                            "恢复遗留 running sync_task 为 interrupted: count=%d run_ids=%s",
                            interrupted_count, stale_run_ids,
                        )

                state_repo = HistorySyncStateRepository(session)
                for state in state_repo.all_states():
                    if state.status not in (
                        DatasetStatus.SYNCING.value,
                        DatasetStatus.RETRYING.value,
                        DatasetStatus.CHECKING.value,
                    ):
                        continue
                    recovered = (
                        DatasetStatus.CAUGHT_UP
                        if state.status == DatasetStatus.CHECKING.value
                        else DatasetStatus.LAGGING
                    )
                    state_repo.begin_attempt(
                        state.dataset,
                        state.current_trade_date or state.latest_complete_trade_date,
                        0,
                        status=recovered,
                    )
                session.commit()

    # ---- 主档前置与刷新周期（design.md 第 11 节）----

    def ensure_master_prerequisites(
        self,
        run_id: str,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> bool:
        """trade_cal/stock_basic 硬前置失败返回 False（阻止日级数据集推进）；
        company/namechange 失败仅记录，不影响返回值。

        非阻塞主档的**任何**异常都被吞掉（§40.1：不得阻塞日级数据集）——
        包括落库阶段的 SQLAlchemyError，否则它会穿透 run() 让整轮 run 被
        误判为"硬前置失败"，日出数据集一行不写。
        """
        trade_cal_ok = self._ensure_trade_cal(run_id)
        stock_basic_ok = self._ensure_stock_basic(run_id) if trade_cal_ok else False
        if not (trade_cal_ok and stock_basic_ok):
            return False

        for dataset, call in (
            (DatasetName.STOCK_COMPANY, lambda: self._ensure_stock_company(run_id)),
            (
                DatasetName.NAMECHANGE,
                lambda: self._ensure_namechange(run_id, cancellation_event),
            ),
        ):
            try:
                call()
            except Exception as exc:  # 非阻塞主档：绝不影响本轮日级推进
                logger.exception(
                    "非阻塞主档刷新出现非预期异常，跳过该主档 run_id=%s dataset=%s",
                    run_id, dataset.value,
                )
                self._record_master_failure(
                    dataset, run_id, _error_code_of(exc), _safe_error_text(exc)
                )
        return True

    def _ensure_trade_cal(self, run_id: str) -> bool:
        """trade_cal 缺失即刷新（strict 模式按年缓存，§3）。"""
        today = now_beijing().date()
        try:
            self.calendar_provider.get_days(
                CN_MARKET, self.config.history.start_date, today, strict=True
            )
            self._record_master_success(DatasetName.TRADE_CAL, run_id)
            return True
        except CalendarUnavailableError as exc:
            self._record_master_failure(
                DatasetName.TRADE_CAL, run_id,
                _error_code_of(exc), _safe_error_text(exc),
            )
            logger.error("trade_cal 硬前置失败，阻止本轮日级数据集推进: %s", exc)
            return False

    def _ensure_stock_basic(self, run_id: str) -> bool:
        """超过 stock_basic_refresh_hours 未成功则刷新；Registry 已按 §8.3
        分片规避行数上限，本层只处理截断风险与落库。"""
        with self.session_factory() as session:
            state = HistorySyncStateRepository(session).get(DatasetName.STOCK_BASIC)
        if state is not None and state.last_success_at is not None:
            age_hours = (now_beijing() - state.last_success_at).total_seconds() / 3600
            if age_hours < self.config.history.stock_basic_refresh_hours:
                return True
        return self._refresh_stock_basic(run_id, context="stock_basic 前置")

    def _refresh_stock_basic(self, run_id: str, *, context: str) -> bool:
        """无条件刷新 stock_basic 并落库（§35.1 未知证券恢复亦复用本方法，
        故不带时效判断）。网络请求在写锁外完成。"""
        try:
            batch: ProviderBatch = self.providers.get_stock_basic()
            if batch.truncation_risk:
                raise TushareError(
                    "stock_basic 命中行数上限，截断风险", error_code="TRUNCATION_RISK"
                )
            fetched_at = now_beijing()
            with write_coordinator.write():
                with self.session_factory() as session:
                    count = HistoryMasterRepository(session).upsert_stock_basic(
                        batch.records, source=self.providers.source, run_id=run_id,
                        fetched_at=fetched_at,
                    )
                    state_repo = HistorySyncStateRepository(session)
                    state_repo.ensure(
                        DatasetName.STOCK_BASIC, dataset_kind=DatasetKind.MASTER
                    )
                    state_repo.update_master(DatasetName.STOCK_BASIC, record_count=count)
                    state_repo.finish_success(
                        DatasetName.STOCK_BASIC, status=DatasetStatus.CAUGHT_UP
                    )
                    run_repo = HistorySyncRunDatasetRepository(session)
                    if run_repo.get(run_id, DatasetName.STOCK_BASIC) is None:
                        run_repo.start(
                            run_id, DatasetName.STOCK_BASIC, start_watermark=None,
                            target_trade_date=None, started_at=now_beijing(),
                        )
                    run_repo.add_counts(
                        run_id, DatasetName.STOCK_BASIC, rows=count, requests=1
                    )
                    session.commit()
            return True
        except SYNC_ERRORS as exc:
            self._record_master_failure(
                DatasetName.STOCK_BASIC, run_id, _error_code_of(exc), _safe_error_text(exc)
            )
            logger.error("%s失败，阻止本轮日级数据集推进: %s", context, exc)
            return False

    def _record_master_success(
        self,
        dataset: DatasetName,
        run_id: str,
        *,
        rows: int = 0,
        requests: int = 0,
    ) -> None:
        """主档成功：置 CAUGHT_UP，并把本行累计到 run_dataset（§20）。"""
        with write_coordinator.write():
            with self.session_factory() as session:
                state_repo = HistorySyncStateRepository(session)
                state_repo.ensure(dataset, dataset_kind=DatasetKind.MASTER)
                state_repo.finish_success(dataset, status=DatasetStatus.CAUGHT_UP)
                run_repo = HistorySyncRunDatasetRepository(session)
                if run_repo.get(run_id, dataset) is None:
                    run_repo.start(
                        run_id, dataset, start_watermark=None,
                        target_trade_date=None, started_at=now_beijing(),
                    )
                run_repo.add_counts(run_id, dataset, rows=rows, requests=requests)
                session.commit()

    def _record_master_failure(
        self, dataset: DatasetName, run_id: str, error_code: str, error: str
    ) -> None:
        with write_coordinator.write():
            with self.session_factory() as session:
                state_repo = HistorySyncStateRepository(session)
                state_repo.ensure(dataset, dataset_kind=DatasetKind.MASTER)
                state_repo.finish_error(dataset, error_code=error_code, error=error)
                session.commit()

    def _needs_master_refresh(self, dataset: DatasetName) -> bool:
        with self.session_factory() as session:
            state = HistorySyncStateRepository(session).get(dataset)
        if state is None or state.last_success_at is None:
            return True
        age_days = (now_beijing() - state.last_success_at).total_seconds() / 86400
        return age_days >= self.config.history.master_refresh_days

    def _ensure_stock_company(self, run_id: str) -> None:
        """每 7 天按 exchange 分片刷新（§9.2）；失败仅记录，不阻塞日级数据集。"""
        if not self._needs_master_refresh(DatasetName.STOCK_COMPANY):
            return
        try:
            batch: ProviderBatch = self.providers.get_stock_company()
            if batch.truncation_risk:
                raise TushareError(
                    "stock_company 命中行数上限，截断风险", error_code="TRUNCATION_RISK"
                )
            fetched_at = now_beijing()
            with write_coordinator.write():
                with self.session_factory() as session:
                    count = HistoryMasterRepository(session).upsert_stock_company(
                        batch.records, source=self.providers.source, run_id=run_id,
                        fetched_at=fetched_at,
                    )
                    state_repo = HistorySyncStateRepository(session)
                    state_repo.ensure(
                        DatasetName.STOCK_COMPANY, dataset_kind=DatasetKind.MASTER
                    )
                    state_repo.update_master(DatasetName.STOCK_COMPANY, record_count=count)
                    state_repo.finish_success(
                        DatasetName.STOCK_COMPANY, status=DatasetStatus.CAUGHT_UP
                    )
                    run_repo = HistorySyncRunDatasetRepository(session)
                    if run_repo.get(run_id, DatasetName.STOCK_COMPANY) is None:
                        run_repo.start(
                            run_id, DatasetName.STOCK_COMPANY, start_watermark=None,
                            target_trade_date=None, started_at=now_beijing(),
                        )
                    run_repo.add_counts(
                        run_id, DatasetName.STOCK_COMPANY, rows=count, requests=1
                    )
                    session.commit()
        except SYNC_ERRORS as exc:
            self._record_master_failure(
                DatasetName.STOCK_COMPANY, run_id, _error_code_of(exc), _safe_error_text(exc)
            )
            logger.error("stock_company 非阻塞刷新失败（不影响日级数据集）: %s", exc)

    def _ensure_namechange(
        self, run_id: str, cancellation_event: threading.Event | None = None
    ) -> None:
        """首次 bootstrap 按 ts_code 排序逐只获取（master_cursor 可中断续
        跑）；之后每 7 天窗口增量（重叠窗口替换保留更早历史，§10.2/§10.3）。
        失败仅记录，不阻塞日级数据集。"""
        with self.session_factory() as session:
            state = HistorySyncStateRepository(session).get(DatasetName.NAMECHANGE)
        bootstrap_complete = bool(state and state.bootstrap_complete)

        try:
            if not bootstrap_complete:
                self._namechange_bootstrap_step(run_id, state, cancellation_event)
            elif self._needs_master_refresh(DatasetName.NAMECHANGE):
                self._namechange_window_refresh(run_id, state)
        except SYNC_ERRORS as exc:
            self._record_master_failure(
                DatasetName.NAMECHANGE, run_id,
                _error_code_of(exc), _safe_error_text(exc),
            )
            logger.error("namechange 非阻塞刷新失败（不影响日级数据集）: %s", exc)

    def _namechange_bootstrap_step(
        self, run_id: str, state, cancellation_event: threading.Event | None = None
    ) -> None:
        """逐只推进：取 master_cursor 之后按 symbol 排序的一批证券（每个
        master 分片之间检查停机信号，§49），整证券替换其改名历史；全部
        证券处理完毕后置 bootstrap_complete=True。

        网络请求在写锁外（design D7/D8），仅整批替换的落库持锁。
        """
        with self.session_factory() as session:
            instruments = HistoryMasterRepository(session).list_cn_stock_instruments()
        cursor = state.master_cursor if state else None
        pending = [inst for inst in instruments if cursor is None or inst.symbol > cursor]
        if not pending:
            self._finish_namechange_bootstrap(run_id)
            return

        chunk = pending[:NAMECHANGE_BOOTSTRAP_CHUNK_SIZE]
        fetched_at = now_beijing()

        # 锁外：逐只请求（请求间隙检查停机信号）
        fetched: list[tuple[str, ProviderBatch]] = []
        for inst in chunk:
            if cancellation_event is not None and cancellation_event.is_set():
                logger.info(
                    "namechange bootstrap 收到停机信号，已取 %d 只，游标保持 %s",
                    len(fetched), cursor,
                )
                break
            ts_code = f"{inst.symbol}.{_exchange_suffix(inst.exchange)}"
            fetched.append((inst.instrument_id, self.providers.get_name_changes(ts_code=ts_code)))

        if not fetched:
            return  # 未取到任何证券：本轮不推进，游标不变

        # 整批替换的落库：单事务、写锁内（锁外已完成全部网络请求）
        with write_coordinator.write():
            with self.session_factory() as session:
                master_repo = HistoryMasterRepository(session)
                state_repo = HistorySyncStateRepository(session)
                state_repo.ensure(DatasetName.NAMECHANGE, dataset_kind=DatasetKind.MASTER)
                rows = 0
                for instrument_id, batch in fetched:
                    rows += master_repo.replace_name_changes_for_instrument(
                        instrument_id, batch.records,
                        source=self.providers.source, run_id=run_id, fetched_at=fetched_at,
                    )
                # 仅当本批取满且本批已是最后一批时才算 bootstrap 完成
                is_last_chunk = len(fetched) == len(pending)
                state_repo.update_master(
                    DatasetName.NAMECHANGE,
                    master_cursor=chunk[len(fetched) - 1].symbol,
                    bootstrap_complete=is_last_chunk,
                )
                if is_last_chunk:
                    state_repo.finish_success(
                        DatasetName.NAMECHANGE, status=DatasetStatus.CAUGHT_UP
                    )
                # §20：run_dataset 记本行进度（bootstrap 分批推进期间也如实计数，
                # 与 stock_basic/stock_company 同口径）
                run_repo = HistorySyncRunDatasetRepository(session)
                if run_repo.get(run_id, DatasetName.NAMECHANGE) is None:
                    run_repo.start(
                        run_id, DatasetName.NAMECHANGE, start_watermark=None,
                        target_trade_date=None, started_at=fetched_at,
                    )
                run_repo.add_counts(
                    run_id, DatasetName.NAMECHANGE, rows=rows, requests=len(fetched)
                )
                session.commit()

    def _finish_namechange_bootstrap(self, run_id: str) -> None:
        """无待处理证券：置 bootstrap_complete=True（可能因游标已到末尾）。"""
        with write_coordinator.write():
            with self.session_factory() as session:
                state_repo = HistorySyncStateRepository(session)
                state_repo.ensure(DatasetName.NAMECHANGE, dataset_kind=DatasetKind.MASTER)
                state_repo.update_master(DatasetName.NAMECHANGE, bootstrap_complete=True)
                state_repo.finish_success(
                    DatasetName.NAMECHANGE, status=DatasetStatus.CAUGHT_UP
                )
                session.commit()

    def _namechange_window_refresh(self, run_id: str, state) -> None:
        """每 7 天窗口增量：start=上次成功 - 7 天，重叠窗口替换保留更早历史。"""
        window_start = (state.last_success_at or now_beijing()).date() - timedelta(days=7)
        fetched_at = now_beijing()
        # 网络请求在写锁外（design D7/D8）
        batch: ProviderBatch = self.providers.get_name_changes(start_date=window_start)
        with write_coordinator.write():
            with self.session_factory() as session:
                master_repo = HistoryMasterRepository(session)
                rows = master_repo.replace_name_changes_in_window(
                    batch.records, window_start=window_start, source=self.providers.source,
                    run_id=run_id, fetched_at=fetched_at,
                )
                state_repo = HistorySyncStateRepository(session)
                state_repo.ensure(DatasetName.NAMECHANGE, dataset_kind=DatasetKind.MASTER)
                state_repo.finish_success(
                    DatasetName.NAMECHANGE, status=DatasetStatus.CAUGHT_UP
                )
                run_repo = HistorySyncRunDatasetRepository(session)
                if run_repo.get(run_id, DatasetName.NAMECHANGE) is None:
                    run_repo.start(
                        run_id, DatasetName.NAMECHANGE, start_watermark=None,
                        target_trade_date=None, started_at=fetched_at,
                    )
                run_repo.add_counts(run_id, DatasetName.NAMECHANGE, rows=rows, requests=1)
                session.commit()

    # ---- 严格交易日历辅助 ----

    def _strict_open_days(self, end: date) -> list[date]:
        """history_start_date..end 区间升序 open day 列表。

        必须经严格日历路径（§11.1：market='CN' AND source='tushare'，缺数据
        抛 CalendarUnavailableError，禁止 weekday 近似或 date+1 推进）。直接
        查 ``trading_calendar`` 表不够——实时路径写入的旧行 source 为 NULL，
        不能作为历史连续性依据。
        """
        records = self.calendar_provider.get_days(
            CN_MARKET, self.config.history.start_date, end, strict=True
        )
        return [rec.trade_date for rec in records if rec.is_open]

    def _set_state_error(
        self, dataset: DatasetName, *, error_code: str, error: str, status: DatasetStatus
    ) -> None:
        with write_coordinator.write():
            with self.session_factory() as session:
                HistorySyncStateRepository(session).finish_error(
                    dataset, error_code=error_code, error=error, status=status
                )
                session.commit()

    # ---- 日级数据集：个股串行循环（design D7）----

    def _sync_stock_dataset(
        self,
        run_id: str,
        dataset: DatasetName,
        *,
        cancellation_event: threading.Event | None,
    ) -> str:
        """个股模式数据集主循环；返回 SUCCESS / NOOP / CANCELLED。

        流程（design D7）：
          1. 解析 target（AvailabilityPolicy）
          2. 批量补建缺失 stock_sync_state 行（一次写事务）
          3. 取 universe（watermark ASC NULLS FIRST + ts_code ASC 排序）
          4. 逐股：planner 算有效区间 → 无工作 skipped；有工作交 Executor
          5. 单股失败 continue 下一股；系统级异常上抛

        run_dataset 语义（design D10）：
          - 旧水位列（start_watermark/target_trade_date/end_watermark 等）
            冻结置 NULL，新列（processed/success/failed/skipped）承载统计；
          - status: 有工作 → SUCCESS（允许存在个股 failed）；
            无工作 → NOOP。
        """
        now = now_beijing()
        open_days = self._strict_open_days(now.date())
        target = self.availability.latest_expected_trade_date(
            dataset, now=now, strict_open_days=open_days
        )

        # 取主档全量快照（含生命周期）——一次查询，供全数据集复用
        with self.session_factory() as session:
            instruments = HistoryMasterRepository(session).list_cn_stock_instruments()
            lifecycle_map = HistoryMasterRepository(session).get_cn_stock_lifecycle_map()

        # 构造 universe 入口列表（instrument_id, ts_code, list_date, delist_date）
        universe_entries: list[tuple[str, str, date | None, date | None]] = []
        for inst in instruments:
            lc = lifecycle_map.get(inst.instrument_id)
            if lc is None:
                # 主档中没有 cn_stock_basic 记录的证券（理论上不应发生，
                # 防御性跳过）
                continue
            ts_code, list_date, delist_date = lc
            universe_entries.append((inst.instrument_id, ts_code, list_date, delist_date))

        # 批量补建缺失 stock_sync_state 行（design D3/D7：一次写事务）
        self._bulk_ensure_stock_states(
            dataset,
            [(iid, ts_code) for iid, ts_code, _, _ in universe_entries],
        )

        # 取排序后的 universe（落后最久优先：watermark ASC NULLS FIRST + ts_code ASC）
        with self.session_factory() as session:
            stock_states = StockSyncStateRepository(session).universe(dataset)

        # 确保 history_sync_state 行存在（数据集级运营字段用）
        with write_coordinator.write():
            with self.session_factory() as session:
                state_repo = HistorySyncStateRepository(session)
                state_repo.ensure(
                    dataset,
                    dataset_kind=DatasetKind.DAILY_CONTIGUOUS,
                    history_start_date=self.config.history.start_date,
                )
                if target is not None:
                    state_repo.set_expected(dataset, target)
                session.commit()

        # 重置进度
        self.progress.reset()
        self.progress.current_dataset = dataset.value

        if target is None:
            # 目标不可用：整个数据集无事可做（全部 skipped）
            skipped = len(stock_states)
            self.progress.skipped = skipped
            self._flush_skipped_count(run_id, dataset, skipped)
            self._finish_stock_run_dataset(run_id, dataset, status=RunDatasetStatus.NOOP)
            return "NOOP"

        # 启动 run_dataset（旧水位列冻结置 NULL）
        self._start_stock_run_dataset(run_id, dataset, target)

        # 构造 instrument_id -> Instrument 映射，便于按 universe 顺序查找
        inst_by_id = {inst.instrument_id: inst for inst in instruments}

        has_work = False
        any_failed = False

        for stock_state in stock_states:
            inst = inst_by_id.get(stock_state.instrument_id)
            if inst is None:
                # universe 中存在但主档无对应 instrument（理论上不应发生）
                continue
            if cancellation_event is not None and cancellation_event.is_set():
                logger.info(
                    "%s 收到停机信号，已完成进度已保存，本轮不再推进 "
                    "run_id=%s processed=%d succeeded=%d failed=%d skipped=%d",
                    dataset.value, run_id,
                    self.progress.processed, self.progress.succeeded,
                    self.progress.failed, self.progress.skipped,
                )
                # 数据集级状态派生：有失败 → LAGGING，否则 → CAUGHT_UP
                self._flush_skipped_count(run_id, dataset, self.progress.skipped)
                self._refresh_dataset_state(dataset, has_work=has_work, any_failed=any_failed)
                self._finish_stock_run_dataset(run_id, dataset, status=RunDatasetStatus.NOOP)
                return "CANCELLED"

            lc = lifecycle_map.get(inst.instrument_id)
            if lc is None:
                continue
            ts_code, list_date, delist_date = lc

            watermark = stock_state.watermark_date

            # 计算有效区间
            sync_range = HistorySyncPlanner.stock_effective_range(
                watermark=watermark,
                target=target,
                history_start_date=self.config.history.start_date,
                list_date=list_date,
                delist_date=delist_date,
                open_days=open_days,
            )

            if sync_range.is_empty:
                # 无工作：skipped 计数（不建 task、不发请求）
                self.progress.record_skip()
                continue

            has_work = True
            self.progress.current_ts_code = ts_code

            try:
                outcome = self.stock_executor.execute(
                    run_id=run_id,
                    dataset=dataset,
                    instrument=inst,
                    ts_code=ts_code,
                    start_date=sync_range.start_date,
                    end_date=sync_range.end_date,
                    list_date=list_date,
                    delist_date=delist_date,
                    cancellation_event=cancellation_event,
                )
                self.progress.record_outcome(outcome)
                if outcome.status == "failed":
                    any_failed = True
            except Exception as exc:
                # 未预期异常（如数据库崩溃、框架级错误）：上抛终止数据集
                # 区分 SQLAlchemyError 等系统级异常
                logger.exception(
                    "个股同步出现系统级异常，终止该数据集 run_id=%s dataset=%s ts_code=%s",
                    run_id, dataset.value, ts_code,
                )
                raise

        if not has_work:
            # 全部股票无工作 → NOOP
            self._flush_skipped_count(run_id, dataset, self.progress.skipped)
            self._refresh_dataset_state(dataset, has_work=False, any_failed=False)
            self._finish_stock_run_dataset(run_id, dataset, status=RunDatasetStatus.NOOP)
            return "NOOP"

        # 有工作 → SUCCESS（允许存在个股 failed，Run 语义 D10）
        self._flush_skipped_count(run_id, dataset, self.progress.skipped)
        self._refresh_dataset_state(dataset, has_work=True, any_failed=any_failed)
        self._finish_stock_run_dataset(
            run_id, dataset,
            status=RunDatasetStatus.SUCCESS,
            has_failures=any_failed,
        )
        return "SUCCESS"

    def _bulk_ensure_stock_states(
        self,
        dataset: DatasetName,
        entries: list[tuple[str, str | None]],
    ) -> None:
        """批量补建缺失 stock_sync_state 行（design D7：一次写事务）。"""
        with write_coordinator.write():
            with self.session_factory() as session:
                StockSyncStateRepository(session).bulk_ensure_missing(dataset, entries)
                session.commit()

    def _start_stock_run_dataset(
        self, run_id: str, dataset: DatasetName, target: date | None
    ) -> None:
        """启动 run_dataset（个股模式：旧水位列冻结置 NULL）。"""
        with write_coordinator.write():
            with self.session_factory() as session:
                HistorySyncRunDatasetRepository(session).start(
                    run_id, dataset,
                    start_watermark=None,  # 冻结兼容列
                    target_trade_date=None,  # 冻结兼容列
                    started_at=now_beijing(),
                )
                session.commit()

    def _finish_stock_run_dataset(
        self,
        run_id: str,
        dataset: DatasetName,
        *,
        status: RunDatasetStatus,
        has_failures: bool = False,
    ) -> None:
        """结束 run_dataset（个股模式：旧水位列冻结，last_error 取自数据集级 state）。"""
        last_error_code = last_error = None
        if status == RunDatasetStatus.FAILED:
            with self.session_factory() as session:
                state = HistorySyncStateRepository(session).get(dataset)
                if state is not None:
                    last_error_code = state.last_error_code
                    last_error = state.last_error

        with write_coordinator.write():
            with self.session_factory() as session:
                run_repo = HistorySyncRunDatasetRepository(session)
                if run_repo.get(run_id, dataset) is None:
                    run_repo.start(
                        run_id, dataset, start_watermark=None,
                        target_trade_date=None, started_at=now_beijing(),
                    )
                run_repo.finish(
                    run_id, dataset, status=status, finished_at=now_beijing(),
                    end_watermark=None,  # 冻结兼容列
                    failed_trade_date=None,  # 冻结兼容列
                    last_error_code=last_error_code,
                    last_error=last_error,
                )
                session.commit()

    def _flush_skipped_count(self, run_id: str, dataset: DatasetName, count: int) -> None:
        """数据集结束时一次性写入 skipped 计数（避免每股一个小事务）。"""
        if count <= 0:
            return
        with write_coordinator.write():
            with self.session_factory() as session:
                run_repo = HistorySyncRunDatasetRepository(session)
                if run_repo.get(run_id, dataset) is not None:
                    run_repo.add_counts(run_id, dataset, skipped=count)
                session.commit()

    def _refresh_dataset_state(
        self, dataset: DatasetName, *, has_work: bool, any_failed: bool
    ) -> None:
        """数据集段结束时刷新 history_sync_state 运营字段。

        个股模式下状态派生（design D10 / spec"同步执行记录"）：
          - 有落后股票 → LAGGING
          - 全部追平 → CAUGHT_UP
          - 本轮系统级失败 → FAILED（由调用方 _record_unexpected_failure 处理）
        last_success_at / last_error_code / last_error 在段结束时刷新。
        """
        with write_coordinator.write():
            with self.session_factory() as session:
                state_repo = HistorySyncStateRepository(session)
                state = state_repo.get(dataset)
                if state is None:
                    return
                if any_failed:
                    status = DatasetStatus.LAGGING
                elif has_work:
                    # 本轮有工作且全部成功：是否追平取决于水位 vs target
                    # 简化：有工作 + 无失败 → CAUGHT_UP（严格来说应再检查，
                    # 但个股模式下本轮处理过的股票水位已推进到 eff_end，
                    # 若全部成功且 target 是最新，则整体 CAUGHT_UP）
                    status = DatasetStatus.CAUGHT_UP
                else:
                    # 无工作：保持原状态或置 CAUGHT_UP
                    status = DatasetStatus.CAUGHT_UP

                if any_failed:
                    # 有个股失败：数据集级状态置 LAGGING（不设 last_error——
                    # 个股失败详情在 stock_sync_state，数据集级只反映整体
                    # 落后状态）。last_success_at 不刷新（本轮有失败）。
                    state.status = status.value
                    state.updated_at = now_beijing()
                else:
                    state_repo.finish_success(dataset, status=status)
                session.commit()

    # ---- run 收尾（design D10：SUCCESS/FAILED/INTERRUPTED/NOOP）----

    def _finalize_run(
        self,
        run_id: str,
        *,
        dataset_outcomes: dict[DatasetName, str],
        master_ok: bool,
        aborted: bool = False,
    ) -> None:
        if not master_ok:
            status = RunStatus.FAILED
            error_summary = "主档硬前置失败（trade_cal/stock_basic），本轮未推进日级数据集"
        elif aborted and not dataset_outcomes:
            # 对账等前置阶段失败后中止：没有数据集结果≠无事可做，不得记 NOOP
            status = RunStatus.FAILED
            error_summary = "前置阶段失败，本轮未推进日级数据集"
        else:
            outcomes = set(dataset_outcomes.values())
            if "CANCELLED" in outcomes:
                # §49：收到停机信号——当前事务已正常完成，不开始下一日
                status = RunStatus.INTERRUPTED
                error_summary = "收到停机信号，本轮提前结束（已完成进度已保存）"
            elif outcomes <= {"NOOP"}:
                status = RunStatus.NOOP
                error_summary = None
            elif "FAILED" in outcomes:
                # D10：FAILED 仅表示系统级错误。只要有数据集系统级失败，
                # Run 就标 FAILED（个股 task failed 不影响数据集级 outcome，
                # 数据集级 FAILED 仅产生于系统级异常路径）。
                # PARTIAL 枚举保留但不再产生（D10 明确）。
                status = RunStatus.FAILED
                error_summary = self._error_summary(dataset_outcomes)
            else:
                # 全为 SUCCESS/NOOP：SUCCESS（允许存在个股 task failed，D10）
                status = RunStatus.SUCCESS
                error_summary = None

        self._finalize_master_run_datasets(run_id, status=status)

        with write_coordinator.write():
            with self.session_factory() as session:
                HistorySyncRunRepository(session).finish(
                    run_id, status=status, finished_at=now_beijing(), error_summary=error_summary
                )
                session.commit()
        logger.info(
            "同步任务结束 run_id=%s status=%s outcomes=%s",
            run_id, status.value,
            {ds.value: outcome for ds, outcome in dataset_outcomes.items()},
        )

    def _finalize_master_run_datasets(self, run_id: str, *, status: RunStatus) -> None:
        """主档数据集补写 run_dataset 行（§20：管理员"最近执行记录"按 run×
        dataset 读取）。主档不进水位模型，只记状态与最近错误，供页面展示。"""
        master_datasets = (
            DatasetName.TRADE_CAL,
            DatasetName.STOCK_BASIC,
            DatasetName.STOCK_COMPANY,
            DatasetName.NAMECHANGE,
        )
        finished_at = now_beijing()
        with write_coordinator.write():
            with self.session_factory() as session:
                run_repo = HistorySyncRunDatasetRepository(session)
                state_repo = HistorySyncStateRepository(session)
                for dataset in master_datasets:
                    state = state_repo.get(dataset)
                    if state is None:
                        # 本轮完全未触及该主档（如 trade_cal 硬前置失败后
                        # 提前中止）：无 state 即未执行，不补行（§20 只要求
                        # 记录"执行过"的数据集）。
                        continue
                    row = run_repo.get(run_id, dataset)
                    if row is None:
                        run_repo.start(
                            run_id, dataset, start_watermark=None,
                            target_trade_date=None, started_at=finished_at,
                        )
                    failed = state.status == DatasetStatus.FAILED.value
                    run_repo.finish(
                        run_id, dataset,
                        status=(
                            RunDatasetStatus.FAILED if failed
                            else RunDatasetStatus.SUCCESS
                        ),
                        finished_at=finished_at,
                        last_error_code=state.last_error_code if failed else None,
                        last_error=state.last_error if failed else None,
                    )
                session.commit()

    def _error_summary(self, dataset_outcomes: dict[DatasetName, str]) -> str:
        failed = [str(ds) for ds, outcome in dataset_outcomes.items() if outcome == "FAILED"]
        return f"失败数据集: {', '.join(failed)}" if failed else "部分数据集未完成"


# 底部 import：避免循环依赖（timedelta 仅 _namechange_window_refresh 使用）
from datetime import timedelta  # noqa: E402
