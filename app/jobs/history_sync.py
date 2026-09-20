"""历史数据同步任务：每日 schedule_time 调度 + 启动 catch-up + 进程级 single-flight。

职责边界（design.md 第 10 节 D17~D18）：业务编排全在
``HistorySyncService.run``；本 Job 只负责进程级调度与互斥：

- 每日 ``schedule_time``（Asia/Shanghai，含周末——不产生虚假交易日且可追平
  周五缺口）触发 SCHEDULED 同步；
- 启动时若 ``startup_catchup=true``，触发一次 STARTUP 同步；
- 进程级 single-flight：定时触发时已有任务在跑则记 skip 不重复启动；管理员
  手动触发时返回 None，由 API 层回 409 与正在运行的 run_id；
- 同步 Service 为同步实现（Tushare SDK 阻塞），经 ``asyncio.to_thread`` 调用，
  不阻塞事件循环（与既有 Job 一致）；
- 停机时置 cancellation event：Service 在交易日边界/重试 sleep 前后/master
  分片间检查，当前事务允许正常完成；
- 经 JobStatusService 记录 job_name="history_sync" 的高层健康（业务进度在
  history_sync_* 表，职责分离）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, time as dt_time, timedelta

from app.config import AppConfig
from app.models.history_sync import TriggerType
from app.repositories.history_sync import HistorySyncRunRepository
from app.services.history.sync_service import HistorySyncService
from app.services.market_session_service import now_beijing

logger = logging.getLogger(__name__)

# 调度循环的检查间隔：每 30 秒判断一次是否到点
_CHECK_INTERVAL_SECONDS = 30
# 调度容差：到点后 5 分钟内允许触发（避免 sleep 抖动错过整点）
_SCHEDULE_TOLERANCE = timedelta(minutes=5)


def _parse_schedule_time(value: str) -> dt_time:
    """``"20:30"`` -> ``time(20, 30)``；非法配置直接失败（不静默降级）。"""
    try:
        hour, minute = value.split(":")
        return dt_time(hour=int(hour), minute=int(minute))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"history.schedule_time 配置非法: {value!r}（应为 HH:MM）") from exc


class HistorySyncJob:
    """历史同步调度任务（进程级 single-flight）。"""

    JOB_NAME = "history_sync"

    def __init__(
        self,
        config: AppConfig,
        history_service: HistorySyncService,
        job_status_service=None,
    ):
        self.config = config
        self.history = history_service
        self.job_status = job_status_service
        self.schedule_time = _parse_schedule_time(config.history.schedule_time)

        self._task: asyncio.Task | None = None
        # 进程级 single-flight（design D17）：调度循环与手动触发共享同一把锁
        self._lock = threading.Lock()
        self._running = False
        self._cancellation_event: threading.Event | None = None
        # 本进程正在运行的 run_id：手动触发时由 API 层预留并立刻登记，
        # 使 409 响应体在 run 行落库前也能给出 run_id（§52.2 契约要求必填）
        self._running_run_id: str | None = None
        self._last_scheduled_date = None

    # ---- 生命周期 ----

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="history-sync")
            logger.info(
                "历史同步任务已启动（每日 %s Asia/Shanghai，含周末）",
                self.config.history.schedule_time,
            )

    async def stop(self) -> None:
        """优雅停机：先置 cancellation event 让 Service 在检查点收尾，再取消任务。"""
        self._signal_cancel()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("历史同步任务已停止")

    def _signal_cancel(self) -> None:
        with self._lock:
            event = self._cancellation_event
        if event is not None:
            event.set()

    # ---- 互斥状态（API 层据此回 409） ----

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def running_run_id(self) -> str | None:
        """当前运行中 run 的 run_id（供 409 响应体）。

        先返回本进程登记值（手动触发时 API 层在 try_begin 后立即登记，
        此阶段 run 行可能尚未落库）；调度触发或进程外遗留运行则回落到库
        中唯一的 RUNNING 行。两者皆无才返回 None。
        """
        with self._lock:
            known = self._running_run_id
        if known is not None:
            return known
        with self.session_factory() as session:
            stale = HistorySyncRunRepository(session).find_stale_running()
        return stale[0].run_id if stale else None

    @property
    def session_factory(self):
        return self.history.session_factory

    # ---- 执行入口 ----

    async def run_scheduled(self, trigger: TriggerType) -> str | None:
        """调度触发：single-flight 失败时记 skip（不排队、不报错）。"""
        with self._lock:
            if self._running:
                logger.warning(
                    "历史同步已在进行中，本次 %s 触发跳过（不产生第二个 run）", trigger.value
                )
                return None
            self._running = True
            cancellation_event = threading.Event()
            self._cancellation_event = cancellation_event
        try:
            return await asyncio.to_thread(
                self._execute, trigger, None, cancellation_event
            )
        finally:
            self._end()

    def try_begin(self, *, run_id: str | None = None) -> bool:
        """尝试取得 single-flight（API 层在返回 202 前调用；失败即 409）。

        ``run_id`` 为 API 层预留的 run_id：一旦取得锁即登记，使随后（run 行
        落库前）到达的冲突请求也能拿到 run_id（§52.2）。
        """
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._cancellation_event = threading.Event()
            self._running_run_id = run_id
            return True

    def _end(self) -> None:
        with self._lock:
            self._running = False
            self._cancellation_event = None
            self._running_run_id = None

    def run_manual(self, requested_by_user_id: str, *, run_id: str | None = None) -> str:
        """已 ``try_begin()`` 占位后的手动执行入口（API 层用，经 to_thread 调用）。

        ``requested_by_user_id`` 由服务端从已认证用户取得，绝不接受客户端传值。
        ``run_id`` 由 API 层预留以便 202 立即回传。
        """
        with self._lock:
            cancellation_event = self._cancellation_event
        try:
            return self._execute(
                TriggerType.MANUAL, requested_by_user_id, cancellation_event, run_id
            )
        finally:
            self._end()

    def _execute(
        self,
        trigger: TriggerType,
        requested_by_user_id: str | None,
        cancellation_event: threading.Event | None,
        run_id: str | None = None,
    ) -> str:
        """真正执行一次同步；调用方已持有 single-flight 权利。"""
        started = time.monotonic()
        if self.job_status is not None:
            self.job_status.record_started(self.JOB_NAME)
        try:
            run_id = self.history.run(
                trigger=trigger,
                requested_by_user_id=requested_by_user_id,
                cancellation_event=cancellation_event,
                run_id=run_id,
            )
        except Exception as exc:
            # Service 内部已兜底绝大多数异常；此处只兜底"run 记录创建失败"
            # 等极端情形，绝不让调度循环退出（与既有 Job 一致）。
            logger.exception("历史同步任务异常 trigger=%s", trigger.value)
            if self.job_status is not None:
                self.job_status.record_failure(
                    self.JOB_NAME, int((time.monotonic() - started) * 1000), str(exc)
                )
            return ""
        if self.job_status is not None:
            self.job_status.record_success(
                self.JOB_NAME, int((time.monotonic() - started) * 1000)
            )
        return run_id

    # ---- 调度循环 ----

    async def _run(self) -> None:
        if self.config.history.startup_catchup:
            await self.run_scheduled(TriggerType.STARTUP)
        while True:
            await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
            moment = now_beijing()
            if self._should_fire_scheduled(moment):
                self._last_scheduled_date = moment.date()
                await self.run_scheduled(TriggerType.SCHEDULED)

    def _should_fire_scheduled(self, moment: datetime) -> bool:
        """到点判定：当天未触发过、时间已过 schedule_time 且在容差内。

        容差窗口之外的「已过点」不补跑——跨重启后的缺口由 startup catch-up
        与业务水位负责补齐，不靠调度器追补历史时点。
        """
        if self._last_scheduled_date == moment.date():
            return False
        scheduled = moment.replace(
            hour=self.schedule_time.hour,
            minute=self.schedule_time.minute,
            second=0,
            microsecond=0,
        )
        return scheduled <= moment < scheduled + _SCHEDULE_TOLERANCE
