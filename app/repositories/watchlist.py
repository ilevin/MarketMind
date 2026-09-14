"""自选 / 指数配置仓储（multi-user-auth 用户作用域）。

- 用户作用域仓储构造时 user_id 必填（无默认值），全部查询在 SQL 层
  附加 user_id 过滤——隔离必须发生在数据库查询层（user-data-isolation spec）；
- SystemWatchlistRepository 为系统作用域：跨用户 DISTINCT instrument 集合，
  仅供后台任务（缓存预热 / 周期刷新 / 收盘补抓）使用，业务路径禁止引用；
- 两套列表结构一致，用泛型基类避免重复（不加抽象层）。
"""

from __future__ import annotations

from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.instrument import Instrument
from app.models.tag import Tag
from app.models.watchlist import IndexWatchlist, Watchlist
from app.models.watchlist_tag import WatchlistTag

T = TypeVar("T", bound=Watchlist | IndexWatchlist)


class BaseWatchlistRepository(Generic[T]):
    model: type[T]

    def __init__(self, session: Session, user_id: int):
        # user_id 必填且无默认值：不存在漏传后静默查全库的路径
        self.session = session
        self.user_id = user_id

    def list_ordered(self) -> list[tuple[T, Instrument]]:
        stmt = (
            select(self.model, Instrument)
            .join(Instrument, self.model.instrument_id == Instrument.instrument_id)
            .where(self.model.user_id == self.user_id)
            .order_by(self.model.sort_order, self.model.created_at)
        )
        return list(self.session.execute(stmt).all())

    def get(self, instrument_id: str) -> T | None:
        return self.session.scalar(
            select(self.model).where(
                self.model.user_id == self.user_id,
                self.model.instrument_id == instrument_id,
            )
        )

    def exists(self, instrument_id: str) -> bool:
        return self.get(instrument_id) is not None

    def add(self, instrument_id: str, sort_order: int = 0) -> T:
        row = self.model(user_id=self.user_id, instrument_id=instrument_id, sort_order=sort_order)
        self.session.add(row)
        self.session.flush()
        return row

    def remove(self, instrument_id: str) -> bool:
        row = self.get(instrument_id)
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    def reorder(self, orders: dict[str, int]) -> None:
        for instrument_id, sort_order in orders.items():
            row = self.get(instrument_id)
            if row is not None:
                row.sort_order = sort_order
        self.session.flush()

    def next_sort_order(self) -> int:
        rows = self.session.scalars(
            select(self.model).where(self.model.user_id == self.user_id)
        ).all()
        return max((r.sort_order for r in rows), default=0) + 10


class WatchlistRepository(BaseWatchlistRepository[Watchlist]):
    model = Watchlist

    def list_ordered_with_tags(self) -> list[tuple[Watchlist, Instrument, list[Tag]]]:
        """带标签列表的列表（多对多）；在子类扩展而非泛型基类，避免波及指数仓储。"""
        stmt = (
            select(Watchlist, Instrument, Tag)
            .join(Instrument, Watchlist.instrument_id == Instrument.instrument_id)
            .outerjoin(
                WatchlistTag,
                (WatchlistTag.instrument_id == Watchlist.instrument_id)
                & (WatchlistTag.user_id == Watchlist.user_id),
            )
            .outerjoin(Tag, Tag.tag_id == WatchlistTag.tag_id)
            .where(Watchlist.user_id == self.user_id)
            .order_by(Watchlist.sort_order, Watchlist.created_at, Tag.tag_id)
        )
        result: list[tuple[Watchlist, Instrument, list[Tag]]] = []
        index: dict[str, tuple[Watchlist, Instrument, list[Tag]]] = {}
        for row, inst, tag in self.session.execute(stmt).all():
            item = index.get(row.instrument_id)
            if item is None:
                item = (row, inst, [])
                index[row.instrument_id] = item
                result.append(item)
            if tag is not None:
                item[2].append(tag)
        return result


class IndexWatchlistRepository(BaseWatchlistRepository[IndexWatchlist]):
    model = IndexWatchlist


class SystemWatchlistRepository:
    """系统作用域（仅后台任务）：跨用户聚合的全部自选 instrument_id（DISTINCT）。

    多用户关注同一证券时行情仍只刷新一次（quote-cache-refresh spec）。
    """

    def __init__(self, session: Session):
        self.session = session

    def all_instrument_ids(self) -> list[str]:
        # UNION 自带去重语义；CompoundSelect 无 .distinct()（SQLAlchemy 2.0）
        stmt = select(Watchlist.instrument_id).union(select(IndexWatchlist.instrument_id))
        return list(self.session.scalars(stmt).all())

    def all_instruments(self) -> list[Instrument]:
        """去重后的全部自选证券主数据（含指数），供后台刷新按市场分组。"""
        ids = set(self.all_instrument_ids())
        if not ids:
            return []
        return list(
            self.session.scalars(
                select(Instrument).where(Instrument.instrument_id.in_(ids))
            ).all()
        )
