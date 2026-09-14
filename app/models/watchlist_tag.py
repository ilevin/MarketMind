"""自选条目与标签的多对多关联模型，按用户隔离。

- (user_id, instrument_id, tag_id) 复合主键；
- 复合外键 (user_id, instrument_id) -> watchlist(user_id, instrument_id)：
  保证关联行与其所属自选条目始终属于同一用户（user_id 单独在 watchlist
  中不唯一，必须整体引用复合主键）；
- tag_id 外键为数据库层兜底（RESTRICT 语义），被引用的标签由业务层拦截删除；
- 外键均不带 ON DELETE（DuckDB 不支持外键级联删除）：删除自选条目时由
  WatchlistService 在同一写锁内先删关联行再删条目（分两次提交，Phase 0 结论）。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WatchlistTag(Base):
    __tablename__ = "watchlist_tag"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "instrument_id"],
            ["watchlist.user_id", "watchlist.instrument_id"],
            name="fk_watchlist_tag_watchlist",
        ),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tag_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag.tag_id", name="fk_watchlist_tag_tag"),
        primary_key=True,
    )
