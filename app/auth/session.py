"""Session 工具与当前用户身份（multi-user-auth design D1/D6）。

- Cookie 只存高强度随机 token，数据库只存 SHA-256(token)；
- CurrentUser 由认证依赖从 Session 解析，是业务层唯一合法的用户身份来源
  （普通业务 API 不接受客户端传入 user_id）。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

SESSION_COOKIE_NAME = "marketmind_session"


def generate_session_token() -> str:
    """高强度随机 Session Token（仅存于 Cookie，日志禁止输出）。"""
    return secrets.token_urlsafe(32)


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Session Token 的 SHA-256 摘要（user_session 主键）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CurrentUser:
    """服务端解析出的当前用户；业务层据此注入用户作用域。"""

    user_id: int
    username: str
    role: str
    session_token_hash: str
    csrf_token: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"
