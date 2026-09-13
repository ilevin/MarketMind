"""后台行情刷新任务：FastAPI lifespan 启动的 60 秒 asyncio 循环。

Provider 均为同步实现（akshare/httpx），通过 asyncio.to_thread 调用，
避免阻塞事件循环。单轮异常只记日志，绝不让主进程退出。
v0.03：每轮经 JobStatusService 记录开始 / 成功 / 失败（last-success 语义）。
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.config import AppConfig
from app.services.refresh_service import RefreshService

logger = logging.getLogger(__name__)


class QuoteRefreshJob:
    JOB_NAME = "quote_refresh"

    def __init__(
        self,
        config: AppConfig,
        refresh_service: RefreshService,
        job_status_service=None,
    ):
        self.refresh_seconds = config.quote.refresh_seconds
        self.refresh_service = refresh_service
        self.job_status = job_status_service
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="quote-refresh")
            logger.info("行情刷新任务已启动（周期 %d 秒）", self.refresh_seconds)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("行情刷新任务已停止")

    async def _run(self) -> None:
        while True:
            await asyncio.to_thread(self._tick_with_status)
            await asyncio.sleep(self.refresh_seconds)

    def _tick_with_status(self) -> None:
        """单轮刷新 + 状态记录；执行完成无异常即记成功（含无 OPEN 市场的空转）。"""
        started = time.monotonic()
        if self.job_status is not None:
            self.job_status.record_started(self.JOB_NAME)
        try:
            result = self.refresh_service.tick()
            logger.info(
                "行情刷新完成: updated=%d failed=%d markets=%s",
                result.updated, result.failed, result.markets,
            )
            if self.job_status is not None:
                self.job_status.record_success(
                    self.JOB_NAME, int((time.monotonic() - started) * 1000)
                )
        except Exception as exc:
            logger.exception("后台行情刷新任务异常")
            if self.job_status is not None:
                self.job_status.record_failure(
                    self.JOB_NAME, int((time.monotonic() - started) * 1000), str(exc)
                )
