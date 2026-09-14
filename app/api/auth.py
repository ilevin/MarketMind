"""认证 API（multi-user-auth）：登录、退出、当前用户、修改密码。

登录接口不要求 CSRF Token（登录前无 Session，design D7）；其余接口经
require_user 保护。登录成功 Set-Cookie（HttpOnly / SameSite=Lax / Path=/，
Secure 随配置），Cookie 只存随机 Session Token。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth.dependencies import require_user
from app.auth.password import WeakPasswordError
from app.auth.rate_limit import login_rate_key, login_rate_limiter
from app.auth.session import SESSION_COOKIE_NAME, CurrentUser
from app.schemas import (
    ChangePasswordRequest,
    ChangePasswordResponse,
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    MeResponse,
)
from app.services.auth_service import (
    AccountDisabledError,
    AuthService,
    InvalidCredentialsError,
    RateLimitedError,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _cookie_max_age(request: Request) -> int:
    return request.app.state.config.auth.session.ttl_days * 24 * 3600


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    session_cfg = request.app.state.config.auth.session
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=_cookie_max_age(request),
        httponly=True,
        samesite="lax",
        secure=session_cfg.cookie_secure,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, response: Response):
    """登录：成功创建新 Session（登录即旋转）并下发 Cookie。"""
    client_ip = request.client.host if request.client else "unknown"
    key = login_rate_key(client_ip, body.username)
    with request.app.state.session_factory() as session:
        auth = AuthService(
            session, session_ttl_days=request.app.state.config.auth.session.ttl_days
        )
        try:
            result = auth.login(
                username=body.username,
                password=body.password,
                client_key=key,
                limiter=login_rate_limiter,
            )
        except RateLimitedError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except AccountDisabledError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except InvalidCredentialsError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    _set_session_cookie(request, response, result.token)
    return LoginResponse(username=result.user.username, role=result.user.role)


@router.post("/logout", response_model=LogoutResponse)
def logout(
    request: Request,
    response: Response,
    current_user: CurrentUser = Depends(require_user),
):
    """退出登录：立即撤销当前 Session 并清除 Cookie。"""
    with request.app.state.session_factory() as session:
        AuthService(session).logout(current_user)
    _clear_session_cookie(response)
    return LogoutResponse(success=True)


@router.get("/me", response_model=MeResponse)
def me(current_user: CurrentUser = Depends(require_user)):
    return MeResponse(
        user_id=current_user.user_id,
        username=current_user.username,
        role=current_user.role,
    )


@router.post("/change-password", response_model=ChangePasswordResponse)
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    current_user: CurrentUser = Depends(require_user),
):
    """修改自己的密码：成功后该用户全部 Session 失效（含当前），需重新登录。"""
    with request.app.state.session_factory() as session:
        auth = AuthService(session)
        try:
            auth.change_password(
                current_user, old_password=body.old_password, new_password=body.new_password
            )
        except InvalidCredentialsError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except WeakPasswordError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    _clear_session_cookie(response)
    return ChangePasswordResponse(success=True)
