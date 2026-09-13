"""指数配置 API（仅 INDEX）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.watchlist import _error_status, _trigger_refresh, get_index_watchlist_service
from app.schemas import OrderUpdateRequest, WatchlistAddRequest, WatchlistItem, WatchlistListResponse

router = APIRouter(prefix="/api/index-watchlist", tags=["index-watchlist"])


@router.get("", response_model=WatchlistListResponse)
def list_index_watchlist(service=Depends(get_index_watchlist_service)):
    items = [
        WatchlistItem(
            instrument_id=inst.instrument_id,
            symbol=inst.symbol,
            name=inst.name,
            market=inst.market,
            asset_type=inst.asset_type,
            sort_order=sort_order,
        )
        for inst, sort_order in service.list()
    ]
    return WatchlistListResponse(items=items)


@router.post("", response_model=WatchlistItem, status_code=201)
def add_index_watchlist(
    body: WatchlistAddRequest,
    request: Request,
    service=Depends(get_index_watchlist_service),
):
    try:
        instrument_id = service.add(
            symbol=body.symbol, market=body.market, asset_type=body.asset_type
        )
    except Exception as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc

    _trigger_refresh(request, instrument_id)

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
def delete_index_watchlist(
    instrument_id: str, service=Depends(get_index_watchlist_service)
):
    if not service.remove(instrument_id):
        raise HTTPException(status_code=404, detail=f"指数配置中不存在: {instrument_id}")


@router.put("/order", response_model=WatchlistListResponse)
def reorder_index_watchlist(
    body: OrderUpdateRequest, service=Depends(get_index_watchlist_service)
):
    service.reorder({item.instrument_id: item.sort_order for item in body.items})
    return list_index_watchlist(service)
