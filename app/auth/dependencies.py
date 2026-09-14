"""认证 / 授权 FastAPI 依赖（multi-user-auth design D6）。

- ``require_user``：API 用——未登录 401；
- ``require_user_page``：页面用——未登录 302 重定向 /login；
- ``require_admin``：admin API 用——非 admin 403（Router 层统一声明，
  避免新增接口漏加权限检查）；
- ``require_admin_page``：admin 页面用——未登录 302，已登录非 admin 403。

身份解析：Cookie token -> AuthService.resolve_session -> CurrentUser。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from app.auth.session import SESSION_COOKIE_NAME, CurrentUser
from app.services.auth_service import AuthService


def _resolve_current_user(request: Request) -> CurrentUser | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    with request.app.state.session_factory() as session:
        return AuthService(session).resolve_session(token)


def get_current_user_optional(request: Request) -> CurrentUser | None:
    """匿名返回 None；供可选登录场景使用。"""
    return _resolve_current_user(request)


def require_user(request: Request) -> CurrentUser:
    user = _resolve_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已失效")
    return user


def require_user_page(request: Request) -> CurrentUser:
    user = _resolve_current_user(request)
    if user is None:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user


def require_admin(user: CurrentUser = Depends(require_user)) -> CurrentUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def require_admin_page(user: CurrentUser = Depends(require_user_page)) -> CurrentUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
