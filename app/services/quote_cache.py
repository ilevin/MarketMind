"""行情内存缓存：进程内 dict + DuckDB 快照回退（PRD 第 12 节）。

写入顺序：Provider 成功 -> 更新内存 -> 落 quote_snapshot；
失败 -> 两者都不动（不因失败清空缓存）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.models.quote import QuoteSnapshot
from app.providers.base import Quote
from app.services.market_session_service import MarketStatus, now_beijing


@dataclass
class CachedQuote:
    quote: Quote
    fetched_at: datetime


class QuoteCache:
    def __init__(self, stale_seconds: int = 180):
        self._data: dict[str, CachedQuote] = {}
        self.stale_seconds = stale_seconds

    def warmup(self, snapshots: dict[str, QuoteSnapshot]) -> None:
        """启动时从 DuckDB 预热最近快照。"""
        for instrument_id, snap in snapshots.items():
            if instrument_id not in self._data:
                self._data[instrument_id] = CachedQuote(
                    quote=_snapshot_to_quote(snap), fetched_at=snap.fetched_at
                )

    def update(self, quotes: dict[str, Quote], fetched_at: datetime | None = None) -> None:
        fetched_at = fetched_at or now_beijing()
        for instrument_id, quote in quotes.items():
            self._data[instrument_id] = CachedQuote(quote=quote, fetched_at=fetched_at)

    def get(self, instrument_id: str) -> CachedQuote | None:
        return self._data.get(instrument_id)

    def is_stale(self, instrument_id: str, market_status: MarketStatus, now: datetime | None = None) -> bool:
        """OPEN 且超过 stale_seconds 未更新 -> stale；非交易时段不因时间流逝 stale。"""
        if market_status != MarketStatus.OPEN:
            return False
        cached = self._data.get(instrument_id)
        if cached is None:
            return False
        now = now or now_beijing()
        # DuckDB TIMESTAMPTZ 保真往返，fetched_at 恒为 aware，直接相减即可（design D4）
        age = (now - cached.fetched_at).total_seconds()
        return age > self.stale_seconds


def _snapshot_to_quote(snap: QuoteSnapshot) -> Quote:
    from app.providers.safe_values import safe_float

    return Quote(
        instrument_id=snap.instrument_id,
        price=safe_float(snap.price),
        change_percent=safe_float(snap.change_percent),
        volume_ratio=safe_float(snap.volume_ratio),
        previous_close=safe_float(snap.previous_close),
        source=snap.source,
        source_timestamp=snap.source_timestamp,
        delayed=False,  # 快照不保存 delayed 时按实时展示
    )
