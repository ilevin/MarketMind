"""AKShare 行情 Provider（本环境实测可用的腾讯通道）。

A股股票使用 ak.stock_zh_a_spot_tx（全市场接口，内存过滤自选，不落库多余数据）。
实测列名（akshare 1.18.91，2026-08-18）：
    code 代码（如 sh600519）、name 名称、zxj 最新价、zdf 涨跌幅(%)、
    lb 量比、pe_ttm 市盈率TTM、zd 涨跌额

其余资产见同目录 tencent.py（东财/新浪接口在当前环境被断连，故按市场配置数据源）。
"""

from __future__ import annotations

import logging

from app.models.instrument import Instrument
from app.providers.base import Quote
from app.providers.safe_values import safe_float

logger = logging.getLogger(__name__)

_CODE_MAP = {"sh": "sh", "sz": "sz"}


class AkshareQuoteProvider:
    """A股股票行情（全市场接口 + 内存过滤）。"""

    def __init__(self):
        pass

    def _fetch_all(self):
        import akshare as ak  # 延迟导入，避免拖慢应用启动

        return ak.stock_zh_a_spot_tx()

    def get_quotes(self, instruments: list[Instrument]) -> dict[str, Quote]:
        targets = [inst for inst in instruments if inst.market == "CN" and inst.asset_type == "STOCK"]
        if not targets:
            return {}

        # 期望的腾讯代码集合：sh600519 / sz000001
        wanted = {}
        for inst in targets:
            prefix = "sh" if inst.symbol.startswith("6") else "sz"
            wanted[f"{prefix}{inst.symbol}"] = inst

        last_exc: Exception | None = None
        df = None
        for _attempt in range(2):  # 单次任务最多重试 1 次
            try:
                df = self._fetch_all()
                break
            except Exception as exc:
                last_exc = exc
        if df is None:
            logger.error("AKShare(stock_zh_a_spot_tx) 请求失败: %s", last_exc)
            return {}

        quotes: dict[str, Quote] = {}
        for row in df.itertuples(index=False):
            code = getattr(row, "code", None)
            inst = wanted.get(str(code))
            if inst is None:
                continue  # 全市场接口：只保留自选，其余丢弃
            quotes[inst.instrument_id] = Quote(
                instrument_id=inst.instrument_id,
                price=safe_float(getattr(row, "zxj", None)),
                change_percent=safe_float(getattr(row, "zdf", None)),
                volume_ratio=safe_float(getattr(row, "lb", None)),
                previous_close=None,
                source="akshare",
                source_timestamp=None,
            )
        return quotes
