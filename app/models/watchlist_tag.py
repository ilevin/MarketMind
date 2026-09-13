"""自选条目与标签的多对多关联模型（v0.03b 需求修订：一个条目可关联多个标签）。

- (instrument_id, tag_id) 复合主键，一证券一行一标签；
- 外键均不带 ON DELETE（DuckDB 不支持外键级联删除）：删除自选条目时由
  WatchlistService 在同一事务内先删关联行再删条目；
- tag_id 外键为数据库层兜底（RESTRICT 语义），被引用的标签由业务层计数检查拦截删除。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WatchlistTag(Base):
    __tablename__ = "watchlist_tag"

    instrument_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("watchlist.instrument_id", name="fk_watchlist_tag_instrument"),
        primary_key=True,
    )
    tag_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag.tag_id", name="fk_watchlist_tag_tag"),
        primary_key=True,
    )
