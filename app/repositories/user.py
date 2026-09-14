"""用户仓储（multi-user-auth）：用户名小写比较、写锁内查重、角色/状态/密码更新。

Repository 只 flush 不 commit（database-persistence 事务边界）；用户名查重与
创建必须由调用方（UserService/AuthService）包裹在 write_coordinator.write()
内执行，单写者模型下无并发窗口（与 tag.name 查重同模式，design D4）。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.user import ROLE_ADMIN, AppUser


class UserRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, user_id: int) -> AppUser | None:
        return self.session.get(AppUser, user_id)

    def get_by_username(self, username: str) -> AppUser | None:
        """用户名唯一性按小写比较（大小写不敏感，design D11）。"""
        return self.session.scalar(
            select(AppUser).where(func.lower(AppUser.username) == username.lower())
        )

    def list_all(self) -> list[AppUser]:
        return list(self.session.scalars(select(AppUser).order_by(AppUser.user_id)).all())

    def create(
        self,
        *,
        username: str,
        password_hash: str,
        role: str = "user",
    ) -> AppUser:
        user = AppUser(
            username=username,
            password_hash=password_hash,
            role=role,
            is_active=True,
        )
        self.session.add(user)
        self.session.flush()
        return user

    def set_password_hash(self, user: AppUser, password_hash: str) -> None:
        user.password_hash = password_hash
        user.must_change_password = False
        self.session.flush()

    def set_role(self, user: AppUser, role: str) -> None:
        user.role = role
        self.session.flush()

    def set_active(self, user: AppUser, is_active: bool) -> None:
        user.is_active = is_active
        self.session.flush()

    def touch_last_login(self, user: AppUser, now) -> None:
        user.last_login_at = now
        self.session.flush()

    def count_active_admins(self) -> int:
        """有效管理员数量；最后一个管理员保护的判定依据（design D13）。"""
        return int(
            self.session.scalar(
                select(func.count(AppUser.user_id)).where(
                    AppUser.role == ROLE_ADMIN, AppUser.is_active.is_(True)
                )
            )
            or 0
        )
