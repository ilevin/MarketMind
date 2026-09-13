"""行情刷新服务：按市场分组、仅 OPEN 刷新、收盘补抓、单市场失败隔离。

刷新流程（PRD 第 13 节）：
    读取 watchlist + index_watchlist -> 按市场分组 -> 判断市场状态 ->
    仅刷新 OPEN 市场 -> 按资产类型调用 QuoteProvider -> 更新内存缓存 -> 保存 QuoteSnapshot
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import AppConfig
from app.db import write_coordinator
from app.models.instrument import Instrument
from app.models.quote import QuoteSnapshot
from app.providers.base import QuoteProvider
from app.repositories.quote import QuoteSnapshotRepository
from app.services.market_session_service import (
    MarketSessionService,
    MarketStatus,
    now_beijing,
)
from app.services.quote_cache import QuoteCache

logger = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    updated: int = 0
    failed: int = 0
    markets: dict = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.failed == 0


class RefreshService:
    def __init__(
        self,
        config: AppConfig,
        session_factory,
        quote_providers,
        session_service: MarketSessionService,
        cache: QuoteCache,
    ):
        self.config = config
        self.session_factory = session_factory
        self.quote_providers = quote_providers  # QuoteProviderRegistry
        self.session_service = session_service
        self.cache = cache
        self._last_status: dict[str, MarketStatus] = {}

    # ---- 读取待刷新标的 ----

    def _load_instruments(self) -> list[Instrument]:
        from app.repositories.watchlist import IndexWatchlistRepository, WatchlistRepository

        with self.session_factory() as session:
            instruments = [inst for _, inst in WatchlistRepository(session).list_ordered()]
            instruments += [inst for _, inst in IndexWatchlistRepository(session).list_ordered()]
            return instruments

    # ---- 刷新入口 ----

    def tick(self, now=None) -> RefreshResult:
        """60 秒任务单轮：处理 OPEN 刷新与 OPEN->CLOSED 收盘补抓边沿。"""
        result = RefreshResult()
        statuses = self.session_service.all_status(now)
        for market, status in statuses.items():
            if status == MarketStatus.OPEN:
                result.markets[market] = self._refresh_market(market, result, now=now)
            elif (
                self._last_status.get(market) == MarketStatus.OPEN
                and status == MarketStatus.CLOSED
            ):
                # 收盘补抓：避免缓存停留在收盘前一分钟
                logger.info("%s 收盘补抓一次行情", market)
                result.markets[market] = self._refresh_market(market, result, now=now)
            self._last_status[market] = status
        return result

    def refresh_all(self, force: bool = False) -> RefreshResult:
        """手动刷新（管理接口）：默认仍遵守市场状态；force=True 忽略状态。"""
        result = RefreshResult()
        for market in ("CN", "HK"):
            if force or self.session_service.status(market) == MarketStatus.OPEN:
                result.markets[market] = self._refresh_market(market, result)
            else:
                result.markets[market] = "skipped"
        return result

    def refresh_instruments_now(self, instrument_ids: list[str]) -> None:
        """添加自选后立即触发一次该资产行情更新（PRD 17.4）。"""
        if not instrument_ids:
            return
        try:
            instruments = [
                inst
                for inst in self._load_instruments()
                if inst.instrument_id in instrument_ids
            ]
            self._refresh_instrument_list(instruments)
        except Exception:
            logger.exception("添加自选后的即时行情刷新失败: %s", instrument_ids)

    # ---- 内部 ----

    def _refresh_market(self, market: str, result: RefreshResult, now=None) -> str:
        instruments = [i for i in self._load_instruments() if i.market == market]
        if not instruments:
            return "no_instruments"
        updated, failed = self._refresh_instrument_list(instruments, now=now)
        result.updated += updated
        result.failed += failed
        return "ok" if failed == 0 else "partial"

    def _refresh_instrument_list(self, instruments: list[Instrument], now=None) -> tuple[int, int]:
        """按资产类型分组调用 Provider；单市场/单 Provider 失败隔离。"""
        fetched_at = now or now_beijing()
        try:
            quotes = self.quote_providers.get_quotes(instruments)
        except Exception:
            # Provider 注册表整体异常：本轮全部记失败，保留缓存，不让上层 500
            logger.exception("行情 Provider 注册表调用失败（%d 个标的）", len(instruments))
            quotes = {}

        updated = 0
        failed = 0
        with write_coordinator.write():
            with self.session_factory() as session:
                repo = QuoteSnapshotRepository(session)
                for inst in instruments:
                    quote = quotes.get(inst.instrument_id)
                    if quote is None:
                        failed += 1
                        logger.warning(
                            "未获取到行情: %s（%s/%s）", inst.instrument_id, inst.market, inst.asset_type
                        )
                        continue
                    repo.upsert(_quote_to_snapshot(quote, fetched_at))
                    updated += 1
                session.commit()
        # 只用本轮成功的报价更新内存缓存，失败标的保留上次缓存
        self.cache.update(quotes, fetched_at)
        return updated, failed


def _quote_to_snapshot(quote: Quote, fetched_at) -> QuoteSnapshot:
    return QuoteSnapshot(
        instrument_id=quote.instrument_id,
        price=quote.price,
        change_percent=quote.change_percent,
        volume_ratio=quote.volume_ratio,
        previous_close=quote.previous_close,
        source=quote.source,
        source_timestamp=quote.source_timestamp,
        fetched_at=fetched_at,
    )
