"""标签管理 API（v0.03 技术方案 §9；multi-user-auth：要求登录，按当前用户隔离）。"""

from __future__ import annotations

from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.dependencies import require_user
from app.auth.session import CurrentUser
from app.schemas import TagCreateRequest, TagItem, TagListResponse, TagUpdateRequest
from app.services.tag_service import (
    DuplicateTagNameError,
    TagInUseError,
    TagNameEmptyError,
    TagNameTooLongError,
    TagNotFoundError,
    TagService,
)

router = APIRouter(
    prefix="/api/tags",
    tags=["tags"],
    dependencies=[Depends(require_user)],
)


def get_tag_service(
    request: Request, current_user: CurrentUser = Depends(require_user)
) -> Iterator[TagService]:
    with request.app.state.session_factory() as session:
        yield TagService(session, current_user.user_id)


def _error_status(exc: Exception) -> int:
    if isinstance(exc, (DuplicateTagNameError, TagInUseError)):
        return 409
    if isinstance(exc, TagNotFoundError):
        return 404
    if isinstance(exc, (TagNameEmptyError, TagNameTooLongError)):
        return 422
    return 500


@router.get("", response_model=TagListResponse)
def list_tags(service: TagService = Depends(get_tag_service)):
    items = [
        TagItem(id=tag.tag_id, name=tag.name, usage_count=usage)
        for tag, usage in service.list_with_usage()
    ]
    return TagListResponse(items=items)


@router.post("", response_model=TagItem, status_code=201)
def create_tag(
    body: TagCreateRequest, service: TagService = Depends(get_tag_service)
):
    try:
        tag = service.create(body.name)
    except Exception as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return TagItem(id=tag.tag_id, name=tag.name, usage_count=0)


@router.patch("/{tag_id}", response_model=TagItem)
def rename_tag(
    tag_id: int,
    body: TagUpdateRequest,
    service: TagService = Depends(get_tag_service),
):
    try:
        tag = service.rename(tag_id, body.name)
    except Exception as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    usage = service.repo.count_usage(tag.tag_id)
    return TagItem(id=tag.tag_id, name=tag.name, usage_count=usage)


@router.delete("/{tag_id}", status_code=204)
def delete_tag(tag_id: int, service: TagService = Depends(get_tag_service)):
    try:
        service.delete(tag_id)
    except Exception as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
