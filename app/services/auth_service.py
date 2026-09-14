"""认证业务服务（multi-user-auth）：登录、退出、修改密码、Session 校验链。

校验链（user-authentication spec）：
    Cookie token -> SHA-256 -> user_session（过期/撤销判定）->
    app_user -> is_active -> CurrentUser

写路径（登录/退出/撤销）经 write_coordinator；登录走进程内限速（design D14）。
日志禁止输出密码与 Session Token。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from app.auth.password import (
    dummy_verify,
    hash_password,
    validate_password,
    verify_password,
)
from app.auth.rate_limit import LoginRateLimiter
from app.auth.session import (
    CurrentUser,
    generate_csrf_token,
    generate_session_token,
    hash_token,
)
from app.db import write_coordinator
from app.models.user import utcnow
from app.repositories.user import UserRepository
from app.repositories.user_session import UserSessionRepository

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """认证业务错误基类。"""


class InvalidCredentialsError(AuthError):
    """用户名或密码错误（统一文案，不泄露账户存在性）。"""


class AccountDisabledError(AuthError):
    """账户已禁用。"""


class RateLimitedError(AuthError):
    """登录尝试过于频繁。"""


class SessionRevokedError(AuthError):
    """Session 不存在或已失效（logout 时不应发生）。"""


@dataclass(frozen=True)
class LoginResult:
    """登录产物：token 仅下发 Cookie 不落日志；csrf_token 注入页面 meta。"""

    token: str
    csrf_token: str
    user: CurrentUser


class AuthService:
    def __init__(self, session: Session, session_ttl_days: int = 7):
        self.session = session
        self.user_repo = UserRepository(session)
        self.session_repo = UserSessionRepository(session)
        self._ttl = timedelta(days=session_ttl_days)

    # ---- 登录 ----

    def login(
        self,
        *,
        username: str,
        password: str,
        client_key: str,
        limiter: LoginRateLimiter,
    ) -> LoginResult:
        """校验凭据并创建新 Session（登录即旋转）；失败计入限速。"""
        if limiter.is_blocked(client_key):
            raise RateLimitedError("登录尝试过于频繁，请稍后再试")

        user = self.user_repo.get_by_username(username)
        if user is None:
            # 时序抹平：对哑哈希执行同代价 Argon2 校验，消除用户名枚举侧信道
            dummy_verify(password)
            limiter.record_failure(client_key)
            raise InvalidCredentialsError("用户名或密码错误")
        if not verify_password(password, user.password_hash):
            limiter.record_failure(client_key)
            raise InvalidCredentialsError("用户名或密码错误")
        if not user.is_active:
            # 禁用账户：不计入限速（凭据正确），但不创建 Session
            raise AccountDisabledError("账户已被禁用，请联系管理员")

        token = generate_session_token()
        csrf_token = generate_csrf_token()
        now = utcnow()
        # Argon2 校验（~数百 ms）在锁外完成；写段进锁前先结束读事务，
        # 锁内以新快照重读用户行——旧快照跨锁边界 UPDATE 会触发 DuckDB
        # 写冲突（并发登录 500），且校验期间账户可能被禁用
        # （database-persistence spec：写路径的查询与写入同锁）。
        self.session.rollback()
        with write_coordinator.write():
            user = self.user_repo.get(user.user_id)
            if user is None or not user.is_active:
                raise AccountDisabledError("账户已被禁用，请联系管理员")
            self.session_repo.create(
                session_token_hash=hash_token(token),
                user_id=user.user_id,
                csrf_token=csrf_token,
                expires_at=now + self._ttl,
            )
            self.user_repo.touch_last_login(user, now)
            self.session.commit()
        limiter.reset(client_key)
        logger.info("用户登录成功: user_id=%s", user.user_id)
        return LoginResult(
            token=token,
            csrf_token=csrf_token,
            user=CurrentUser(
                user_id=user.user_id,
                username=user.username,
                role=user.role,
                session_token_hash=hash_token(token),
                csrf_token=csrf_token,
            ),
        )

    # ---- Session 校验链 ----

    def resolve_session(self, token: str | None) -> CurrentUser | None:
        """Cookie token -> CurrentUser；任一环节失败返回 None（不抛错）。"""
        if not token:
            return None
        row = self.session_repo.get_valid(hash_token(token), utcnow())
        if row is None:
            return None
        user = self.user_repo.get(row.user_id)
        if user is None or not user.is_active:
            # 用户被禁用后 Session 立即失效（即使未显式撤销）
            return None
        return CurrentUser(
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            session_token_hash=row.session_token_hash,
            csrf_token=row.csrf_token,
        )

    # ---- 退出 ----

    def logout(self, current_user: CurrentUser) -> None:
        """立即撤销当前 Session。"""
        with write_coordinator.write():
            self.session_repo.revoke(current_user.session_token_hash, utcnow())
            self.session.commit()
        logger.info("用户退出登录: user_id=%s", current_user.user_id)

    # ---- 修改自己的密码 ----

    def change_password(
        self, current_user: CurrentUser, *, old_password: str, new_password: str
    ) -> None:
        """校验旧密码后更新哈希，并撤销该用户全部 Session（含当前）。"""
        validate_password(new_password)
        user = self.user_repo.get(current_user.user_id)
        if user is None or not verify_password(old_password, user.password_hash):
            raise InvalidCredentialsError("原密码错误")
        # Argon2 哈希锁外预计算；写段在锁内以新快照重读（同 login）
        new_hash = hash_password(new_password)
        self.session.rollback()
        with write_coordinator.write():
            user = self.user_repo.get(current_user.user_id)
            if user is None:
                raise InvalidCredentialsError("原密码错误")
            self.user_repo.set_password_hash(user, new_hash)
            self.session_repo.revoke_all_for_user(user.user_id, utcnow())
            self.session.commit()
        logger.info("用户修改了自己的密码: user_id=%s", user.user_id)
