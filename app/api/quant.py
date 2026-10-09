"""量化查询 REST API（etf-data-module，design D11）。

``GET /api/quant/etf/daily?symbol=&start=&end=&adjust=``：登录用户可用
（非 admin 专属——研究用户场景，PRD 场景三 AI Agent 的落地：Agent/回测
工具经 HTTP 获取标准序列，无需理解数据来源）。

校验：symbol 六位数字、adjust ∈ raw|qfq|hfq、start ≤ end、日期格式
（422）；未知 ETF 代码 404；复权因子缺失 409（含缺失日明细，不静默
回退 raw）。空区间返回 200 空 items。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth.dependencies import require_user
from app.auth.session import CurrentUser
from app.schemas.quant import EtfDailyItem, EtfDailyResponse
from app.services.quant.etf_data import (
    AdjustFactorMissingDaysError,
    AdjustFactorNotSyncedError,
    EtfDataService,
    UnknownEtfError,
)

router = APIRouter(
    prefix="/api/quant",
    tags=["quant"],
    dependencies=[Depends(require_user)],
)


@router.get("/etf/daily", response_model=EtfDailyResponse)
def get_etf_daily(
    request: Request,
    symbol: str = Query(..., pattern=r"^\d{6}$", description="ETF 代码（6 位数字，如 510300）"),
    start: date = Query(..., description="开始日期（含），YYYY-MM-DD"),
    end: date = Query(..., description="结束日期（含），YYYY-MM-DD"),
    adjust: Literal["raw", "qfq", "hfq"] = Query(
        "raw",
        description="raw=未复权；qfq=前复权（基准=全库最新因子，当前价=真实价）；hfq=后复权（回测首选）",
    ),
    _user: CurrentUser = Depends(require_user),
) -> EtfDailyResponse:
    """ETF 日线区间查询（登录用户即可；qfq/hfq 需复权因子已同步）。"""
    if start > end:
        raise HTTPException(
            status_code=422,
            detail=f"start({start.isoformat()}) 不能晚于 end({end.isoformat()})",
        )

    app = request.app
    try:
        with app.state.session_factory() as session:
            result = EtfDataService(session).get_etf_daily(
                symbol, start, end, adjust=adjust
            )
    except UnknownEtfError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AdjustFactorNotSyncedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AdjustFactorMissingDaysError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return EtfDailyResponse(
        symbol=result.symbol,
        name=result.name,
        ts_code=result.ts_code,
        instrument_id=result.instrument_id,
        adjust=result.adjust,
        items=[
            EtfDailyItem(
                trade_date=bar.trade_date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                amount=bar.amount,
                turnover_rate=bar.turnover_rate,
            )
            for bar in result.items
        ],
    )
