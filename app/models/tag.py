"""标签模型：股票 / ETF 自选条目的用户自定义分类，按用户隔离命名空间。

- 标签先在标签管理中创建，再经 watchlist_tag 关联到自选条目（多对多）；
- 存在引用时禁止删除（业务层计数检查 + 数据库 RESTRICT 外键双层保护）；
- tag_id 由显式 sequence（seq_tag_id）生成，不依赖任何数据库的自增主键行为；
- name 唯一范围为同一 user_id 内（跨用户同名允许）：不设数据库 UNIQUE 约束
  （DuckDB 1.5.5 中父表被 FK 引用后其 UNIQUE 列不可 UPDATE，Phase 0 结论），
  重名校验由 TagService 在写锁内 SELECT 查重保证（单写者模型下无并发窗口）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Sequence, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tag(Base):
    """标签；name 同一用户内唯一（TagService 写锁内查重），非空且不超过 50 字符。"""

    __tablename__ = "tag"

    tag_id: Mapped[int] = mapped_column(BigInteger, Sequence("seq_tag_id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.user_id", name="fk_tag_user")
    )
    name: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
