"""标签仓储（v0.03b）：usage 计数按 watchlist_tag 关联表统计（多对多；指数无标签，不计入）。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.tag import Tag
from app.models.watchlist_tag import WatchlistTag


class TagRepository:
    def __init__(self, session: Session):
        self.session = session

    def list_with_usage(self) -> list[tuple[Tag, int]]:
        """全部标签及其被股票/ETF 自选引用的次数（按关联行计数），按创建顺序返回。

        group_by(Tag) 展开为 tag 全部列：DuckDB 严格执行 SQL 标准，
        GROUP BY 仅主键时 SELECT 的非聚合列（name 等）会报 Binder Error
        （SQLite/MySQL 的裸列容忍不可用）；tag_id 为主键，语义不变。
        """
        stmt = (
            select(Tag, func.count(WatchlistTag.tag_id))
            .outerjoin(WatchlistTag, WatchlistTag.tag_id == Tag.tag_id)
            .group_by(Tag)
            .order_by(Tag.tag_id)
        )
        return [(tag, count) for tag, count in self.session.execute(stmt).all()]

    def get(self, tag_id: int) -> Tag | None:
        return self.session.get(Tag, tag_id)

    def get_by_name(self, name: str) -> Tag | None:
        return self.session.scalar(select(Tag).where(Tag.name == name))

    def create(self, name: str) -> Tag:
        tag = Tag(name=name)
        self.session.add(tag)
        self.session.flush()
        return tag

    def rename(self, tag: Tag, name: str) -> Tag:
        tag.name = name
        self.session.flush()
        return tag

    def delete(self, tag: Tag) -> None:
        self.session.delete(tag)
        self.session.flush()

    def count_usage(self, tag_id: int) -> int:
        """被引用次数（watchlist_tag 关联行数）；删除保护的业务层依据。"""
        return int(
            self.session.scalar(
                select(func.count(WatchlistTag.tag_id)).where(WatchlistTag.tag_id == tag_id)
            )
            or 0
        )
