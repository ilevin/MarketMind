"""历史同步统一编排（a-share-historical-data，design.md D17~D18，
spec.md"统一入口与触发方式"/"任务互斥"/"进程中断与恢复"/"单日原子提交与
幂等"/"失败日期绝不跳过"/"主档前置与刷新周期"/"水位一致性对账"/
"空结果与等待数据源"）。

``HistorySyncService.run`` 为唯一业务入口：

    recover_stale_runs -> ensure_master_prerequisites（trade_cal/stock_basic
    硬前置；company/namechange 非阻塞） -> reconcile_daily_watermarks ->
    四个日级数据集顺序执行（互不阻塞） -> finalize_run

Job 层（进程级 single-flight、cancellation Event 构造、asyncio.to_thread）
不在本文件职责内——本 Service 只接受可选的 ``threading.Event`` 并在检查点
读取，不管理其生命周期。
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from datetime import date, timedelta
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
    HistoryDayStatusRepository,
    HistorySyncRunDatasetRepository,
    HistorySyncRunRepository,
    HistorySyncStateRepository,
)
from app.repositories.trading_calendar import TradingCalendarRepository
from app.services.history.availability import AvailabilityPolicy
from app.services.history.planner import HistorySyncPlanner
from app.services.history.retry import RetryPolicy, is_config_error
from app.services.history.validation import (
    HistoryValidationError,
    TruncationRiskError,
    validate_batch,
)
from app.services.market_session_service import now_beijing

logger = logging.getLogger(__name__)

# 四个日级数据集的处理顺序（互不阻塞：一个 FAILED 不影响后续数据集推进）
DAY_LEVEL_DATASETS: tuple[DatasetName, ...] = (
    DatasetName.DAILY,
    DatasetName.ADJ_FACTOR,
    DatasetName.DAILY_BASIC,
    DatasetName.MONEYFLOW,
)

# 每数据集 Provider 方法名（主路径 / 截断 fallback），§33 截断风险处理用
_FETCH_METHODS: dict[DatasetName, tuple[str, str]] = {
    DatasetName.DAILY: ("get_daily", "get_daily_for_instruments"),
    DatasetName.ADJ_FACTOR: ("get_adj_factors", "get_adj_factors"),
    DatasetName.DAILY_BASIC: ("get_daily_basic", "get_daily_basic_for_instruments"),
    DatasetName.MONEYFLOW: ("get_moneyflow", "get_moneyflow_for_instruments"),
}

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
    HistoryValidationError,
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

            try:
                self.reconcile_daily_watermarks()
            except SYNC_ERRORS as exc:
                # 日历不可用等：对账是"保守回退"的前置，做不了就不能推进
                # （宁可本轮不推进，也不在"水位可能不一致"的前提下写数据）。
                logger.error(
                    "水位对账失败，本轮不推进日级数据集 run_id=%s error_code=%s: %s",
                    run_id, _error_code_of(exc), _safe_error_text(exc),
                )
                aborted = True
                return run_id

            for dataset in DAY_LEVEL_DATASETS:
                if cancellation_event is not None and cancellation_event.is_set():
                    outcomes[dataset] = "CANCELLED"
                    continue
                try:
                    outcomes[dataset] = self._sync_day_level_dataset(
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

    # ---- 启动恢复（§19）----

    def recover_stale_runs(self, *, exclude_run_id: str | None = None) -> None:
        """遗留 RUNNING run 标 INTERRUPTED；SYNCING/RETRYING/CHECKING 的
        state 恢复为 LAGGING/CAUGHT_UP（依据水位，不依据"曾经 RUNNING"猜测
        某日已完成）。"""
        now = now_beijing()
        with write_coordinator.write():
            with self.session_factory() as session:
                run_repo = HistorySyncRunRepository(session)
                stale_runs = [
                    r for r in run_repo.find_stale_running() if r.run_id != exclude_run_id
                ]
                for stale in stale_runs:
                    run_repo.mark_interrupted(stale.run_id, finished_at=now)
                    logger.warning(
                        "恢复遗留 RUNNING 任务为 INTERRUPTED: run_id=%s", stale.run_id
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

    def _reload_instruments(self, instruments: list | None) -> tuple[list | None, set | None]:
        """刷新后重读主档并就地更新调用方列表（§35.1 的"重新映射"）。

        就地更新而非返回新列表：同一数据集的后续交易日复用刷新后的主档，
        否则每个未推进的交易日都会再刷新一次主档（§35.1 只要求"刷新一次"）。
        """
        with self.session_factory() as session:
            refreshed = HistoryMasterRepository(session).list_cn_stock_instruments()
        if instruments is not None:
            instruments[:] = refreshed
        known_ids = (
            {inst.instrument_id for inst in instruments} if instruments else None
        )
        return instruments, known_ids

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

    # ---- 水位一致性对账（§25）----

    def reconcile_daily_watermarks(self) -> None:
        """每次任务开始对四数据集轻量 reconcile：发现不一致时保守回退水位到
        第一个缺失 COMPLETE 日的前一交易日（下轮据此重同步）。"""
        today = now_beijing().date()
        open_days = self._strict_open_days(today)
        with write_coordinator.write():
            with self.session_factory() as session:
                state_repo = HistorySyncStateRepository(session)
                day_repo = HistoryDayStatusRepository(session)
                fact_repo = HistoryFactRepository(session)
                for dataset in DAY_LEVEL_DATASETS:
                    state = state_repo.ensure(
                        dataset,
                        dataset_kind=DatasetKind.DAILY_CONTIGUOUS,
                        history_start_date=self.config.history.start_date,
                    )
                    watermark = state.latest_complete_trade_date
                    if watermark is None:
                        continue
                    expected_days = [d for d in open_days if d <= watermark]
                    completed = day_repo.completed_dates(
                        dataset, start=self.config.history.start_date, end=watermark
                    )
                    reconciled = HistorySyncPlanner.reconcile_watermark(
                        watermark=watermark,
                        expected_days=expected_days,
                        completed_dates=completed,
                    )
                    if reconciled != watermark:
                        logger.warning(
                            "水位对账发现缺口，回退 %s 水位: %s -> %s",
                            dataset, watermark, reconciled,
                        )
                        state.latest_complete_trade_date = reconciled
                        # 不回写 data_max_date：该字段是"事实表最大交易日"，
                        # 事实行仍在，回退水位不代表数据被删（§45）。
                    # 事实表 MAX 与水位矛盾只用于发现异常、告警，绝不据此
                    # 推进水位（spec"水位一致性对账"）。
                    max_fact = fact_repo.max_trade_date(dataset)
                    if max_fact is not None and max_fact > watermark:
                        logger.warning(
                            "对账发现事实表日期晚于水位（仅告警，不推进水位） "
                            "dataset=%s watermark=%s max_fact_date=%s",
                            dataset.value, watermark, max_fact,
                        )
                    elif max_fact is None or max_fact < watermark:
                        # 反向矛盾（§25 第 4 项）：水位声称已完成的日期在事实表
                        # 中不存在。正常路径不可能出现——推水位与写事实在同一
                        # 事务（D5/D6），且 COMPLETE 日必 >=1 行（空结果为
                        # EMPTY_RESULT/WAITING_SOURCE，均不推进水位）。出现即
                        # 说明事实行被越过应用删除，ledger 却仍称完整；仅告警，
                        # 不据此回退水位（回退需 ledger 缺口证据，见上）。
                        logger.warning(
                            "对账发现水位晚于事实表（仅告警，不回退水位） "
                            "dataset=%s watermark=%s max_fact_date=%s",
                            dataset.value, watermark, max_fact,
                        )
                session.commit()

    def reconcile_dataset(self, dataset: DatasetName | str) -> date | None:
        """内部诊断能力（§25）：单数据集 reconcile，返回回退后水位（不落库）。"""
        today = now_beijing().date()
        open_days = self._strict_open_days(today)
        with self.session_factory() as session:
            state = HistorySyncStateRepository(session).get(dataset)
            watermark = state.latest_complete_trade_date if state else None
            if watermark is None:
                return None
            expected_days = [d for d in open_days if d <= watermark]
            completed = HistoryDayStatusRepository(session).completed_dates(
                dataset, start=self.config.history.start_date, end=watermark
            )
        return HistorySyncPlanner.reconcile_watermark(
            watermark=watermark, expected_days=expected_days, completed_dates=completed
        )

    # ---- 日级数据集主循环（§21~§29）----

    def _sync_day_level_dataset(
        self,
        run_id: str,
        dataset: DatasetName,
        *,
        cancellation_event: threading.Event | None,
    ) -> str:
        """返回该数据集本轮结果：SUCCESS / FAILED / NOOP。"""
        now = now_beijing()
        open_days = self._strict_open_days(now.date())
        target = self.availability.latest_expected_trade_date(
            dataset, now=now, strict_open_days=open_days
        )

        # ensure 必须与本次提交一起落库：否则新行随会话关闭回滚，后续
        # complete_day/begin_attempt 会因 state 行不存在而失败（首次同步路径）。
        with write_coordinator.write():
            with self.session_factory() as session:
                state_repo = HistorySyncStateRepository(session)
                state = state_repo.ensure(
                    dataset,
                    dataset_kind=DatasetKind.DAILY_CONTIGUOUS,
                    history_start_date=self.config.history.start_date,
                )
                watermark = state.latest_complete_trade_date
                pending = (
                    []
                    if target is None
                    else HistorySyncPlanner.pending_dates(
                        watermark=watermark,
                        target=target,
                        history_start_date=self.config.history.start_date,
                        open_days=open_days,
                    )
                )
                if target is not None:
                    state_repo.set_expected(dataset, target)
                    if pending:
                        state_repo.mark_started(dataset, status=DatasetStatus.SYNCING)
                    else:
                        # 无事可做即已追平：不得停在非终态 CHECKING（否则页面
                        # 长期显示"检查中"，且只能靠下次启动恢复才转正）。
                        state_repo.finish_success(
                            dataset, status=DatasetStatus.CAUGHT_UP
                        )
                session.commit()

        if target is None or not pending:
            self._finish_run_dataset(
                run_id, dataset, status=RunDatasetStatus.NOOP,
                start_watermark=watermark, target=target, end_watermark=watermark,
            )
            return "NOOP"

        self._start_run_dataset(run_id, dataset, start_watermark=watermark, target=target)

        with self.session_factory() as session:
            instruments = HistoryMasterRepository(session).list_cn_stock_instruments()

        for trade_date in pending:
            if cancellation_event is not None and cancellation_event.is_set():
                logger.info(
                    "%s 收到停机信号，已完成的交易日进度已保存，本轮不再推进 "
                    "run_id=%s watermark=%s",
                    dataset.value, run_id, self._current_watermark(dataset),
                )
                self._finish_run_dataset(
                    run_id, dataset, status=RunDatasetStatus.NOOP,
                    start_watermark=watermark, target=target,
                    end_watermark=self._current_watermark(dataset),
                )
                return "CANCELLED"

            outcome = self._sync_single_day(
                run_id, dataset, trade_date, instruments, cancellation_event=cancellation_event
            )
            if outcome == "WAITING_SOURCE":
                # 非错误：本轮不推进，下次任务从该日继续（§34.2），不算数据集失败
                self._finish_run_dataset(
                    run_id, dataset, status=RunDatasetStatus.NOOP,
                    start_watermark=watermark, target=target,
                    end_watermark=self._current_watermark(dataset),
                )
                return "NOOP"
            if outcome == "CANCELLED":
                self._finish_run_dataset(
                    run_id, dataset, status=RunDatasetStatus.NOOP,
                    start_watermark=watermark, target=target,
                    end_watermark=self._current_watermark(dataset),
                )
                return "CANCELLED"
            if outcome != "SUCCESS":
                self._finish_run_dataset(
                    run_id, dataset, status=RunDatasetStatus.FAILED,
                    start_watermark=watermark, target=target,
                    end_watermark=self._current_watermark(dataset),
                    failed_trade_date=trade_date,
                )
                return "FAILED"

        with write_coordinator.write():
            with self.session_factory() as session:
                HistorySyncStateRepository(session).finish_success(
                    dataset, status=DatasetStatus.CAUGHT_UP
                )
                session.commit()

        self._finish_run_dataset(
            run_id, dataset, status=RunDatasetStatus.SUCCESS,
            start_watermark=watermark, target=target,
            end_watermark=self._current_watermark(dataset),
        )
        return "SUCCESS"

    def _current_watermark(self, dataset: DatasetName) -> date | None:
        with self.session_factory() as session:
            state = HistorySyncStateRepository(session).get(dataset)
            return state.latest_complete_trade_date if state else None

    def _start_run_dataset(
        self, run_id: str, dataset: DatasetName, *, start_watermark: date | None, target: date
    ) -> None:
        with write_coordinator.write():
            with self.session_factory() as session:
                HistorySyncRunDatasetRepository(session).start(
                    run_id, dataset, start_watermark=start_watermark,
                    target_trade_date=target, started_at=now_beijing(),
                )
                session.commit()

    def _finish_run_dataset(
        self,
        run_id: str,
        dataset: DatasetName,
        *,
        status: RunDatasetStatus,
        start_watermark: date | None,
        target: date,
        end_watermark: date | None,
        failed_trade_date: date | None = None,
    ) -> None:
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
                        run_id, dataset, start_watermark=start_watermark,
                        target_trade_date=target, started_at=now_beijing(),
                    )
                run_repo.finish(
                    run_id, dataset, status=status, finished_at=now_beijing(),
                    end_watermark=end_watermark, failed_trade_date=failed_trade_date,
                    last_error_code=last_error_code, last_error=last_error,
                )
                session.commit()

    # ---- 单日：抓取 + 校验 + 重试编排 + 原子提交（§22/§29/§34）----

    def _sync_single_day(
        self,
        run_id: str,
        dataset: DatasetName,
        trade_date: date,
        instruments: list | None,
        *,
        cancellation_event: threading.Event | None,
    ) -> str:
        """返回 SUCCESS / FAILED / WAITING_SOURCE / CANCELLED。

        单 dataset×单交易日最多 ``max_attempts`` 次；退避见 RetryPolicy。
        配置类错误快速失败（§29.3）。空结果为 0 行时：接近发布时间按
        WAITING_SOURCE（本轮不推进、不算失败，§34.2），否则按 EMPTY_RESULT
        进入重试直至失败（§34.1）。停机信号在每次重试 sleep 前后检查
        （§49），命中则返回 CANCELLED（当前事务已完成）。
        """
        known_ids = (
            {inst.instrument_id for inst in instruments} if instruments else None
        )
        remap_attempted = False
        max_attempts = self.retry_policy.max_attempts
        for attempt in range(1, max_attempts + 1):
            self._mark_attempt_started(run_id, dataset, trade_date, attempt)
            started = time.monotonic()
            try:
                batch, request_count = self._fetch_day(dataset, trade_date, instruments)
            except SYNC_ERRORS as exc:
                # Provider 边界的 UNKNOWN_INSTRUMENT（normalize 阶段 ts_code
                # 无法映射主档）与校验层的是同一个错误码，必须走同一条 §35.1
                # 恢复路径；否则它会作为"配置类错误"在第 1 次尝试就终态失败。
                if (
                    _error_code_of(exc) == "UNKNOWN_INSTRUMENT"
                    and not remap_attempted
                ):
                    remap_attempted = True
                    logger.warning(
                        "出现未知证券（请求阶段），刷新 stock_basic 后重试 "
                        "dataset=%s trade_date=%s run_id=%s: %s",
                        dataset.value, trade_date, run_id, _safe_error_text(exc),
                    )
                    if self._refresh_stock_basic(run_id, context="未知证券恢复"):
                        instruments, known_ids = self._reload_instruments(
                            instruments
                        )
                        if attempt < max_attempts:
                            continue
                        # 已是最后一次尝试：主档刷新成功但已无余量重试，必须
                        # 落到下面的终态失败分支。否则循环自然结束，state 停在
                        # RETRYING/SYNCING 且无错误码（§35.1 要求记录未知证券）。
                request_count = 1
                outcome = self._handle_attempt_failure(
                    run_id, dataset, trade_date, attempt,
                    error_code=_error_code_of(exc), error=_safe_error_text(exc),
                    requests=request_count,
                    elapsed_ms=_elapsed_ms(started),
                    cancellation_event=cancellation_event,
                )
                if outcome is not None:
                    return outcome
                continue

            try:
                is_waiting = (
                    len(batch.records) == 0
                    and self._is_near_publish_time(dataset, trade_date)
                )
                validate_batch(
                    dataset, batch, trade_date=trade_date,
                    known_instrument_ids=known_ids,
                    allow_empty=is_waiting,
                )
            except HistoryValidationError as exc:
                if (
                    exc.error_code == "UNKNOWN_INSTRUMENT"
                    and not remap_attempted
                ):
                    # §35.1：先刷新一次 stock_basic 并重新映射，仍未知才判定
                    # 失败（该交易日不推进水位）。只做一次，避免与重试叠加。
                    remap_attempted = True
                    logger.warning(
                        "出现未知证券，刷新 stock_basic 后重试 dataset=%s "
                        "trade_date=%s run_id=%s: %s",
                        dataset.value, trade_date, run_id, _safe_error_text(exc),
                    )
                    if self._refresh_stock_basic(run_id, context="未知证券恢复"):
                        instruments, known_ids = self._reload_instruments(
                            instruments
                        )
                        # 同请求阶段分支：最后一次尝试不再 continue，落到终态失败
                        if attempt < max_attempts:
                            continue
                outcome = self._handle_attempt_failure(
                    run_id, dataset, trade_date, attempt,
                    error_code=_error_code_of(exc), error=_safe_error_text(exc),
                    requests=request_count,
                    elapsed_ms=_elapsed_ms(started),
                    cancellation_event=cancellation_event,
                )
                if outcome is not None:
                    return outcome
                continue

            elapsed_ms = _elapsed_ms(started)
            if is_waiting:
                logger.info(
                    "数据集等待数据源 dataset=%s trade_date=%s attempt=%d "
                    "row_count=0 elapsed_ms=%d run_id=%s",
                    dataset.value, trade_date, attempt, elapsed_ms, run_id,
                )
                self._set_state_error(
                    dataset, error_code="WAITING_SOURCE",
                    error=f"{trade_date} 接近发布时间空结果，等待数据源",
                    status=DatasetStatus.WAITING_SOURCE,
                )
                return "WAITING_SOURCE"

            try:
                self._commit_single_day(
                    run_id, dataset, trade_date, batch,
                    attempt=attempt, requests=request_count,
                )
            except SQLAlchemyError as exc:
                # §8：DB 事务失败可有限重试，但以"单日 max_attempts 次"为上层
                # 边界（不与 WriteCoordinator 自带重试嵌套成无界）。事务已整体
                # 回滚，旧事实/水位/ledger 均未推进，下一轮从头重放。
                outcome = self._handle_attempt_failure(
                    run_id, dataset, trade_date, attempt,
                    error_code="DATABASE_ERROR", error=_safe_error_text(exc),
                    requests=0,
                    elapsed_ms=_elapsed_ms(started),
                    cancellation_event=cancellation_event,
                )
                if outcome is not None:
                    return outcome
                continue
            logger.info(
                "数据集单日完成 dataset=%s trade_date=%s attempt=%d row_count=%d "
                "elapsed_ms=%d run_id=%s",
                dataset.value, trade_date, attempt, len(batch.records),
                _elapsed_ms(started), run_id,
            )
            return "SUCCESS"

        return "FAILED"

    def _mark_attempt_started(
        self, run_id: str, dataset: DatasetName, trade_date: date, attempt: int
    ) -> None:
        """记录当前日与尝试次数（§29）；attempt>1 时状态为 RETRYING。

        ``retry_count`` 计"首次之外的尝试次数"，在第 2 次及以后的尝试**开始时**
        累加：这样第 3 次才成功的那次重试同样被计入（若只在失败时累加，成功
        的那次重试会漏计）。请求数仍在各次尝试结束时按实际发出次数累加。
        """
        with write_coordinator.write():
            with self.session_factory() as session:
                HistorySyncStateRepository(session).begin_attempt(
                    dataset, trade_date, attempt,
                    status=DatasetStatus.RETRYING if attempt > 1 else DatasetStatus.SYNCING,
                )
                if attempt > 1:
                    run_repo = HistorySyncRunDatasetRepository(session)
                    if run_repo.get(run_id, dataset) is not None:
                        run_repo.add_counts(run_id, dataset, retries=1)
                session.commit()

    def _handle_attempt_failure(
        self,
        run_id: str,
        dataset: DatasetName,
        trade_date: date,
        attempt: int,
        *,
        error_code: str,
        error: str,
        requests: int,
        elapsed_ms: int,
        cancellation_event: threading.Event | None,
    ) -> str | None:
        """一次尝试失败的统一处理；返回终态（"FAILED"/"CANCELLED"）或 None
        表示可继续重试（调用方 sleep 后进入下一轮）。"""
        self._record_attempt_counts(run_id, dataset, requests=requests)
        logger.warning(
            "数据集单日尝试失败 dataset=%s trade_date=%s attempt=%d "
            "error_code=%s elapsed_ms=%d run_id=%s: %s",
            dataset.value, trade_date, attempt, error_code, elapsed_ms, run_id, error,
        )
        is_terminal = is_config_error(error_code) or attempt >= self.retry_policy.max_attempts
        if is_terminal:
            logger.error(
                "数据集单日判定失败 dataset=%s trade_date=%s attempt=%d "
                "error_code=%s reason=%s run_id=%s（停止该数据集本轮推进，"
                "水位保持在上一成功交易日）",
                dataset.value, trade_date, attempt, error_code,
                "配置类错误" if is_config_error(error_code) else "已达最大重试次数",
                run_id,
            )
            self._set_state_error(
                dataset, error_code=error_code, error=error, status=DatasetStatus.FAILED,
            )
            return "FAILED"

        if cancellation_event is not None and cancellation_event.is_set():
            return "CANCELLED"
        self.retry_policy.sleep_before_retry(attempt)
        if cancellation_event is not None and cancellation_event.is_set():
            return "CANCELLED"
        return None

    def _set_state_error(
        self, dataset: DatasetName, *, error_code: str, error: str, status: DatasetStatus
    ) -> None:
        with write_coordinator.write():
            with self.session_factory() as session:
                HistorySyncStateRepository(session).finish_error(
                    dataset, error_code=error_code, error=error, status=status
                )
                session.commit()

    def _record_attempt_counts(
        self, run_id: str, dataset: DatasetName, *, requests: int, retries: int = 0
    ) -> None:
        """累计请求/重试次数（§20 的 request_count / retry_count）。

        数据集未在本轮开始时已 start（或本轮 run_dataset 尚未建立）时忽略。
        """
        with write_coordinator.write():
            with self.session_factory() as session:
                run_repo = HistorySyncRunDatasetRepository(session)
                if run_repo.get(run_id, dataset) is not None:
                    run_repo.add_counts(run_id, dataset, requests=requests, retries=retries)
                session.commit()

    def _is_near_publish_time(self, dataset: DatasetName, trade_date: date) -> bool:
        """接近发布时间的判定（§34.1/§34.2）：仅当该交易日就是当前北京日期
        时才可能是"数据源尚未生成"；明显早于当前日期的空结果一律按
        EMPTY_RESULT 处理。"""
        return trade_date == now_beijing().date()

    def _fetch_day(
        self, dataset: DatasetName, trade_date: date, instruments: list
    ) -> tuple[ProviderBatch, int]:
        """主路径请求；命中截断风险时走 fallback（§33.2）。

        返回 ``(batch, request_count)``；fallback 会额外计一次请求。
        """
        primary_method, fallback_method = _FETCH_METHODS[dataset]
        batch: ProviderBatch = getattr(self.providers, primary_method)(
            trade_date, instruments
        )
        if not batch.truncation_risk:
            return batch, 1
        logger.warning(
            "数据集命中截断风险，回退 fallback 请求 dataset=%s trade_date=%s",
            dataset.value, trade_date,
        )
        if dataset is DatasetName.DAILY_BASIC:
            fallback_batch = self._daily_basic_fallback(trade_date, instruments, batch)
        else:
            fallback_batch = getattr(self.providers, fallback_method)(
                trade_date, instruments
            )
        if fallback_batch.truncation_risk:
            raise TruncationRiskError(
                f"{dataset.value} {trade_date} 截断风险，fallback 请求仍不完整"
            )
        return fallback_batch, 2

    def _daily_basic_fallback(
        self, trade_date: date, instruments: list, primary: ProviderBatch
    ) -> ProviderBatch:
        """daily_basic 截断补齐：只逐只查询 ``候选集 - 已返回代码``（§33.2）。

        daily_basic 不支持多代码参数（逗号分隔静默返回空，已在线验证），故
        不复用其他接口的 multi-code fallback。候选集按当日上市/退市状态从
        ``cn_stock_basic`` 生成；每个缺失证券都必须得到"有记录"或"明确空
        结果"——任一请求异常直接向上抛（不吞、不跳过），由重试编排判定该
        交易日不 COMPLETE、水位不推进。

        候选集与主档 instruments 不一致（如主档尚缺该证券）会让
        Provider 抛 UNKNOWN_INSTRUMENT，同样走"该日不 COMPLETE"。
        """
        with self.session_factory() as session:
            candidates = HistoryMasterRepository(session).list_ts_codes_tradable_on(
                trade_date
            )
        returned = {record.ts_code for record in primary.records}
        missing = sorted(set(candidates) - returned)
        if not missing:
            logger.info(
                "daily_basic 截断但候选集已全部返回，无需补齐 trade_date=%s candidates=%d",
                trade_date, len(candidates),
            )
        else:
            logger.warning(
                "daily_basic 截断补齐：逐只查询缺失证券 trade_date=%s 候选=%d 已返回=%d 缺失=%d",
                trade_date, len(candidates), len(returned), len(missing),
            )
        supplemental: ProviderBatch = self.providers.get_daily_basic_for_instruments(
            trade_date, instruments, missing_ts_codes=missing
        )
        merged = list(primary.records) + list(supplemental.records)
        # 复检：单证券单日至多一行，逐只查询不存在再被上限截断；候选集内每个
        # 缺失证券都已得到"有记录"或"明确空结果"（空结果同样合法——停牌等
        # 自然缺失），因此合并结果不再是"可能被截断"的批次。
        return ProviderBatch(
            records=merged,
            source=primary.source,
            raw_row_count=primary.raw_row_count + supplemental.raw_row_count,
            truncation_risk=False,
        )

    def _commit_single_day(
        self,
        run_id: str,
        dataset: DatasetName,
        trade_date: date,
        batch: ProviderBatch,
        *,
        attempt: int,
        requests: int = 1,
    ) -> None:
        """单日原子提交（§22）：old_count -> DELETE -> INSERT -> day_status ->
        state（水位/record_count/min-max）-> run_dataset，单事务，失败整体
        回滚。请求与校验已在锁外完成。

        ``record_count`` 增量口径为 ``new_count - old_count``（§22 步骤 10、
        §45）：整日替换语义下重复运行同一日期不得让计数翻倍。
        """
        fetched_at = now_beijing()
        with write_coordinator.write():
            with self.session_factory() as session:
                fact_repo = HistoryFactRepository(session)
                old_count = fact_repo.count_for_date(dataset, trade_date)
                fact_repo.delete_for_date(dataset, trade_date)
                new_count = fact_repo.insert_records(
                    dataset, batch.records, source=self.providers.source, fetched_at=fetched_at,
                )

                HistoryDayStatusRepository(session).upsert_complete(
                    dataset, trade_date, row_count=new_count, run_id=run_id,
                    fetched_at=fetched_at,
                )

                state_repo = HistorySyncStateRepository(session)
                state_repo.complete_day(
                    dataset, trade_date, rows_delta=new_count - old_count,
                    status=DatasetStatus.SYNCING,
                )

                run_repo = HistorySyncRunDatasetRepository(session)
                if run_repo.get(run_id, dataset) is not None:
                    run_repo.add_counts(
                        run_id, dataset, dates=1, rows=new_count, requests=requests
                    )

                session.commit()
        logger.debug(
            "单日提交 run_id=%s dataset=%s trade_date=%s attempt=%d "
            "row_count=%d old_count=%d",
            run_id, dataset.value, trade_date, attempt, new_count, old_count,
        )

    # ---- run 收尾（§19：SUCCESS/PARTIAL/FAILED/NOOP）----

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
            error_summary = "水位对账失败，本轮未推进日级数据集"
        else:
            outcomes = set(dataset_outcomes.values())
            if "CANCELLED" in outcomes:
                # §49：收到停机信号——当前事务已正常完成，不开始下一日
                status = RunStatus.INTERRUPTED
                error_summary = "收到停机信号，本轮提前结束（已完成进度已保存）"
            elif outcomes <= {"NOOP"}:
                status = RunStatus.NOOP
                error_summary = None
            elif "FAILED" in outcomes and "SUCCESS" not in outcomes:
                status = RunStatus.FAILED
                error_summary = self._error_summary(dataset_outcomes)
            elif "FAILED" in outcomes:
                status = RunStatus.PARTIAL
                error_summary = self._error_summary(dataset_outcomes)
            else:
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

