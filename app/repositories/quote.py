"""行情快照仓储：原子 upsert（一证券一行）+ 主键直查（缓存回退用）。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.quote import QuoteSnapshot


class QuoteSnapshotRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert(self, snapshot: QuoteSnapshot) -> None:
        """原子 upsert：主键冲突原地更新，数据库保证每只证券只有一行当前行情。

        fetched_at 未显式提供时省略该列（values 显式 None 会绕过列 default
        导致 NOT NULL 违反），由列 default（utcnow）填充；冲突更新仅在显式
        提供时才覆盖 fetched_at（保留已有时间戳语义）。
        """
        values = dict(
            instrument_id=snapshot.instrument_id,
            price=snapshot.price,
            change_percent=snapshot.change_percent,
            volume_ratio=snapshot.volume_ratio,
            previous_close=snapshot.previous_close,
            source=snapshot.source,
            source_timestamp=snapshot.source_timestamp,
        )
        if snapshot.fetched_at is not None:
            values["fetched_at"] = snapshot.fetched_at
        stmt = postgresql.insert(QuoteSnapshot).values(**values)
        set_ = {
            "price": stmt.excluded.price,
            "change_percent": stmt.excluded.change_percent,
            "volume_ratio": stmt.excluded.volume_ratio,
            "previous_close": stmt.excluded.previous_close,
            "source": stmt.excluded.source,
            "source_timestamp": stmt.excluded.source_timestamp,
        }
        if snapshot.fetched_at is not None:
            set_["fetched_at"] = stmt.excluded.fetched_at
        stmt = stmt.on_conflict_do_update(
            index_elements=[QuoteSnapshot.instrument_id],
            set_=set_,
        )
        self.session.execute(stmt)

    def latest(self, instrument_id: str) -> QuoteSnapshot | None:
        return self.session.scalar(
            select(QuoteSnapshot).where(QuoteSnapshot.instrument_id == instrument_id)
        )

    def latest_many(self, instrument_ids: list[str]) -> dict[str, QuoteSnapshot]:
        """每个 instrument 的当前快照（主键保证单行；内存缓存预热 / API 回退）。"""
        result: dict[str, QuoteSnapshot] = {}
        if not instrument_ids:
            return result
        rows = self.session.scalars(
            select(QuoteSnapshot).where(QuoteSnapshot.instrument_id.in_(instrument_ids))
        ).all()
        for row in rows:
            result[row.instrument_id] = row
        return result

    def count(self) -> int:
        return self.session.scalar(select(func.count()).select_from(QuoteSnapshot)) or 0
