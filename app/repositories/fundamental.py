"""估值快照仓储。"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.fundamental import FundamentalSnapshot


class FundamentalRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert(self, snapshot: FundamentalSnapshot) -> None:
        """原子 upsert：复合主键 (instrument_id, trade_date) 冲突原地更新（幂等）。

        fetched_at 未显式提供时省略该列（values 显式 None 会绕过列 default
        导致 NOT NULL 违反），由列 default（utcnow）填充；冲突更新仅在显式
        提供时才覆盖 fetched_at（保留已有时间戳语义）。
        """
        values = dict(
            instrument_id=snapshot.instrument_id,
            trade_date=snapshot.trade_date,
            pe_ttm=snapshot.pe_ttm,
            pb=snapshot.pb,
            dividend_yield_ttm=snapshot.dividend_yield_ttm,
            source=snapshot.source,
        )
        if snapshot.fetched_at is not None:
            values["fetched_at"] = snapshot.fetched_at
        stmt = postgresql.insert(FundamentalSnapshot).values(**values)
        set_ = {
            "pe_ttm": stmt.excluded.pe_ttm,
            "pb": stmt.excluded.pb,
            "dividend_yield_ttm": stmt.excluded.dividend_yield_ttm,
            "source": stmt.excluded.source,
        }
        if snapshot.fetched_at is not None:
            set_["fetched_at"] = stmt.excluded.fetched_at
        stmt = stmt.on_conflict_do_update(
            index_elements=[FundamentalSnapshot.instrument_id, FundamentalSnapshot.trade_date],
            set_=set_,
        )
        self.session.execute(stmt)

    def latest(self, instrument_id: str) -> FundamentalSnapshot | None:
        return self.session.scalar(
            select(FundamentalSnapshot)
            .where(FundamentalSnapshot.instrument_id == instrument_id)
            .order_by(FundamentalSnapshot.trade_date.desc())
            .limit(1)
        )

    def latest_many(self, instrument_ids: list[str]) -> dict[str, FundamentalSnapshot]:
        result: dict[str, FundamentalSnapshot] = {}
        if not instrument_ids:
            return result
        rows = self.session.scalars(
            select(FundamentalSnapshot).where(
                FundamentalSnapshot.instrument_id.in_(instrument_ids)
            )
        ).all()
        for row in rows:
            current = result.get(row.instrument_id)
            if current is None or row.trade_date > current.trade_date:
                result[row.instrument_id] = row
        return result

    def covered_instrument_ids(self, trade_date: date) -> set[str]:
        """当日估值已完整的 instrument_id 集合（三指标全非空，供覆盖率判定）。

        存在空指标的行视为未覆盖：数据源当日部分指标（如股息率）生成有
        延迟，空指标行参与周期补刷重试，回填后覆盖完成；指标当日确实无
        值的标的（如亏损股 PE 为空）会重试至当日结束，量级安全。
        """
        return set(
            self.session.scalars(
                select(FundamentalSnapshot.instrument_id).where(
                    FundamentalSnapshot.trade_date == trade_date,
                    FundamentalSnapshot.pe_ttm.is_not(None),
                    FundamentalSnapshot.pb.is_not(None),
                    FundamentalSnapshot.dividend_yield_ttm.is_not(None),
                )
            ).all()
        )
