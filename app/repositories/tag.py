"""标签仓储（multi-user-auth 用户作用域）：usage 计数按当前用户的 watchlist_tag 关联统计。

标签命名空间为同一 user_id 内；跨用户同名标签共存（tag-management spec）。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.tag import Tag
from app.models.watchlist_tag import WatchlistTag


class TagRepository:
    def __init__(self, session: Session, user_id: int):
        # user_id 必填且无默认值：不存在漏传后静默查全库的路径
        self.session = session
        self.user_id = user_id

    def list_with_usage(self) -> list[tuple[Tag, int]]:
        """当前用户的全部标签及其被其股票/ETF 自选引用的次数，按创建顺序返回。

        group_by(Tag) 展开为 tag 全部列：DuckDB 严格执行 SQL 标准，
        GROUP BY 仅主键时 SELECT 的非聚合列（name 等）会报 Binder Error
        （SQLite/MySQL 的裸列容忍不可用）；tag_id 为主键，语义不变。
        """
        stmt = (
            select(Tag, func.count(WatchlistTag.tag_id))
            .outerjoin(
                WatchlistTag,
                (WatchlistTag.tag_id == Tag.tag_id)
                & (WatchlistTag.user_id == self.user_id),
            )
            .where(Tag.user_id == self.user_id)
            .group_by(Tag)
            .order_by(Tag.tag_id)
        )
        return [(tag, count) for tag, count in self.session.execute(stmt).all()]

    def get(self, tag_id: int) -> Tag | None:
        """仅返回属于当前用户的标签（越权与不存在同返回 None → API 层 404）。"""
        return self.session.scalar(
            select(Tag).where(Tag.tag_id == tag_id, Tag.user_id == self.user_id)
        )

    def get_by_name(self, name: str) -> Tag | None:
        """用户内唯一性查重（写锁内调用）。"""
        return self.session.scalar(
            select(Tag).where(Tag.user_id == self.user_id, Tag.name == name)
        )

    def create(self, name: str) -> Tag:
        tag = Tag(user_id=self.user_id, name=name)
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
        """被当前用户自选引用的次数（watchlist_tag 关联行数）；删除保护依据。"""
        return int(
            self.session.scalar(
                select(func.count(WatchlistTag.tag_id)).where(
                    WatchlistTag.tag_id == tag_id,
                    WatchlistTag.user_id == self.user_id,
                )
            )
            or 0
        )
