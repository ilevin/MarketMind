"""量化查询 API Schema（etf-data-module，design D11）。"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class EtfDailyItem(BaseModel):
    """单交易日条目（复权后的价格口径由顶层 adjust 字段说明）。"""

    trade_date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    # 单位口径（东财在线 spike 固化）：volume 手 / amount 元 / turnover_rate %
    volume: int | None = None
    amount: float | None = None
    turnover_rate: float | None = None


class EtfDailyResponse(BaseModel):
    """GET /api/quant/etf/daily 响应（design D11）。

    qfq 基准为该 ETF 全库最新复权因子（当前价=真实价），历史区间回测
    注意基准随新因子滚动；hfq 复现性最强（回测首选）。
    """

    symbol: str
    name: str | None = None
    ts_code: str
    instrument_id: str
    adjust: str
    items: list[EtfDailyItem]
