"""用户模型（multi-user-auth）：账户、角色与启用状态。

- 避免数据库保留词 ``user``，表名 ``app_user``；
- 角色仅 ``user`` / ``admin`` 两种，直接存列，不做 RBAC 多表（design D3）；
- user_id 由显式 sequence（seq_user_id）生成，风格与 tag_id 一致；
- username 唯一性按小写比较、由 UserRepository 在写锁内查重保证
  （沿用 tag.name 的单写者模式，不依赖数据库 UNIQUE 约束，design D4）；
- 只禁用不物理删除（is_active=false），避免误删自选/标签并保留审计（design D13）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Sequence, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLES = (ROLE_USER, ROLE_ADMIN)


class AppUser(Base):
    """用户账户；password_hash 为 Argon2id，明文/可逆形式禁止入库。"""

    __tablename__ = "app_user"

    user_id: Mapped[int] = mapped_column(BigInteger, Sequence("seq_user_id"), primary_key=True)
    username: Mapped[str] = mapped_column(String(32))
    # 不可登录的占位哈希（迁移 legacy owner）；正式密码经 /setup、CLI 或管理接口设置
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(8), server_default=text("'user'"))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    must_change_password: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
