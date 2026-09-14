"""估值刷新任务：每日收盘后一次 + 周期检查补齐覆盖缺口（PRD 第 14 节）。

当日估值是否「已刷新」按自选 A 股覆盖率判定：存在缺当日数据的自选 A 股
（含盘中新增的股票）时，下一次周期检查补刷一次。停牌/新股等 Tushare 当日
无记录的标的在内存标记已尝试，当日不再重试；手动刷新强制重试全部。
v0.03：每轮经 JobStatusService 记录开始 / 成功 / 失败（last-success 语义）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta

from app.config import AppConfig
from app.providers.base import Fundamental
from app.repositories.fundamental import FundamentalRepository
from app.services.market_session_service import MarketStatus, now_beijing

logger = logging.getLogger(__name__)

# 收盘后多久开始当日估值更新
_AFTER_CLOSE_DELAY = timedelta(minutes=30)
_CHECK_INTERVAL_SECONDS = 30 * 60  # 每 30 分钟检查一次是否需要更新


class FundamentalRefreshJob:
    JOB_NAME = "fundamental_refresh"

    def __init__(self, config, session_factory, provider, session_service, job_status_service=None):
        self.config = config
        self.session_factory = session_factory
        self.provider = provider
        self.session_service = session_service
        self.job_status = job_status_service
        self._task: asyncio.Task | None = None
        # {trade_date: 当日确认无数据的 instrument_id 集合}；内存态，重启后允许再试一次
        self._attempted: dict[date, set[str]] = {}

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="fundamental-refresh")
            logger.info("估值刷新任务已启动")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.to_thread(self._maybe_run_with_status)
            await asyncio.sleep(_CHECK_INTERVAL_SECONDS)

    def _maybe_run_with_status(self) -> None:
        """单轮检查 + 状态记录；执行完成无异常即记成功（含无 Token / 无缺口的空转）。"""
        started = time.monotonic()
        if self.job_status is not None:
            self.job_status.record_started(self.JOB_NAME)
        try:
            self._maybe_run()
            if self.job_status is not None:
                self.job_status.record_success(
                    self.JOB_NAME, int((time.monotonic() - started) * 1000)
                )
        except Exception as exc:
            logger.exception("估值刷新任务异常")
            if self.job_status is not None:
                self.job_status.record_failure(
                    self.JOB_NAME, int((time.monotonic() - started) * 1000), str(exc)
                )

    def _latest_trade_date(self):
        """最近的 CN 交易日（含今天，若今天是交易日且已收盘）。

        经 session_service.calendar（Provider）判定：未缓存时自动拉取日历，
        避免启动初期日历仓储为空导致 trade_date 误判为 None、补刷被跳过。
        """
        today = now_beijing().date()
        calendar = self.session_service.calendar
        for back in range(7):
            day = today - timedelta(days=back)
            if calendar.is_trading_day("CN", day):
                # 今天是交易日但要收盘后才取当日数据
                if day == today:
                    close_time = now_beijing().replace(hour=15, minute=30, second=0, microsecond=0)
                    if now_beijing() < close_time:
                        continue
                return day
        return None

    def _maybe_run(self) -> None:
        if not self.config.has_tushare_token:
            return
        trade_date = self._latest_trade_date()
        if trade_date is None:
            return
        missing = self._missing_instruments(trade_date)
        if not missing:
            return  # 当日自选 A 股估值已全覆盖
        self._refresh(trade_date, missing)

    def _watchlist_cn_stocks(self) -> list:
        # 系统作用域：跨用户聚合全部自选（DISTINCT），供后台估值刷新
        from app.repositories.watchlist import SystemWatchlistRepository

        with self.session_factory() as session:
            return [
                inst
                for inst in SystemWatchlistRepository(session).all_instruments()
                if inst.market == "CN" and inst.asset_type == "STOCK"
            ]

    def _missing_instruments(self, trade_date: date) -> list:
        """当日缺有效估值的自选 A 股（剔除已标记「当日无数据」的标的）。

        行存在但三指标全空也视为缺失：数据源当日指标（如股息率）生成有
        延迟，待其回填后覆盖才完成。
        """
        stocks = self._watchlist_cn_stocks()
        attempted = self._attempted.get(trade_date, set())
        candidates = [inst for inst in stocks if inst.instrument_id not in attempted]
        if not candidates:
            return []
        with self.session_factory() as session:
            covered = FundamentalRepository(session).covered_instrument_ids(trade_date)
        return [inst for inst in candidates if inst.instrument_id not in covered]

    def run_once(self, trade_date=None) -> dict:
        """执行一次估值刷新（手动/维护入口，强制重试全部自选 A 股）。"""
        trade_date = trade_date or self._latest_trade_date()
        if trade_date is not None:
            self._attempted.pop(trade_date, None)
        return self._refresh(trade_date, self._watchlist_cn_stocks())

    def refresh_instruments(self, instrument_ids: list[str]) -> int:
        """按 instrument_id 获取最近一期估值（添加自选后即时调用）。返回写入条数。"""
        if not instrument_ids or not self.config.has_tushare_token:
            return 0
        from app.repositories.instrument import InstrumentRepository

        instruments = []
        with self.session_factory() as session:
            repo = InstrumentRepository(session)
            for instrument_id in instrument_ids:
                inst = repo.get(instrument_id)
                if inst is not None and inst.market == "CN" and inst.asset_type == "STOCK":
                    instruments.append(inst)
        if not instruments:
            return 0
        fundamentals = self.provider.get_fundamentals(instruments)
        self._persist(fundamentals)
        return len(fundamentals)

    def _refresh(self, trade_date, instruments: list) -> dict:
        fundamentals = self.provider.get_fundamentals(instruments, trade_date)
        self._persist(fundamentals)
        written = set(fundamentals)
        updated = len(written)
        failed = max(len(instruments) - updated, 0)
        if trade_date is not None and failed:
            # 补刷后仍缺失（停牌/新股等当日无数据）：标记后当日不再重试
            self._attempted.setdefault(trade_date, set()).update(
                inst.instrument_id for inst in instruments if inst.instrument_id not in written
            )
        logger.info(
            "估值刷新完成: trade_date=%s updated=%d failed=%d", trade_date, updated, failed
        )
        return {"success": failed == 0 and updated > 0, "updated": updated, "failed": failed}

    def _persist(self, fundamentals: dict[str, Fundamental]) -> None:
        from app.db import write_coordinator
        from app.models.fundamental import FundamentalSnapshot

        with write_coordinator.write():
            with self.session_factory() as session:
                repo = FundamentalRepository(session)
                for fund in fundamentals.values():
                    repo.upsert(
                        FundamentalSnapshot(
                            instrument_id=fund.instrument_id,
                            trade_date=fund.trade_date,
                            pe_ttm=fund.pe_ttm,
                            pb=fund.pb,
                            dividend_yield_ttm=fund.dividend_yield_ttm,
                            source=fund.source,
                        )
                    )
                session.commit()
