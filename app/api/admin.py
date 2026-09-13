"""管理 API：手动刷新（调试与维护用，普通首页禁止周期调用）。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.schemas import RefreshResult

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/refresh/quotes", response_model=RefreshResult)
async def refresh_quotes(request: Request, force: bool = False):
    """执行一次行情刷新；force=True 可刷新已收盘市场（维护用途）。"""
    refresher = getattr(request.app.state, "refresh_service", None)
    if refresher is None:
        raise HTTPException(status_code=503, detail="刷新服务未就绪")
    try:
        result = await run_in_threadpool(refresher.refresh_all, force)
    except Exception:
        logger.exception("手动行情刷新失败")
        raise HTTPException(status_code=500, detail="刷新失败，详见服务日志")
    logger.info("手动行情刷新: %s", result)
    return RefreshResult(success=result.success, updated=result.updated, failed=result.failed)


@router.post("/refresh/fundamentals", response_model=RefreshResult)
async def refresh_fundamentals(request: Request):
    """手动刷新基本面（主要开发维护用途）。"""
    job = getattr(request.app.state, "fundamental_refresh", None)
    if job is None:
        raise HTTPException(status_code=503, detail="基本面刷新未接入")
    result = await run_in_threadpool(job.run_once)
    return RefreshResult(**result)
