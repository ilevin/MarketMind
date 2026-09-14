"""登录会话模型（multi-user-auth）：服务端 Session，Cookie 只存随机 token。

- 主键为 session_token_hash（SHA-256(token)），数据库泄露不直接暴露可用 Cookie；
- csrf_token 与 Session 绑定，写请求经 X-CSRF-Token 头双提交校验；
- 撤销语义用 revoked_at 可空列表达（不物理删行，便于审计）；
- Session 读写属于普通 DuckDB 读写路径；创建/撤销走 WriteCoordinator。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserSession(Base):
    __tablename__ = "user_session"

    session_token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.user_id", name="fk_user_session_user")
    )
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
