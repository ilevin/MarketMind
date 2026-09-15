"""CSRF 中间件（multi-user-auth design D7）。

Cookie 认证下，所有改变状态的请求（POST/PUT/PATCH/DELETE）要求请求头
X-CSRF-Token 与当前 Session 绑定的 csrf_token 一致；不一致返回 403。

- 登录与首次 setup 接口豁免（调用前无 Session）；
- 匿名请求不拦截（由 require_user 返回 401/302，保持未登录语义优先）；
- GET/HEAD/OPTIONS 安全方法不校验。
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.auth.session import SESSION_COOKIE_NAME

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
EXEMPT_PATHS = frozenset({"/api/auth/login", "/api/auth/setup"})
CSRF_HEADER = "X-CSRF-Token"


class CsrfMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request, call_next):
        if request.method in SAFE_METHODS or request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token:
            # 解析有效 Session 才强制校验；匿名写请求放行给认证依赖返回 401
            from app.services.auth_service import AuthService

            with request.app.state.session_factory() as session:
                current = AuthService(session).resolve_session(token)
            if current is not None and request.headers.get(CSRF_HEADER) != current.csrf_token:
                return JSONResponse(status_code=403, content={"detail": "CSRF 校验失败，请刷新页面重试"})
        return await call_next(request)
