"""用户仓储（multi-user-auth）：用户名小写比较、写锁内查重、角色/状态/密码更新。

Repository 只 flush 不 commit（database-persistence 事务边界）；用户名查重与
创建必须由调用方（UserService/AuthService）包裹在 write_coordinator.write()
内执行，单写者模型下无并发窗口（与 tag.name 查重同模式，design D4）。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.password import PLACEHOLDER_HASH
from app.models.user import ROLE_ADMIN, AppUser

INITIALIZATION_EMPTY = "empty"
INITIALIZATION_PLACEHOLDER = "placeholder"
INITIALIZATION_INITIALIZED = "initialized"
LEGACY_OWNER_USERNAME = "admin"


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

    def get_initialization_state(self) -> str:
        """返回首用户引导状态；异常由调用方处理，不能降级为空库。"""
        users = self.list_all()
        if not users:
            return INITIALIZATION_EMPTY
        if (
            len(users) == 1
            and users[0].username.lower() == LEGACY_OWNER_USERNAME
            and users[0].password_hash == PLACEHOLDER_HASH
            and users[0].role == ROLE_ADMIN
            and users[0].is_active
            and users[0].must_change_password
        ):
            return INITIALIZATION_PLACEHOLDER
        return INITIALIZATION_INITIALIZED

    def get_placeholder_owner(self) -> AppUser | None:
        """读取仍处于迁移占位状态的唯一 legacy owner。"""
        user = self.session.scalar(
            select(AppUser).where(
                func.lower(AppUser.username) == LEGACY_OWNER_USERNAME,
                AppUser.password_hash == PLACEHOLDER_HASH,
                AppUser.role == ROLE_ADMIN,
                AppUser.is_active.is_(True),
                AppUser.must_change_password.is_(True),
            )
        )
        return user

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

    def claim_placeholder_admin(
        self, user: AppUser, *, username: str, password_hash: str
    ) -> None:
        """用首访信息认领 legacy owner，保留原 user_id 及外键数据。"""
        user.username = username
        user.password_hash = password_hash
        user.role = ROLE_ADMIN
        user.is_active = True
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
        """可登录管理员数量；迁移占位账户不计入最后管理员保护。"""
        return int(
            self.session.scalar(
                select(func.count(AppUser.user_id)).where(
                    AppUser.role == ROLE_ADMIN,
                    AppUser.is_active.is_(True),
                    AppUser.password_hash != PLACEHOLDER_HASH,
                )
            )
            or 0
        )
