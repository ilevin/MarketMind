"""证券内部唯一标识：market:asset_type:symbol（见 PRD 第 9 节）。"""

from __future__ import annotations

VALID_MARKETS = frozenset({"CN", "HK"})
VALID_ASSET_TYPES = frozenset({"STOCK", "ETF", "INDEX"})

# 各市场默认货币
MARKET_CURRENCY = {"CN": "CNY", "HK": "HKD"}


class InvalidInstrumentError(ValueError):
    """证券参数非法。"""


def build_instrument_id(market: str, asset_type: str, symbol: str) -> str:
    """生成 instrument_id，例如 600519+CN+STOCK -> CN:STOCK:600519。"""
    market = market.upper()
    asset_type = asset_type.upper()
    symbol = symbol.strip()
    if market not in VALID_MARKETS:
        raise InvalidInstrumentError(f"不支持的市场: {market}")
    if asset_type not in VALID_ASSET_TYPES:
        raise InvalidInstrumentError(f"不支持的资产类型: {asset_type}")
    if not symbol or ":" in symbol:
        raise InvalidInstrumentError(f"非法证券代码: {symbol!r}")
    return f"{market}:{asset_type}:{symbol}"


def parse_instrument_id(instrument_id: str) -> tuple[str, str, str]:
    """拆解 instrument_id -> (market, asset_type, symbol)。"""
    parts = instrument_id.split(":", 2)
    if len(parts) != 3:
        raise InvalidInstrumentError(f"非法 instrument_id: {instrument_id!r}")
    return parts[0], parts[1], parts[2]
