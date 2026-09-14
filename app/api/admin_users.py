"""管理员用户管理 API（multi-user-auth design D6/D13）。

Router 层统一 require_admin：未登录 401、普通用户 403。
第一阶段不提供 DELETE（只禁用不物理删除）；响应不含 password_hash。
"""

from __future__ import annotations

import logging
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.dependencies import require_admin
from app.auth.password import WeakPasswordError
from app.auth.session import CurrentUser
from app.schemas import (
    ResetPasswordRequest,
    ResetPasswordResponse,
    UserCreateRequest,
    UserItem,
    UserListResponse,
    UserPatchRequest,
)
from app.services.user_service import (
    DuplicateUsernameError,
    InvalidRoleError,
    LastAdminError,
    UserNotFoundError,
    UserService,
    UsernameRuleError,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/admin/users",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


def get_user_service(request: Request) -> Iterator[UserService]:
    with request.app.state.session_factory() as session:
        yield UserService(session)


def _error_status(exc: Exception) -> int:
    if isinstance(exc, (DuplicateUsernameError, LastAdminError)):
        return 409
    if isinstance(exc, UserNotFoundError):
        return 404
    if isinstance(exc, (UsernameRuleError, InvalidRoleError, WeakPasswordError)):
        return 422
    return 500


def _to_item(user) -> UserItem:
    return UserItem(
        user_id=user.user_id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        must_change_password=user.must_change_password,
        created_at=user.created_at.isoformat() if user.created_at else None,
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
    )


@router.get("", response_model=UserListResponse)
def list_users(service: UserService = Depends(get_user_service)):
    return UserListResponse(items=[_to_item(u) for u in service.list_all()])


@router.post("", response_model=UserItem, status_code=201)
def create_user(
    body: UserCreateRequest, service: UserService = Depends(get_user_service)
):
    try:
        user = service.create_user(
            username=body.username, password=body.password, role=body.role
        )
    except Exception as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return _to_item(user)


@router.patch("/{user_id}", response_model=UserItem)
def patch_user(
    user_id: int,
    body: UserPatchRequest,
    service: UserService = Depends(get_user_service),
):
    """修改角色 / 启用状态；角色变化或禁用后该用户 Session 全部失效。

    两个字段合并为单事务：409（last-admin 保护）拒绝时不留半截已提交副作用。
    """
    try:
        service.update_user(user_id, role=body.role, is_active=body.is_active)
    except Exception as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return _to_item(service.get(user_id))


@router.post("/{user_id}/reset-password", response_model=ResetPasswordResponse)
def reset_password(
    user_id: int,
    body: ResetPasswordRequest,
    service: UserService = Depends(get_user_service),
):
    """重置密码并撤销该用户全部 Session。"""
    try:
        service.reset_password(user_id, body.new_password)
    except Exception as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return ResetPasswordResponse(success=True)
