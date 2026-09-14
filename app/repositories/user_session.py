"""登录会话仓储（multi-user-auth）：创建、按 token hash 校验查询、撤销。

Session 校验为读路径；创建/撤销为写路径，由 AuthService 包裹
write_coordinator 执行。撤销用 revoked_at 列表达，不物理删行。
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.user_session import UserSession


class UserSessionRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_valid(self, session_token_hash: str, now) -> UserSession | None:
        """按 token hash 查询有效 Session：未过期且未撤销，否则 None。"""
        row = self.session.get(UserSession, session_token_hash)
        if row is None:
            return None
        if row.revoked_at is not None:
            return None
        if row.expires_at is not None and row.expires_at <= now:
            return None
        return row

    def create(
        self,
        *,
        session_token_hash: str,
        user_id: int,
        csrf_token: str,
        expires_at,
    ) -> UserSession:
        row = UserSession(
            session_token_hash=session_token_hash,
            user_id=user_id,
            csrf_token=csrf_token,
            expires_at=expires_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def revoke(self, session_token_hash: str, now) -> None:
        self.session.execute(
            update(UserSession)
            .where(UserSession.session_token_hash == session_token_hash)
            .values(revoked_at=now)
        )
        self.session.flush()

    def revoke_all_for_user(self, user_id: int, now) -> None:
        """禁用/重置密码/改角色时撤销该用户全部 Session。"""
        self.session.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        self.session.flush()
