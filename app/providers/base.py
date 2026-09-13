"""Provider 内部标准模型与接口（见 PRD 第 8、23 节）。

第三方字段名只允许存在于具体 Provider 实现内部。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from app.models.instrument import Instrument


@dataclass(frozen=True)
class Quote:
    """统一行情模型（股票 / ETF / 指数共用）。"""

    instrument_id: str
    price: float | None
    change_percent: float | None
    volume_ratio: float | None = None
    previous_close: float | None = None
    source: str = ""
    source_timestamp: datetime | None = None
    delayed: bool = False


@dataclass(frozen=True)
class Fundamental:
    """统一估值模型（主要 A 股股票）。"""

    instrument_id: str
    trade_date: date
    pe_ttm: float | None = None
    pb: float | None = None
    dividend_yield_ttm: float | None = None
    source: str = "tushare"


@runtime_checkable
class QuoteProvider(Protocol):
    def get_quotes(self, instruments: list[Instrument]) -> dict[str, Quote]: ...


@runtime_checkable
class FundamentalProvider(Protocol):
    def get_fundamentals(self, instruments: list[Instrument]) -> dict[str, Fundamental]: ...


@runtime_checkable
class TradingCalendarProvider(Protocol):
    def is_trading_day(self, market: str, date: date) -> bool: ...


@runtime_checkable
class InstrumentNameProvider(Protocol):
    """证券名称识别：添加自选时自动获取名称，无法识别则返回 None。"""

    def get_name(self, market: str, asset_type: str, symbol: str) -> str | None: ...
