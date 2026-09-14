"""股票/ETF 自选 API（multi-user-auth：要求登录，按当前用户隔离）。"""

from __future__ import annotations

from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.dependencies import require_user
from app.auth.session import CurrentUser
from app.schemas import (
    OrderUpdateRequest,
    TagBrief,
    WatchlistAddRequest,
    WatchlistItem,
    WatchlistListResponse,
    WatchlistTagsRequest,
)
from app.services.tag_service import TagNotFoundError
from app.services.watchlist_service import (
    DuplicateItemError,
    IndexWatchlistService,
    InstrumentNotFoundError,
    InvalidInstrumentError,
    InvalidTypeError,
    TagNotAllowedError,
    WatchlistEntryNotFoundError,
    WatchlistService,
)

router = APIRouter(
    prefix="/api/watchlist",
    tags=["watchlist"],
    dependencies=[Depends(require_user)],
)


def get_watchlist_service(
    request: Request, current_user: CurrentUser = Depends(require_user)
) -> Iterator[WatchlistService]:
    with request.app.state.session_factory() as session:
        yield WatchlistService(
            session, request.app.state.name_provider, current_user.user_id
        )


def get_index_watchlist_service(
    request: Request, current_user: CurrentUser = Depends(require_user)
) -> Iterator[IndexWatchlistService]:
    with request.app.state.session_factory() as session:
        yield IndexWatchlistService(
            session, request.app.state.name_provider, current_user.user_id
        )


def _error_status(exc: Exception) -> int:
    if isinstance(exc, DuplicateItemError):
        return 409
    if isinstance(exc, (InstrumentNotFoundError, WatchlistEntryNotFoundError, TagNotFoundError)):
        return 404
    if isinstance(exc, TagNotAllowedError):
        return 400
    if isinstance(exc, (InvalidTypeError, InvalidInstrumentError)):
        return 422
    return 500


def _trigger_refresh(request: Request, instrument_id: str) -> None:
    """添加自选/指数后，无论市场状态如何均触发一次该资产行情更新（PRD 17.4）。"""
    refresher = getattr(request.app.state, "refresh_service", None)
    if refresher is None:
        return
    try:
        refresher.refresh_instruments_now([instrument_id])
    except Exception:  # 刷新失败不影响添加
        pass


def _trigger_fundamental_refresh(request: Request, instrument_id: str) -> None:
    """添加自选 A 股后立即获取该股最近一期估值；失败不影响添加。"""
    job = getattr(request.app.state, "fundamental_refresh", None)
    if job is None:
        return
    try:
        job.refresh_instruments([instrument_id])
    except Exception:  # 估值获取失败不影响添加
        pass


@router.get("", response_model=WatchlistListResponse)
def list_watchlist(service: WatchlistService = Depends(get_watchlist_service)):
    items = [
        WatchlistItem(
            instrument_id=inst.instrument_id,
            symbol=inst.symbol,
            name=inst.name,
            market=inst.market,
            asset_type=inst.asset_type,
            sort_order=sort_order,
            tags=[TagBrief(id=tag.tag_id, name=tag.name) for tag in tags],
        )
        for inst, sort_order, tags in service.list_with_tags()
    ]
    return WatchlistListResponse(items=items)


@router.post("", response_model=WatchlistItem, status_code=201)
def add_watchlist(
    body: WatchlistAddRequest,
    request: Request,
    service: WatchlistService = Depends(get_watchlist_service),
):
    try:
        instrument_id = service.add(
            symbol=body.symbol, market=body.market, asset_type=body.asset_type
        )
    except Exception as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc

    _trigger_refresh(request, instrument_id)
    if instrument_id.startswith("CN:STOCK:"):
        _trigger_fundamental_refresh(request, instrument_id)
    inst = service.instrument_repo.get(instrument_id)
    sort_order = service.repo.get(instrument_id).sort_order
    return WatchlistItem(
        instrument_id=inst.instrument_id,
        symbol=inst.symbol,
        name=inst.name,
        market=inst.market,
        asset_type=inst.asset_type,
        sort_order=sort_order,
    )


@router.delete("/{instrument_id}", status_code=204)
def delete_watchlist(
    instrument_id: str, service: WatchlistService = Depends(get_watchlist_service)
):
    if not service.remove(instrument_id):
        raise HTTPException(status_code=404, detail=f"自选中不存在: {instrument_id}")


@router.put("/{instrument_id}/tags", response_model=WatchlistItem)
def set_watchlist_tags(
    instrument_id: str,
    body: WatchlistTagsRequest,
    service: WatchlistService = Depends(get_watchlist_service),
):
    """以全量集合替换自选条目的全部标签（v0.03b 多对多）；空数组即解除全部。"""
    try:
        inst, sort_order, tags = service.set_tags(instrument_id, body.tag_ids)
    except Exception as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    return WatchlistItem(
        instrument_id=inst.instrument_id,
        symbol=inst.symbol,
        name=inst.name,
        market=inst.market,
        asset_type=inst.asset_type,
        sort_order=sort_order,
        tags=[TagBrief(id=tag.tag_id, name=tag.name) for tag in tags],
    )


@router.put("/order", response_model=WatchlistListResponse)
def reorder_watchlist(
    body: OrderUpdateRequest, service: WatchlistService = Depends(get_watchlist_service)
):
    service.reorder({item.instrument_id: item.sort_order for item in body.items})
    return list_watchlist(service)
