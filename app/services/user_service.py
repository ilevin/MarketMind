"""用户管理业务服务（multi-user-auth）：创建、启停、角色、重置密码。

- 用户名规则：3~32 字符、仅 [A-Za-z0-9_-]、唯一性大小写不敏感（design D11）；
- 只禁用不物理删除（design D9/D13）；
- 最后一个有效管理员保护：不能禁用/降级最后一个 admin（409）；
- 禁用、改角色、重置密码均撤销该用户全部 Session（design D12）；
- 写锁内查询查重（与 tag.name 同模式，单写者模型下无并发窗口）。
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.auth.password import WeakPasswordError, hash_password, validate_password
from app.db import write_coordinator
from app.models.user import ROLES, ROLE_ADMIN, AppUser, utcnow
from app.repositories.user import UserRepository
from app.repositories.user_session import UserSessionRepository

logger = logging.getLogger(__name__)

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 32
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class UserServiceError(Exception):
    """用户管理业务错误基类。"""


class UsernameRuleError(UserServiceError):
    """用户名不合法（422）。"""


class InvalidRoleError(UserServiceError):
    """角色不合法（422）。"""


class DuplicateUsernameError(UserServiceError):
    """用户名已存在（409，大小写不敏感）。"""


class UserNotFoundError(UserServiceError):
    """用户不存在（404）。"""


class LastAdminError(UserServiceError):
    """操作会导致系统没有可登录管理员（409）。"""


class UserService:
    def __init__(self, session: Session):
        self.session = session
        self.user_repo = UserRepository(session)
        self.session_repo = UserSessionRepository(session)

    # ---- 查询 ----

    def get(self, user_id: int) -> AppUser:
        user = self.user_repo.get(user_id)
        if user is None:
            raise UserNotFoundError(f"用户不存在: {user_id}")
        return user

    def get_by_username(self, username: str) -> AppUser:
        """按用户名查找（大小写不敏感）；不存在抛 UserNotFoundError。"""
        user = self.user_repo.get_by_username(username)
        if user is None:
            raise UserNotFoundError(f"用户不存在: {username}")
        return user

    def list_all(self) -> list[AppUser]:
        return self.user_repo.list_all()

    # ---- 创建 ----

    def create_user(self, *, username: str, password: str, role: str = "user") -> AppUser:
        username = self._validate_username(username)
        validate_password(password)
        if role not in ROLES:
            raise InvalidRoleError(f"role 仅允许 {'/'.join(ROLES)}，收到 {role}")
        # Argon2 哈希（~数百 ms）锁外预计算，不占写锁
        password_hash = hash_password(password)
        with write_coordinator.write():
            if self.user_repo.get_by_username(username) is not None:
                raise DuplicateUsernameError(f"用户名已存在: {username}")
            user = self.user_repo.create(
                username=username, password_hash=password_hash, role=role
            )
            self.session.commit()
        logger.info("已创建用户: %s (role=%s)", username, role)
        return user

    # ---- 启用 / 禁用 ----

    def set_active(self, user_id: int, is_active: bool) -> AppUser:
        return self.update_user(user_id, is_active=is_active)

    # ---- 角色分配 ----

    def set_role(self, user_id: int, role: str) -> AppUser:
        return self.update_user(user_id, role=role)

    # ---- 角色 / 启用状态合并变更 ----

    def update_user(
        self, user_id: int, *, role: str | None = None, is_active: bool | None = None
    ) -> AppUser:
        """角色与启用状态合并为单事务（409 拒绝时不留半截已提交副作用）。

        - 值无变化（no-op）直接返回：不撤销 Session、不写库；
        - 最后一个有效管理员保护在写锁内判定（消除 TOCTOU 并发窗口）；
        - 角色变化或禁用均撤销该用户全部 Session（design D12）。
        """
        if role is not None and role not in ROLES:
            raise InvalidRoleError(f"role 仅允许 {'/'.join(ROLES)}，收到 {role}")
        user = self.get(user_id)
        if (role is None or role == user.role) and (
            is_active is None or is_active == user.is_active
        ):
            return user
        old_role, old_active = user.role, user.is_active

        # 结束读事务：写段在锁内以新快照重读（旧快照跨锁边界 UPDATE
        # 会触发 DuckDB 写冲突，同 login 的处理）
        self.session.rollback()
        with write_coordinator.write():
            user = self.get(user_id)
            demote = role is not None and role != user.role
            deactivate = is_active is not None and is_active is not user.is_active
            if demote or deactivate:
                self._guard_last_admin(
                    user,
                    demote=demote,
                    deactivate=deactivate,
                )
            if demote:
                self.user_repo.set_role(user, role)
                # 角色变化后撤销既有 Session，避免旧登录态保留旧角色（design D12）
                self.session_repo.revoke_all_for_user(user.user_id, utcnow())
            if deactivate:
                self.user_repo.set_active(user, is_active)
                if not is_active:
                    self.session_repo.revoke_all_for_user(user.user_id, utcnow())
            self.session.commit()
        logger.info(
            "已修改用户属性: user_id=%s role=%s->%s is_active=%s->%s",
            user_id,
            old_role if demote else "-",
            role if demote else "-",
            old_active if deactivate else "-",
            is_active if deactivate else "-",
        )
        return user

    # ---- 重置密码（管理员） ----

    def reset_password(self, user_id: int, new_password: str) -> AppUser:
        validate_password(new_password)
        # Argon2 哈希锁外预计算，不占写锁
        password_hash = hash_password(new_password)
        self.session.rollback()
        with write_coordinator.write():
            user = self.get(user_id)
            self.user_repo.set_password_hash(user, password_hash)
            self.session_repo.revoke_all_for_user(user.user_id, utcnow())
            self.session.commit()
        logger.info("管理员重置了用户密码: user_id=%s", user_id)
        return user

    # ---- 校验 ----

    @staticmethod
    def _validate_username(username: str) -> str:
        username = (username or "").strip()
        if not (USERNAME_MIN_LENGTH <= len(username) <= USERNAME_MAX_LENGTH):
            raise UsernameRuleError(
                f"用户名长度须为 {USERNAME_MIN_LENGTH}~{USERNAME_MAX_LENGTH} 个字符"
            )
        if not _USERNAME_RE.fullmatch(username):
            raise UsernameRuleError("用户名仅允许字母、数字、下划线与连字符")
        return username

    def _guard_last_admin(self, user: AppUser, *, demote: bool, deactivate: bool) -> None:
        """变更会使最后一个有效管理员失去权限时，拒绝降级/禁用。

        必须在写锁内调用：count_active_admins 的读与后续写入同锁，
        两个并发管理操作不会同时清零管理员（TOCTOU）。
        """
        if user.role != ROLE_ADMIN or not user.is_active:
            return
        if self.user_repo.count_active_admins() <= 1:
            action = "降级" if demote else "禁用"
            raise LastAdminError(f"不能{action}最后一个有效管理员")
